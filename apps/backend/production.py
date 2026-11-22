import asyncio
import base64
import json
import logging
import re
from shutil import which
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from collection import connection
from character_library import initialize_character_library
from config import CONFIG_PATH
from narration_timing import dialogue_sentences, spoken_text, timing_issues


router = APIRouter()
logger = logging.getLogger("uvicorn.error")
ARK ="https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks"
INTERACTIONS = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_MODEL = "gemini-omni-1.2-flash"
SEEDANCE_MODEL = "dreamina-seedance-2-0-260128"
MODEL_LABELS = {GEMINI_MODEL: "Gemini Omni 1.2 Flash", SEEDANCE_MODEL: "Seedance 2.0"}
# Seedance 2.0 online 480p/720p price; audio and flex tiers have no separate rate.
SEEDANCE_USD_PER_MILLION_TOKENS = 7.0
ACTIVE = {"submitting", "queued", "running"}
MAX_CLIP_BYTES = 100 * 1024 * 1024
MAX_CHARACTER_BYTES = 4 * 1024 * 1024


class ClipRequest(BaseModel):
    revision: UUID
    scene: int | None = None
    continue_previous: bool = False


class SequenceRequest(BaseModel):
    revision: UUID
    cut_scenes: list[int] = Field(default_factory=list, max_length=8)


class ExportRequest(BaseModel):
    revision: UUID


def initialize_production(db):
    db.execute("""CREATE TABLE IF NOT EXISTS production_stops (
        project_id uuid NOT NULL, revision uuid NOT NULL,
        PRIMARY KEY (project_id, revision)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS production_clips (
        project_id uuid NOT NULL, revision uuid NOT NULL, scene integer NOT NULL,
        status text NOT NULL, task_id text, asset_id uuid, error text,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (project_id, revision, scene)
    )""")
    db.execute("ALTER TABLE production_clips ADD COLUMN IF NOT EXISTS cancel_attempted boolean NOT NULL DEFAULT false")
    db.execute("""CREATE TABLE IF NOT EXISTS production_exports (
        project_id uuid NOT NULL, revision uuid NOT NULL, asset_id uuid NOT NULL,
        PRIMARY KEY (project_id, revision)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS production_characters (
        project_id uuid PRIMARY KEY, version uuid NOT NULL, mime_type text NOT NULL,
        image_bytes bytea NOT NULL
    )""")
    initialize_character_library(db)
    db.execute("ALTER TABLE production_clips ADD COLUMN IF NOT EXISTS character_version uuid")
    db.execute("ALTER TABLE production_clips ADD COLUMN IF NOT EXISTS asset_character_version uuid")
    db.execute("ALTER TABLE production_clips ADD COLUMN IF NOT EXISTS asset_usage jsonb")
    db.execute("ALTER TABLE production_clips ADD COLUMN IF NOT EXISTS usage_checked_at timestamptz")
    db.execute("ALTER TABLE production_exports ADD COLUMN IF NOT EXISTS created_at timestamptz")
    db.execute("""CREATE TABLE IF NOT EXISTS production_sequences (
        project_id uuid NOT NULL, revision uuid NOT NULL, cut_scenes integer[] NOT NULL,
        status text NOT NULL, error text, PRIMARY KEY (project_id, revision)
    )""")


def settings():
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    video = data.get("video_production") or {}
    if not isinstance(video, dict):
        raise HTTPException(503, "video_production 설정 형식을 확인해 주세요.")
    return video


@router.get("/api/project-history")
def project_history(
    q: str = Query(default="", max_length=100), offset: int = Query(default=0, ge=0, le=2_147_483_647),
):
    with connection() as db:
        projects = db.execute(
            """SELECT p.id, p.created_at, r.category, r.url, r.shortcode,
                      b.draft->>'title' AS title, a.updated_at AS analyzed_at,
                      s.updated_at AS planned_at, b.updated_at AS storyboard_at,
                      greatest(p.created_at, a.updated_at, s.updated_at, b.updated_at,
                               clips.changed_at, exports.latest_at) AS updated_at,
                      coalesce(clips.completed, 0) AS completed_clips,
                      coalesce(jsonb_array_length(b.draft->'scenes'), 0) AS scene_count,
                      coalesce(exports.items, '[]'::jsonb) AS exports,
                      coalesce(exports.has_current, false) AS has_current_export
               FROM project p JOIN reference r ON p.shortcode = r.shortcode
               LEFT JOIN project_video_analysis a ON a.project_id = p.id
               LEFT JOIN project_replanning s ON s.project_id = p.id
               LEFT JOIN project_storyboard b ON b.project_id = p.id
               LEFT JOIN production_characters ch ON ch.project_id = p.id
               LEFT JOIN LATERAL (
                   SELECT max(c.updated_at) AS changed_at,
                          count(*) FILTER (WHERE c.asset_id IS NOT NULL
                              AND c.asset_character_version IS NOT DISTINCT FROM ch.version
                              AND c.revision::text = (b.draft->>'revision')) AS completed
                   FROM production_clips c WHERE c.project_id = p.id
               ) clips ON true
               LEFT JOIN LATERAL (
                   SELECT max(e.created_at) AS latest_at,
                          jsonb_agg(jsonb_build_object('assetId', e.asset_id, 'createdAt', e.created_at,
                              'current', coalesce(e.revision::text = (b.draft->>'revision'), false))
                              ORDER BY e.created_at DESC NULLS LAST, e.asset_id) AS items,
                          bool_or(e.revision::text = (b.draft->>'revision')) AS has_current
                   FROM production_exports e WHERE e.project_id = p.id
               ) exports ON true
               WHERE %(term)s = '' OR strpos(lower(concat_ws(' ', b.draft->>'title', r.category,
                                                           r.notes, r.shortcode)), lower(%(term)s)) > 0
               ORDER BY updated_at DESC, p.id DESC LIMIT 21 OFFSET %(offset)s""",
            {"term": q.strip(), "offset": offset},
        ).fetchall()
    return {"items": projects[:20], "nextOffset": offset + 20 if len(projects) > 20 else None}


