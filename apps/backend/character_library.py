import base64
import re
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from collection import connection
from config import CONFIG_PATH


router = APIRouter()
MAX_BYTES = 4 * 1024 * 1024
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"
ACTIVE = ("submitting", "queued", "running")


class CharacterPrompt(BaseModel):
    role: str = Field(min_length=1, max_length=100)
    age: str = Field(min_length=1, max_length=30)
    staging: str = Field(default="", max_length=500)


def initialize_character_library(db):
    db.execute("""CREATE TABLE IF NOT EXISTS character_library (
        id uuid PRIMARY KEY, label text NOT NULL, mime_type text NOT NULL,
        image_bytes bytea NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    )""")
    db.execute("ALTER TABLE character_library ADD COLUMN IF NOT EXISTS deleted_at timestamptz")
    db.execute("""INSERT INTO character_library (id, label, mime_type, image_bytes)
        SELECT version, '기존 캐릭터', mime_type, image_bytes FROM production_characters
        ON CONFLICT DO NOTHING""")


def select_character(db, project_id, character_id):
    if character_id is None:
        return
    current = db.execute(
        "SELECT version FROM production_characters WHERE project_id = %s", (project_id,),
    ).fetchone()
    if current and current["version"] == character_id:
        return
    lock = db.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext('production'), hashtext(%s)) AS acquired",
        (str(project_id),),
    ).fetchone()
    if not lock["acquired"]:
        raise HTTPException(409, "영상 제작 작업이 끝난 뒤 캐릭터를 변경해 주세요.")
    active = db.execute(
        """SELECT 1 FROM project_storyboard b
           LEFT JOIN production_sequences q ON q.project_id = b.project_id AND q.revision::text = (b.draft->>'revision')
           WHERE b.project_id = %(project)s AND (q.status = 'running' OR EXISTS (
               SELECT 1 FROM production_clips c WHERE c.project_id = b.project_id
               AND c.revision::text = (b.draft->>'revision') AND c.status = ANY(%(active)s)
           ))""", {"project": project_id, "active": list(ACTIVE)},
    ).fetchone()
    if active:
        raise HTTPException(409, "클립 생성이 끝난 뒤 캐릭터를 변경해 주세요.")
    row = db.execute(
        """INSERT INTO production_characters (project_id, version, mime_type, image_bytes)
           SELECT %s, id, mime_type, image_bytes FROM character_library WHERE id = %s AND deleted_at IS NULL
           ON CONFLICT (project_id) DO UPDATE SET version = excluded.version,
               mime_type = excluded.mime_type, image_bytes = excluded.image_bytes
           RETURNING version""", (project_id, character_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "선택한 캐릭터 이미지를 찾을 수 없습니다.")


def validate_image(mime, data):
    if len(data) > MAX_BYTES:
        raise ValueError("Image exceeds size limit")
    if mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return
    if mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff"):
        return
    raise ValueError("Invalid image format")


def store_image(label, mime, data):
    key = uuid4()
    with connection() as db:
        db.execute(
            "INSERT INTO character_library (id, label, mime_type, image_bytes) VALUES (%s, %s, %s, %s)",
            (key, label, mime, data),
        )
    return {"id": key, "label": label}


@router.get("/api/characters")
def list_characters(offset: int = Query(default=0, ge=0, le=1_000_000)):
    with connection() as db:
        rows = db.execute(
            """SELECT id, label FROM character_library WHERE deleted_at IS NULL
               ORDER BY created_at DESC, id DESC LIMIT 25 OFFSET %s""",
            (offset,),
        ).fetchall()
    return {"items": rows[:24], "nextOffset": offset + 24 if len(rows) > 24 else None}


@router.delete("/api/characters/{character_id}", status_code=204)
def delete_character(character_id: UUID):
    with connection() as db:
        row = db.execute(
            """UPDATE character_library SET deleted_at = coalesce(deleted_at, now())
               WHERE id = %s RETURNING id""", (character_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "캐릭터를 찾을 수 없습니다.")
    return Response(status_code=204)


@router.get("/api/characters/{character_id}/image")
def character_image(character_id: UUID):
    with connection() as db:
        row = db.execute("SELECT mime_type, image_bytes FROM character_library WHERE id = %s",
                         (character_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "캐릭터를 찾을 수 없습니다.")
    return Response(bytes(row["image_bytes"]), media_type=row["mime_type"], headers={
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600",
    })


@router.post("/api/characters/upload", status_code=201)
async def upload_character_library(request: Request, name: str = Query(default="", max_length=100)):
    mime = request.headers.get("content-type", "").partition(";")[0].lower().strip()
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "4 MiB 이하의 이미지를 선택해 주세요.")
        data.extend(chunk)
    try:
        validate_image(mime, data)
    except ValueError:
        raise HTTPException(422, "JPG 또는 PNG 이미지를 선택해 주세요.") from None
    return store_image(Path(name).stem.strip() or "등록한 캐릭터", mime, bytes(data))


@router.post("/api/characters/generate", status_code=201)
async def generate_character(prompt: CharacterPrompt):
    if not prompt.role.strip() or not prompt.age.strip():
        raise HTTPException(422, "캐릭터 역할과 나이를 입력해 주세요.")
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    section = config.get("character_image") or {}
    if not isinstance(section, dict):
        raise HTTPException(503, "config.yaml의 character_image 설정을 확인해 주세요.")
    production = config.get("video_production") or {}
    key = section.get("api_key") or (
        production.get("api_key") if production.get("model", "gemini-omni-1.2-flash") == "gemini-omni-1.2-flash" else None
    )
    model = section.get("model", DEFAULT_IMAGE_MODEL)
    if not isinstance(key, str) or not key.strip() or not isinstance(model, str) or not re.fullmatch(r"gemini-[a-z0-9.-]+", model):
        raise HTTPException(503, "캐릭터 이미지 생성에 사용할 Gemini 모델과 API 키를 확인해 주세요.")
    instruction = (
        f"세로 9:16 영상용 오리지널 성인 캐릭터 기준 사진 한 장. 역할: {prompt.role}. 연령대: {prompt.age}. "
        f"연출: {prompt.staging}. 정면에서 얼굴과 의상 상반신이 선명하게 보이게 한다. "
        "실존 유명인을 재현하지 않고, 글자, 자막, 로고, 버튼, 화면 UI를 넣지 않는다."
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(65, connect=8)) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key.strip()},
                json={"contents": [{"parts": [{"text": instruction}]}], "generationConfig": {
                    "responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "9:16", "imageSize": "1K"},
                }},
            )
        response.raise_for_status()
        parts = response.json()["candidates"][0]["content"]["parts"]
        result = next(part["inlineData"] for part in parts if "inlineData" in part and not part.get("thought"))
        encoded = result["data"]
        if len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
            raise ValueError("Image exceeds size limit")
        data = base64.b64decode(encoded, validate=True)
        mime = result["mimeType"]
        validate_image(mime, data)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 429:
            raise HTTPException(429, "캐릭터 이미지 생성 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.") from None
        raise HTTPException(502, f"캐릭터 이미지 생성 요청이 실패했습니다. (HTTP {error.response.status_code})") from None
    except (httpx.RequestError, KeyError, IndexError, StopIteration, ValueError, TypeError):
        raise HTTPException(502, "캐릭터 이미지를 생성하지 못했습니다. 다시 시도해 주세요.") from None
    return store_image(f"{prompt.age} {prompt.role}", mime, data)