def asset_path(asset_id):
    root = Path(settings().get("storage_dir") or ".production-media")
    if not root.is_absolute():
        root = CONFIG_PATH.parent / root
    return root / f"{asset_id}.mp4"


def production_model():
    model = settings().get("model", GEMINI_MODEL)
    if not isinstance(model, str) or model not in MODEL_LABELS:
        raise HTTPException(503, "config.yaml의 video_production.model에 지원되는 모델 ID를 입력해 주세요.")
    return model


def provider_key(*, required=True):
    key = settings().get("api_key")
    if not isinstance(key, str) or not key.strip():
        if not required:
            return None
        raise HTTPException(503, "config.yaml의 video_production.api_key에 영상 생성 API 키를 설정해 주세요.")
    return key.strip()


def gemini_headers(key):
    return {"x-goog-api-key": key, "Api-Revision": "2026-05-20"}


def load_draft(db, project_id, revision=None):
    row = db.execute(
        """SELECT p.id, b.draft, s.settings FROM project p
           LEFT JOIN project_storyboard b ON b.project_id = p.id
           LEFT JOIN project_replanning s ON s.project_id = p.id WHERE p.id = %s""",
        (project_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    draft = row["draft"]
    if not draft:
        raise HTTPException(409, "스토리보드를 먼저 생성해 주세요.")
    if revision is not None and str(revision) != draft["revision"]:
        raise HTTPException(409, "스토리보드가 바뀌었습니다. 최신 화면을 다시 열어 주세요.")
    return draft, draft.get("production_settings") or row["settings"] or {}


def lock_project(db, project_id):
    return db.execute(
        "SELECT pg_try_advisory_lock(hashtext('production'), hashtext(%s)) AS acquired",
        (str(project_id),),
    ).fetchone()["acquired"]


def clip_rows(db, project_id, revision):
    return db.execute(
        """SELECT scene, status, task_id, asset_id, error, character_version, asset_character_version, asset_usage,
                  cancel_attempted,
                  (usage_checked_at IS NULL OR usage_checked_at < now() - interval '1 hour') AS can_check_usage,
                  updated_at < now() - interval '3 minutes' AS submission_stale
           FROM production_clips WHERE project_id = %s AND revision = %s ORDER BY scene""",
        (project_id, revision),
    ).fetchall()


def redacted_payload(payload):
    """Return a copy of a clip request payload with base64 image data replaced by a short summary."""
    if isinstance(payload, str) and payload.startswith("data:"):
        header, _, data = payload.partition("base64,")
        return f"<data:{header[5:].split(';')[0]}, {len(data)} base64 chars>"
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), str) and "mime_type" in payload:
            return {**{k: redacted_payload(v) for k, v in payload.items()},
                    "data": f"<{payload['mime_type']}, {len(payload['data'])} base64 chars>"}
        return {k: redacted_payload(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [redacted_payload(v) for v in payload]
    return payload


def stop_requested(db, project_id, revision):
    return db.execute(
        "SELECT 1 FROM production_stops WHERE project_id = %s AND revision = %s",
        (project_id, revision),
    ).fetchone() is not None


def clear_stop(db, project_id, revision):
    db.execute(
        "DELETE FROM production_stops WHERE project_id = %s AND revision = %s",
        (project_id, revision),
    )


def state(db, project_id, revision):
    character = db.execute(
        "SELECT version FROM production_characters WHERE project_id = %s", (project_id,),
    ).fetchone()
    version = character["version"] if character else None
    clips = [
        {"scene": row["scene"], "status": row["status"], "assetId": row["asset_id"], "error": row["error"],
         "matchesCharacter": row["asset_character_version"] == version, "usage": row["asset_usage"]}
        for row in clip_rows(db, project_id, revision)
    ]
    result = db.execute(
        "SELECT asset_id FROM production_exports WHERE project_id = %s AND revision = %s",
        (project_id, revision),
    ).fetchone()
    sequence = db.execute(
        "SELECT status, error, cut_scenes FROM production_sequences WHERE project_id = %s AND revision = %s",
        (project_id, revision),
    ).fetchone()
    return {
        "model": MODEL_LABELS[production_model()],
        "clips": clips,
        "exportId": result["asset_id"] if result and all(clip["matchesCharacter"] for clip in clips) else None,
        "characterVersion": version,
        "sequence": sequence,
        "stopRequested": stop_requested(db, project_id, revision),
    }


@router.put("/api/projects/{project_id}/production/character")
async def upload_character(project_id: UUID, revision: UUID, request: Request):
    mime = request.headers.get("content-type", "").partition(";")[0].lower().strip()
    if mime not in {"image/png", "image/jpeg"}:
        raise HTTPException(415, "JPG 또는 PNG 이미지를 선택해 주세요.")
    image = bytearray()
    async for chunk in request.stream():
        if len(image) + len(chunk) > MAX_CHARACTER_BYTES:
            raise HTTPException(413, "4 MiB 이하의 이미지를 선택해 주세요.")
        image.extend(chunk)
    signature = b"\x89PNG\r\n\x1a\n" if mime == "image/png" else b"\xff\xd8\xff"
    if not image.startswith(signature):
        raise HTTPException(422, "이미지 형식을 확인해 주세요.")
    with connection(autocommit=True) as db:
        if not lock_project(db, project_id):
            raise HTTPException(409, "다른 제작 작업이 끝난 뒤 이미지를 바꿔 주세요.")
        load_draft(db, project_id, revision)
        sequence = db.execute(
            "SELECT status FROM production_sequences WHERE project_id = %s AND revision = %s",
            (project_id, revision),
        ).fetchone()
        if sequence and sequence["status"] == "running":
            raise HTTPException(409, "이어 생성이 끝난 뒤 기준 이미지를 바꿔 주세요.")
        if any(row["status"] in ACTIVE for row in clip_rows(db, project_id, revision)):
            raise HTTPException(409, "클립 생성이 끝난 뒤 이미지를 바꿔 주세요.")
        current = db.execute(
            "SELECT image_bytes, mime_type FROM production_characters WHERE project_id = %s", (project_id,),
        ).fetchone()
        if current and current["image_bytes"] == bytes(image) and current["mime_type"] == mime:
            return state(db, project_id, revision)
        with db.transaction():
            db.execute(
                """INSERT INTO production_characters (project_id, version, mime_type, image_bytes)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (project_id) DO UPDATE
                   SET version = excluded.version, mime_type = excluded.mime_type, image_bytes = excluded.image_bytes""",
                (project_id, uuid4(), mime, bytes(image)),
            )
            db.execute("DELETE FROM production_exports WHERE project_id = %s", (project_id,))
        return state(db, project_id, revision)


@router.get("/api/projects/{project_id}/production/character/{version}")
def view_character(project_id: UUID, version: UUID):
    with connection() as db:
        image = db.execute(
            """SELECT mime_type, image_bytes FROM production_characters
               WHERE project_id = %s AND version = %s""",
            (project_id, version),
        ).fetchone()
    if image is None:
        raise HTTPException(404, "기준 이미지를 찾을 수 없습니다.")
    return Response(bytes(image["image_bytes"]), media_type=image["mime_type"],
                    headers={"X-Content-Type-Options": "nosniff"})


def scene_prompt(draft, planning, index):
    scene = draft["scenes"][index]
    seconds = draft["durations"][index]
    lines = [
        "Vertical 9:16 short-form video, one scene.",
        f"Overall direction: {draft['direction']}",
        f"Main character and staging: {planning.get('age', '')} {planning.get('role', '')}, "
        f"{planning.get('tone', '')}, {planning.get('staging', '')}.",
        f"Scene: {scene['scene_direction']}",
        f"Motion: {scene['motion_direction']}",
        f"Length: {seconds} seconds.",
    ]
    sentences = dialogue_sentences(spoken_text(draft, index))
    if sentences:
        lines.append(
            "Dialogue: the main character speaks Korean and says exactly the quoted lines below, in order, "
            f"word for word, including every particle, and nothing else. Number of lines: {len(sentences)}."
        )
        lines.append("Speak naturally and clearly, at a conversational pace.")
        lines += [f'{number}. "{sentence}"' for number, sentence in enumerate(sentences, 1)]
        lines.append("These quoted lines are the only speech in this video. Do not add, skip, reorder, "
                     "replace, or repeat any word. No ad-libs and no extra sentences.")
        lines.append("After reading all the quoted lines, the main character stays silent "
                     "for the rest of the clip and keeps a natural expression.")
    else:
        lines.append("The main character stays silent for the whole clip and keeps a natural expression.")
    lines.append("Audio: only the main character's voice. No background music. No other voices.")
    return "\n".join(lines)


def extract_continuity_frame(path):
    ffmpeg = which(settings().get("ffmpeg", "ffmpeg"))
    if not ffmpeg or not path.is_file():
        raise FileNotFoundError("FFmpeg or previous clip unavailable")
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-sseof", "-0.5", "-i", str(path.resolve()),
         "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=720:1280:force_original_aspect_ratio=decrease",
         "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25, check=True,
    )
    if not result.stdout.startswith(b"\xff\xd8\xff") or len(result.stdout) > MAX_CHARACTER_BYTES:
        raise ValueError("Could not extract a JPEG")
    return base64.b64encode(result.stdout).decode("ascii")


@router.post("/api/projects/{project_id}/production/sequence", status_code=202)
async def start_sequence(project_id: UUID, request: SequenceRequest):
    if production_model() != GEMINI_MODEL:
        raise HTTPException(422, "이어 생성은 Gemini Omni에서 사용할 수 있습니다.")
    provider_key()
    with connection(autocommit=True) as db:
        if not lock_project(db, project_id):
            raise HTTPException(409, "다른 제작 작업이 끝난 뒤 다시 시도해 주세요.")
        draft, _ = load_draft(db, project_id, request.revision)
        if any(index < 1 or index >= len(draft["scenes"]) for index in request.cut_scenes):
            raise HTTPException(422, "전환 장면의 번호를 확인해 주세요.")
        if not db.execute(
            "SELECT 1 FROM production_characters WHERE project_id = %s", (project_id,),
        ).fetchone():
            raise HTTPException(422, "캐릭터 기준 이미지를 먼저 등록해 주세요.")
        with db.transaction():
            if any(row["status"] in ACTIVE for row in clip_rows(db, project_id, request.revision)):
                raise HTTPException(409, "진행 중인 장면의 상태가 확정된 뒤 다시 시도해 주세요.")
            clear_stop(db, project_id, request.revision)
            db.execute(
                """INSERT INTO production_sequences (project_id, revision, cut_scenes, status)
                   VALUES (%s, %s, %s, 'running') ON CONFLICT (project_id, revision)
                   DO UPDATE SET cut_scenes = excluded.cut_scenes, status = 'running', error = NULL""",
                (project_id, request.revision, sorted(set(request.cut_scenes))),
            )
            db.execute(
                """UPDATE production_clips SET status = 'waiting', error = NULL
                   WHERE project_id = %s AND revision = %s AND status IN ('failed', 'cancelled')""",
                (project_id, request.revision),
            )
    await advance_sequence(project_id, request.revision)
    with connection() as db:
        return state(db, project_id, request.revision)


async def advance_sequence(project_id, revision):
    next_request = None
    with connection() as db:
        current = state(db, project_id, revision)
        sequence = current["sequence"]
        if current["stopRequested"] or sequence is None or sequence["status"] != "running":
            return
        clips = {clip["scene"]: clip for clip in current["clips"]}
        if any(clip["status"] in ACTIVE for clip in clips.values()):
            return
        draft, _ = load_draft(db, project_id, revision)
        index = next((index for index in range(len(draft["scenes"]))
                      if not (index in clips and clips[index]["status"] == "succeeded"
                              and clips[index]["matchesCharacter"] and clips[index]["assetId"])), None)
        if index is None:
            db.execute(
                "UPDATE production_sequences SET status = 'completed' WHERE project_id = %s AND revision = %s AND status = 'running'",
                (project_id, revision),
            )
            return
        if index in clips and clips[index]["status"] in {"failed", "unknown", "cancelled"}:
            db.execute(
                "UPDATE production_sequences SET status = 'stopped', error = %s WHERE project_id = %s AND revision = %s AND status = 'running'",
                (f"{index + 1}번째 장면의 생성 상태를 확인해 주세요.", project_id, revision),
            )
            return
        next_request = ClipRequest(
            revision=revision, scene=index,
            continue_previous=index > 0 and index not in sequence["cut_scenes"],
        )
    try:
        await submit_clips(project_id, next_request, sequence=True)
    except HTTPException as exc:
        if exc.status_code == 409:
            return
        with connection() as db:
            db.execute(
                """UPDATE production_sequences SET status = 'stopped', error = %s
                   WHERE project_id = %s AND revision = %s AND status = 'running'""",
                (str(exc.detail), project_id, revision),
            )


@router.post("/api/projects/{project_id}/production/clips", status_code=202)
async def start_clips(project_id: UUID, request: ClipRequest):
    return await submit_clips(project_id, request)


async def submit_clips(project_id, request, *, sequence=False):
    model = production_model()
    key = provider_key()
    with connection(autocommit=True) as db:
        if not lock_project(db, project_id):
            raise HTTPException(409, "다른 제작 작업을 처리 중입니다. 잠시 후 다시 시도해 주세요.")
        draft, planning = load_draft(db, project_id, request.revision)
        character = db.execute(
            "SELECT version, mime_type, image_bytes FROM production_characters WHERE project_id = %s",
            (project_id,),
        ).fetchone()
        version = character["version"] if character else None
        image_data = base64.b64encode(character["image_bytes"]).decode("ascii") if character else None
        indexes = range(len(draft["scenes"])) if request.scene is None else [request.scene]
        if request.scene is not None and not 0 <= request.scene < len(draft["scenes"]):
            raise HTTPException(422, "장면을 찾을 수 없습니다.")
        existing = {row["scene"]: row for row in clip_rows(db, project_id, request.revision)}
        issues = timing_issues(draft, draft["durations"], indexes)
        if issues:
            detail = ", ".join(f"{index + 1}번 장면 ({units}/{limit})" for index, units, limit in issues)
            raise HTTPException(422, "낭독 분량이 장면 길이를 초과합니다. 스토리보드에서 대본을 줄여 저장해 주세요. " + detail)
        if not sequence:
            if stop_requested(db, project_id, request.revision) and any(
                row["status"] in ACTIVE for row in existing.values()
            ):
                raise HTTPException(409, "중지를 요청한 장면의 상태가 확정된 뒤 다시 시도해 주세요.")
            clear_stop(db, project_id, request.revision)
        if sequence:
            running = db.execute(
                "SELECT status FROM production_sequences WHERE project_id = %s AND revision = %s",
                (project_id, request.revision),
            ).fetchone()
            if running is None or running["status"] != "running":
                return state(db, project_id, request.revision)
            if any(row["status"] in ACTIVE for row in existing.values()):
                return state(db, project_id, request.revision)
        previous_frame = None
        if request.continue_previous:
            if model != GEMINI_MODEL or not request.scene or not character:
                raise HTTPException(422, "이어 생성에는 Gemini Omni, 캐릭터 기준 이미지와 두 번째 이후 장면이 필요합니다.")
            previous = existing.get(request.scene - 1)
            if not previous or not previous["asset_id"] or previous["status"] in ACTIVE or previous["asset_character_version"] != version:
                raise HTTPException(409, "현재 기준 이미지로 이전 장면을 먼저 완성해 주세요.")
            try:
                previous_frame = await asyncio.to_thread(extract_continuity_frame, asset_path(previous["asset_id"]))
            except (OSError, ValueError, subprocess.SubprocessError):
                raise HTTPException(503, "이전 클립에서 프레임을 추출하지 못했습니다. 영상 파일과 FFmpeg를 확인해 주세요.") from None
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5)) as client:
            for index in indexes:
                if stop_requested(db, project_id, request.revision):
                    break
                clip = existing.get(index)
                if sequence and clip and clip["status"] in {"succeeded", "unknown", "failed"}:
                    if clip["status"] != "succeeded" or clip["asset_character_version"] == version:
                        continue
                if clip and (clip["status"] in ACTIVE or (
                    request.scene is None and (clip["status"] == "unknown" or (
                        clip["status"] == "succeeded" and clip["asset_character_version"] == version
                    ))
                )):
                    continue
                duration = draft["durations"][index]
                minimum, maximum = clip_duration_range(model)
                if not minimum <= duration <= maximum:
                    raise HTTPException(422, f"{MODEL_LABELS[model]}는 장면당 {minimum}~{maximum}초를 지원합니다.")
                db.execute(
                    """INSERT INTO production_clips (project_id, revision, scene, status, character_version)
                       VALUES (%s, %s, %s, 'submitting', %s) ON CONFLICT (project_id, revision, scene)
                       DO UPDATE SET status = 'submitting', task_id = NULL, error = NULL, updated_at = now(),
                                     character_version = excluded.character_version, usage_checked_at = NULL,
                                     cancel_attempted = false""",
                    (project_id, request.revision, index, version),
                )
                status, task_id, error_message = "unknown", None, "공급사 접수 여부를 확인하지 못했습니다."
                try:
                    prompt = scene_prompt(draft, {
                        **planning, "age": "", "role": "",
                    } if character else planning, index)
                    if character:
                        prompt = ("Use the person in the attached image as the main character of this scene. "
                                  "Keep the face, hairstyle, age group, and outfit; "
                                  "direct only the scene's motion and composition.\n" + prompt)
                    if model == GEMINI_MODEL:
                        url, headers = INTERACTIONS, gemini_headers(key)
                        payload = {
                            "model": model,
                            "input": prompt,
                            "background": True,
                            "response_format": {
                                "type": "video", "aspect_ratio": "9:16",
                                "resolution": "720p", "duration": f"{duration}s",
                            },
                        }
                        if character:
                            payload["input"] = [
                                {"type": "image", "data": image_data, "mime_type": character["mime_type"]},
                                {"type": "text", "text": prompt},
                            ]
                        if previous_frame:
                            payload["input"].insert(
                                1, {"type": "image", "data": previous_frame, "mime_type": "image/jpeg"},
                            )
                            payload["input"][-1]["text"] = (
                                "The first image is the reference for the character's face, hair, and outfit. "
                                "The second image is a frame from the previous scene, for continuing pose, "
                                "lighting, and composition. Do not copy captions or promotional text from the "
                                "reference images into the new scene. Do not interpolate between the two images.\n"
                                + prompt
                            )
                            payload["generation_config"] = {"video_config": {"task": "reference_to_video"}}
                    else:
                        url, headers = ARK, {"Authorization": f"Bearer {key}"}
                        payload = {
                            "model": model, "content": [{"type": "text", "text": prompt}],
                            "ratio": "9:16", "resolution": "720p", "duration": duration,
                            "generate_audio": True,
                        }
                        if character:
                            payload["content"].append({
                                "type": "image_url", "role": "first_frame",
                                "image_url": {"url": f"data:{character['mime_type']};base64,{image_data}"},
                            })
                    logger.info(
                        "Clip request: project=%s revision=%s scene=%s model=%s endpoint=%s payload=%s",
                        project_id, request.revision, index, model, url,
                        json.dumps(redacted_payload(payload), ensure_ascii=False, indent=2),
                    )
                    response = await client.post(url, headers=headers, json=payload)
                    if response.is_success:
                        task_id = response.json().get("id")
                        if isinstance(task_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
                            status, error_message = "queued", None
                            logger.info("Clip request accepted: project=%s scene=%s task=%s",
                                        project_id, index, task_id)
                        else:
                            task_id = None
                    elif 400 <= response.status_code < 500:
                        logger.warning(
                            "Clip request rejected: project=%s scene=%s model=%s status=%s body=%s",
                            project_id, index, model, response.status_code, response.text[:1000],
                        )
                        status = "failed"
                        if response.status_code in (401, 403):
                            error_message = f"{MODEL_LABELS[model]}의 API 키, 결제 및 모델 접근 권한을 확인해 주세요."
                        else:
                            error_message = f"{MODEL_LABELS[model]}가 영상 생성 요청을 거절했습니다. (HTTP {response.status_code})"
                except (httpx.RequestError, ValueError, AttributeError):
                    pass
                db.execute(
                    """UPDATE production_clips SET status = %s, task_id = %s, error = %s, updated_at = now()
                       WHERE project_id = %s AND revision = %s AND scene = %s""",
                    (status, task_id, error_message, project_id, request.revision, index),
                )
                if status == "unknown" or (status == "failed" and response.status_code in (401, 403, 429)):
                    break
            if stop_requested(db, project_id, request.revision):
                await cancel_active_clips(db, client, model, key, project_id, request.revision)
        return state(db, project_id, request.revision)


async def download_clip(client, url):
    if not isinstance(url, str):
        raise ValueError("missing URL")
    target = urlsplit(url)
    host = target.hostname or ""
    if (
        target.scheme != "https" or target.port not in (None, 443)
        or target.username or target.password
        or not host.endswith((".volces.com", ".bytepluses.com", ".byteplus.com"))
    ):
        raise ValueError("unexpected media host")
    asset = uuid4()
    destination = asset_path(asset)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".part")
    try:
        async with client.stream("GET", url, follow_redirects=False) as response:
            response.raise_for_status()
            size = 0
            with temporary.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_CLIP_BYTES:
                        raise ValueError("clip too large")
                    output.write(chunk)
        with temporary.open("rb") as video:
            if video.read(8)[4:8] != b"ftyp":
                raise ValueError("invalid mp4")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return asset


def save_omni_clip(interaction):
    for step in interaction.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for item in step.get("content", []):
            if item.get("type") != "video":
                continue
            encoded = item.get("data")
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_CLIP_BYTES + 2) // 3):
                raise ValueError("invalid video size")
            data = base64.b64decode(encoded, validate=True)
            if len(data) > MAX_CLIP_BYTES or data[4:8] != b"ftyp":
                raise ValueError("invalid mp4")
            asset = uuid4()
            path = asset_path(asset)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".part")
            try:
                temporary.write_bytes(data)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return asset
    raise ValueError("Gemini did not return a video")


def clip_duration_range(model):
    """Return the inclusive (minimum, maximum) clip seconds the model accepts."""
    return (3, 15) if model == GEMINI_MODEL else (4, 15)


def generation_usage(task):
    usage = task.get("usage")
    if not isinstance(usage, dict):
        return None
    model = task.get("model")
    if model == GEMINI_MODEL and isinstance(usage.get("total_tokens"), int):
        inputs = usage.get("total_input_tokens") or 0
        outputs = usage.get("total_output_tokens") or 0
        thoughts = usage.get("total_thought_tokens") or 0
        modalities = usage.get("output_tokens_by_modality") or []
        video = sum(item["tokens"] for item in modalities
                    if isinstance(item, dict) and item.get("modality") == "video"
                    and isinstance(item.get("tokens"), int))
        video = video or outputs
        return {
            "totalTokens": usage["total_tokens"],
            "estimatedUsd": round((inputs * 1.5 + video * 12 + (max(0, outputs - video) + thoughts) * 9) / 1_000_000, 8),
        }
    if model == SEEDANCE_MODEL and isinstance(usage.get("completion_tokens"), int):
        total = usage["completion_tokens"]
        return {"totalTokens": total, "estimatedUsd": round(total * SEEDANCE_USD_PER_MILLION_TOKENS / 1_000_000, 8)}
    return None


def task_connection(task_id, model, key):
    if task_id.startswith("cgt-"):
        legacy_key = key if model == SEEDANCE_MODEL else settings().get("seedance_api_key")
        return (ARK, {"Authorization": f"Bearer {legacy_key}"}) if legacy_key else (None, {})
    gemini_key = key if model == GEMINI_MODEL else settings().get("gemini_api_key")
    return (INTERACTIONS, gemini_headers(gemini_key)) if gemini_key else (None, {})


async def backfill_clip_usage(db, client, row, model, key, project_id, revision):
    endpoint, headers = task_connection(row["task_id"], model, key)
    if not endpoint:
        return
    db.execute(
        "UPDATE production_clips SET usage_checked_at = now() WHERE project_id = %s AND revision = %s AND scene = %s",
        (project_id, revision, row["scene"]),
    )
    try:
        response = await client.get(endpoint + "/" + row["task_id"], headers=headers)
        response.raise_for_status()
        usage = generation_usage(response.json())
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return
    if usage:
        db.execute(
            "UPDATE production_clips SET asset_usage = %s WHERE project_id = %s AND revision = %s AND scene = %s",
            (Jsonb(usage), project_id, revision, row["scene"]),
        )


def needs_usage(row):
    return (row["status"] == "succeeded" and row["asset_id"] and row["task_id"]
            and row["asset_usage"] is None and row["can_check_usage"])


async def cancel_active_clips(db, client, model, key, project_id, revision):
    if not stop_requested(db, project_id, revision):
        return
    for row in clip_rows(db, project_id, revision):
        task_id = row["task_id"]
        if row["status"] not in ACTIVE or not task_id or row["cancel_attempted"]:
            continue
        endpoint, headers = task_connection(task_id, model, key)
        # ModelArk's DELETE can remove a completed task while its status is changing.
        if endpoint != INTERACTIONS:
            continue
        try:
            response = await client.post(f"{endpoint}/{task_id}/cancel", headers=headers)
            cancelled = response.is_success and response.json().get("status") == "cancelled"
        except (httpx.RequestError, ValueError, AttributeError):
            continue
        db.execute(
            """UPDATE production_clips SET cancel_attempted = %s, status = %s, updated_at = now()
               WHERE project_id = %s AND revision = %s AND scene = %s""",
            (response.status_code < 500 and response.status_code != 429,
             "cancelled" if cancelled else row["status"], project_id, revision, row["scene"]),
        )


@router.post("/api/projects/{project_id}/production/stop")
async def stop_production(project_id: UUID, request: ExportRequest):
    with connection(autocommit=True) as db:
        load_draft(db, project_id, request.revision)
        with db.transaction():
            db.execute(
                "INSERT INTO production_stops VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (project_id, request.revision),
            )
            db.execute(
                """UPDATE production_sequences SET status = 'stopped', error = NULL
                   WHERE project_id = %s AND revision = %s AND status = 'running'""",
                (project_id, request.revision),
            )
        if lock_project(db, project_id):
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as client:
                await cancel_active_clips(
                    db, client, production_model(), provider_key(required=False), project_id, request.revision,
                )
        return state(db, project_id, request.revision)


async def reconcile(db, client, model, key, project_id, revision):
    for row in clip_rows(db, project_id, revision):
        if needs_usage(row):
            await backfill_clip_usage(db, client, row, model, key, project_id, revision)
        if row["status"] not in ACTIVE:
            continue
        status, message, asset = row["status"], None, None
        task = None
        if status == "submitting":
            if not row["submission_stale"]:
                continue
            status, message = "unknown", "서버 중단으로 공급사 접수 여부를 확인하지 못했습니다."
        elif (row["task_id"] or "").startswith("cgt-"):
            legacy_key = key if model == SEEDANCE_MODEL else settings().get("seedance_api_key")
            if not legacy_key:
                status, message = "failed", "이전 Seedance 작업의 조회 키가 없습니다. Gemini로 다시 생성할 수 있습니다."
            else:
                try:
                    response = await client.get(ARK + "/" + row["task_id"], headers={"Authorization": f"Bearer {legacy_key}"})
                    response.raise_for_status()
                    task = response.json()
                    upstream = task.get("status")
                    if upstream == "succeeded":
                        asset = await download_clip(client, (task.get("content") or {}).get("video_url"))
                        status = "succeeded"
                    elif upstream == "cancelled":
                        status = "cancelled"
                    elif upstream in {"failed", "expired"}:
                        status, message = "failed", "이전 Seedance 작업이 완료되지 않았습니다."
                    elif upstream in {"queued", "running"}:
                        status = upstream
                    else:
                        continue
                except httpx.HTTPStatusError as error:
                    if error.response.status_code not in (401, 403, 404, 410):
                        continue
                    status, message = "failed", "이전 Seedance 작업 또는 영상 주소를 사용할 수 없습니다."
                except (ValueError, TypeError, AttributeError):
                    status, message = "failed", "이전 Seedance 영상의 형식을 확인하지 못했습니다."
                except (httpx.RequestError, OSError):
                    continue
        else:
            gemini_key = key if model == GEMINI_MODEL else settings().get("gemini_api_key")
            if not gemini_key:
                raise HTTPException(503, "Gemini 영상 작업을 확인하려면 video_production에 해당 키를 설정해 주세요.")
            try:
                response = await client.get(INTERACTIONS + "/" + row["task_id"], headers=gemini_headers(gemini_key))
                response.raise_for_status()
                task = response.json()
                upstream = task.get("status")
                if upstream == "completed":
                    asset = await asyncio.to_thread(save_omni_clip, task)
                    status = "succeeded"
                elif upstream == "cancelled":
                    status = "cancelled"
                elif upstream in {"failed", "expired", "incomplete", "requires_action"}:
                    status, message = "failed", "Gemini 영상 생성이 완료되지 않았습니다."
                elif upstream in {"in_progress", "running", "pending", "queued"}:
                    status = "queued" if upstream in {"pending", "queued"} else "running"
                else:
                    continue
            except httpx.HTTPStatusError as error:
                if error.response.status_code in (401, 403):
                    raise HTTPException(503, "Gemini 프로젝트의 키와 작업 조회 권한을 확인해 주세요.") from None
                if error.response.status_code not in (404, 410):
                    continue
                status, message = "failed", "Gemini 작업을 찾을 수 없거나 보관 기간이 지났습니다."
            except (ValueError, TypeError, AttributeError):
                status, message = "failed", "Gemini 응답에서 유효한 MP4를 확인하지 못했습니다."
            except (httpx.RequestError, OSError):
                continue
        usage = generation_usage(task) if asset is not None else None
        with db.transaction():
            db.execute(
                """UPDATE production_clips SET status = %s, error = %s, asset_id = coalesce(%s, asset_id),
                       asset_character_version = CASE WHEN %s THEN character_version ELSE asset_character_version END,
                       asset_usage = CASE WHEN %s THEN %s ELSE asset_usage END,
                       updated_at = now() WHERE project_id = %s AND revision = %s AND scene = %s""",
                (status, message, asset, asset is not None, asset is not None, Jsonb(usage) if usage else None,
                 project_id, revision, row["scene"]),
            )
            if asset:
                db.execute(
                    "DELETE FROM production_exports WHERE project_id = %s AND revision = %s",
                    (project_id, revision),
                )


@router.get("/api/projects/{project_id}/production")
async def production_state(project_id: UUID, revision: UUID, response: Response):
    response.headers["Cache-Control"] = "no-store"
    with connection(autocommit=True) as db:
        load_draft(db, project_id, revision)
        rows = clip_rows(db, project_id, revision)
        if any(row["status"] in ACTIVE or needs_usage(row) for row in rows) and lock_project(db, project_id):
            key = provider_key(required=False)
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as client:
                await cancel_active_clips(db, client, production_model(), key, project_id, revision)
                await reconcile(db, client, production_model(), key, project_id, revision)
    await advance_sequence(project_id, revision)
    with connection() as db:
        load_draft(db, project_id, revision)
        return state(db, project_id, revision)


def run_media(args, cwd):
    return subprocess.run(
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True, timeout=180, text=True, encoding="utf-8", errors="replace",
    ).stdout


def assemble(paths, durations, destination, ffmpeg, ffprobe):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as directory:
        files = []
        for index, (path, seconds) in enumerate(zip(paths, durations, strict=True)):
            probe = run_media([ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path.resolve())], directory)
            audio = any(stream.get("codec_type") == "audio" for stream in json.loads(probe)["streams"])
            inputs = ["-i", str(path.resolve())]
            if not audio:
                inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            name = f"scene-{index}.mp4"
            run_media([
                ffmpeg, "-nostdin", "-v", "error", *inputs, "-map", "0:v:0", "-map", "0:a:0" if audio else "1:a:0",
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,tpad=stop_mode=clone:stop_duration=12",
                "-af", "apad", "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-preset", "fast", "-c:a", "aac", "-ar", "48000", "-ac", "2", name,
            ], directory)
            files.append(name)
        work = Path(directory)
        (work / "clips.txt").write_text("".join(f"file '{name}'\n" for name in files), encoding="utf-8")
        run_media([ffmpeg, "-nostdin", "-v", "error", "-f", "concat", "-safe", "1", "-i", "clips.txt",
                   "-c", "copy", "-movflags", "+faststart", "final.mp4"], directory)
        output = json.loads(run_media([
            ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json", "final.mp4",
        ], directory))
        if abs(float(output["format"]["duration"]) - sum(durations)) > 0.25 or not any(
            stream.get("width") == 1080 and stream.get("height") == 1920 for stream in output["streams"]
        ):
            raise ValueError("final media validation failed")
        (work / "final.mp4").replace(destination)


@router.post("/api/projects/{project_id}/production/export")
async def export_video(project_id: UUID, request: ExportRequest):
    with connection(autocommit=True) as db:
        if not lock_project(db, project_id):
            raise HTTPException(409, "다른 제작 작업을 처리 중입니다.")
        draft, _ = load_draft(db, project_id, request.revision)
        rows = clip_rows(db, project_id, request.revision)
        character = db.execute(
            "SELECT version FROM production_characters WHERE project_id = %s", (project_id,),
        ).fetchone()
        version = character["version"] if character else None
        if any(row["asset_id"] and row["asset_character_version"] != version for row in rows):
            raise HTTPException(409, "이전 기준으로 만든 클립을 현재 캐릭터 이미지로 다시 생성해 주세요.")
        if len(rows) != len(draft["scenes"]) or any(not row["asset_id"] or row["status"] in ACTIVE for row in rows):
            raise HTTPException(409, "모든 장면의 클립을 완료한 뒤 조립해 주세요.")
        paths = [asset_path(row["asset_id"]) for row in rows]
        missing_scenes = [str(row["scene"] + 1) for row, path in zip(rows, paths) if not path.is_file()]
        if missing_scenes:
            raise HTTPException(
                409, "저장소에서 클립을 찾을 수 없습니다. 해당 장면을 다시 생성해 주세요. 장면 " + ", ".join(missing_scenes),
            )
        video_settings = settings()
        tools = {}
        for name in ("ffmpeg", "ffprobe"):
            configured = video_settings.get(name, name)
            executable = which(configured) if isinstance(configured, str) and configured else None
            if executable is None:
                raise HTTPException(
                    503, f"{name} 실행 파일을 찾을 수 없습니다. FastAPI의 PATH 또는 config.yaml의 video_production.{name}을 확인해 주세요.",
                )
            tools[name] = str(Path(executable).resolve())
        asset = uuid4()
        try:
            await asyncio.to_thread(
                assemble, paths, draft["durations"], asset_path(asset), tools["ffmpeg"], tools["ffprobe"],
            )
        except FileNotFoundError:
            raise HTTPException(503, "조립 도중 실행 파일이나 영상 저장 경로를 열 수 없었습니다.") from None
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            raise HTTPException(502, "영상 조립에 실패했습니다. 클립과 FFmpeg 설정을 확인해 주세요.") from None
        db.execute(
            """INSERT INTO production_exports (project_id, revision, asset_id, created_at) VALUES (%s, %s, %s, now())
               ON CONFLICT (project_id, revision) DO UPDATE SET asset_id = excluded.asset_id,
                                                               created_at = excluded.created_at""",
            (project_id, request.revision, asset),
        )
        return state(db, project_id, request.revision)


@router.get("/api/projects/{project_id}/production/media/{asset_id}")
def serve_media(project_id: UUID, asset_id: UUID, download: bool = False):
    with connection() as db:
        match = db.execute(
            """SELECT 1 FROM production_clips WHERE project_id = %(project)s AND asset_id = %(asset)s
               UNION ALL
               SELECT 1 FROM production_exports WHERE project_id = %(project)s AND asset_id = %(asset)s LIMIT 1""",
            {"project": project_id, "asset": asset_id},
        ).fetchone()
    path = asset_path(asset_id)
    if match is None or not path.is_file():
        raise HTTPException(404, "영상 파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type="video/mp4", filename="shortform.mp4" if download else None)
