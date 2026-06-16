from __future__ import annotations

import json
import mimetypes
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import FileResponse, PlainTextResponse

from album_backend import db, schemas, services
from utils import get_credits


router = APIRouter(prefix="/api/v1", tags=["album-production"])


def data(value: Any) -> dict[str, Any]:
    return {"data": value}


def require(table: str, row_id: str, label: str) -> dict[str, Any]:
    value = db.get_one(table, row_id)
    if not value:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return value


def dump(model: Any, *, exclude_none: bool = False) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=exclude_none)
    return model.dict(exclude_none=exclude_none)


@router.get("/system/video-icons")
async def get_video_icons():
    return data(services.list_video_icons())


@router.get("/system/video-icons/{filename}")
async def get_video_icon(filename: str):
    path = services.resolve_video_icon(filename)
    if not path:
        raise HTTPException(status_code=404, detail="Video icon not found")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


def album_detail(album_id: str) -> dict[str, Any]:
    album = require("albums", album_id, "Album")
    album["tracks"] = db.fetch_all(
        "SELECT * FROM tracks WHERE album_id = ? ORDER BY sequence", (album_id,)
    )
    album["assets"] = db.fetch_all(
        "SELECT * FROM assets WHERE album_id = ? ORDER BY created_at DESC", (album_id,)
    )
    return album


@router.get("/system/health")
async def health():
    db.init_db()
    return data({"status": "ok", "database": str(db.DB_PATH)})


@router.get("/system/suno-status")
async def suno_status():
    try:
        credits = await get_credits(services._suno_token())
        return data({"connected": True, **credits})
    except Exception as exc:
        return data({"connected": False, "error": str(exc)})


@router.post("/albums", status_code=status.HTTP_201_CREATED)
async def create_album(payload: schemas.AlbumCreate):
    values = dump(payload)
    now = db.now_iso()
    album = db.insert(
        "albums",
        {
            "id": db.new_id(),
            "title": values["title"],
            "artist_name": values.get("artist_name"),
            "description": values.get("description"),
            "genre": values["genre"],
            "vocal_style": values["vocal_style"],
            "tempo": values["tempo"],
            "lyrics_language": values["lyrics_language"],
            "mood": values["mood"],
            "instruments_json": db.encode_json(values["instruments"]),
            "keywords": values["keywords"],
            "additional_instructions": values["additional_instructions"],
            "style_prompt": "",
            "visual_concept": values.get("visual_concept", ""),
            "thumbnail_image_prompt": values.get("thumbnail_image_prompt", ""),
            "track_count": values["track_count"],
            "status": "draft",
            "selected_cover_asset_id": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    return data(album)


@router.get("/albums")
async def list_albums(
    album_status: str | None = None,
    search: str | None = None,
    limit: int = 50,
):
    clauses = []
    params: list[Any] = []
    if album_status:
        clauses.append("status = ?")
        params.append(album_status)
    if search:
        clauses.append("(title LIKE ? OR artist_name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.fetch_all(
        f"SELECT * FROM albums {where} ORDER BY updated_at DESC LIMIT ?",
        (*params, max(1, min(limit, 200))),
    )
    return {"data": rows, "pagination": {"next_cursor": None, "has_more": False}}


@router.get("/albums/{album_id}")
async def get_album(album_id: str):
    return data(album_detail(album_id))


@router.patch("/albums/{album_id}")
async def update_album(album_id: str, payload: schemas.AlbumUpdate):
    require("albums", album_id, "Album")
    values = dump(payload, exclude_none=True)
    if "instruments" in values:
        values["instruments_json"] = db.encode_json(values.pop("instruments"))
    values["updated_at"] = db.now_iso()
    return data(db.update("albums", album_id, values))


@router.delete("/albums/{album_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_album(album_id: str):
    require("albums", album_id, "Album")
    db.delete("albums", album_id)


@router.post("/albums/{album_id}/plan", status_code=status.HTTP_202_ACCEPTED)
async def plan_album(album_id: str, background_tasks: BackgroundTasks):
    require("albums", album_id, "Album")
    job = services.create_job("album_plan", "album", album_id)
    background_tasks.add_task(services.run_album_plan, job["id"], album_id)
    return data({"job_id": job["id"], "status": job["status"]})


@router.get("/albums/{album_id}/tracks")
async def list_tracks(album_id: str):
    require("albums", album_id, "Album")
    tracks = db.fetch_all(
        "SELECT * FROM tracks WHERE album_id = ? ORDER BY sequence", (album_id,)
    )
    for track in tracks:
        track["selected_generations"] = db.fetch_all(
            """
            SELECT * FROM generations
             WHERE track_id = ? AND is_selected = 1
             ORDER BY created_at
            """,
            (track["id"],),
        )
    return data(tracks)


@router.post(
    "/albums/{album_id}/tracks", status_code=status.HTTP_201_CREATED
)
async def create_track(album_id: str, payload: schemas.TrackCreate):
    require("albums", album_id, "Album")
    values = dump(payload)
    now = db.now_iso()
    track = db.insert(
        "tracks",
        {
            "id": db.new_id(),
            "album_id": album_id,
            **values,
            "instrumental": int(values["instrumental"]),
            "status": "lyrics_ready" if values["lyrics"] else "draft",
            "selected_generation_id": None,
            "created_at": now,
            "updated_at": now,
        },
    )
    return data(track)


@router.get("/tracks/{track_id}")
async def get_track(track_id: str):
    track = require("tracks", track_id, "Track")
    track["generations"] = db.fetch_all(
        "SELECT * FROM generations WHERE track_id = ? ORDER BY created_at DESC",
        (track_id,),
    )
    return data(track)


@router.patch("/tracks/{track_id}")
async def update_track(track_id: str, payload: schemas.TrackUpdate):
    require("tracks", track_id, "Track")
    values = dump(payload, exclude_none=True)
    if "instrumental" in values:
        values["instrumental"] = int(values["instrumental"])
    values["updated_at"] = db.now_iso()
    return data(db.update("tracks", track_id, values))


@router.delete("/tracks/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_track(track_id: str):
    require("tracks", track_id, "Track")
    db.delete("tracks", track_id)


@router.put("/tracks/{track_id}/lyrics")
async def save_lyrics(track_id: str, payload: schemas.LyricsUpdate):
    require("tracks", track_id, "Track")
    return data(
        db.update(
            "tracks",
            track_id,
            {
                "lyrics": payload.lyrics,
                "status": "lyrics_ready",
                "updated_at": db.now_iso(),
            },
        )
    )


@router.put("/tracks/{track_id}/style")
async def save_style(track_id: str, payload: schemas.StyleUpdate):
    require("tracks", track_id, "Track")
    return data(
        db.update(
            "tracks",
            track_id,
            {"style_prompt": payload.style_prompt, "updated_at": db.now_iso()},
        )
    )


@router.post(
    "/tracks/{track_id}/lyrics/generate", status_code=status.HTTP_202_ACCEPTED
)
async def generate_track_lyrics(
    track_id: str,
    background_tasks: BackgroundTasks,
    payload: schemas.RegenerateLyricsRequest | None = None,
):
    require("tracks", track_id, "Track")
    request = payload or schemas.RegenerateLyricsRequest(regenerate_style=True)
    job = services.create_job(
        "lyrics_generate", "track", track_id, dump(request)
    )
    background_tasks.add_task(
        services.run_lyrics_generation,
        job["id"],
        track_id,
        request.instruction,
        request.regenerate_style,
    )
    return data({"job_id": job["id"], "status": job["status"]})


@router.post(
    "/tracks/{track_id}/lyrics/regenerate", status_code=status.HTTP_202_ACCEPTED
)
async def regenerate_track_lyrics(
    track_id: str,
    payload: schemas.RegenerateLyricsRequest,
    background_tasks: BackgroundTasks,
):
    return await generate_track_lyrics(track_id, background_tasks, payload)


@router.get("/tracks/{track_id}/lyrics/download")
async def download_lyrics(track_id: str):
    track = require("tracks", track_id, "Track")
    return PlainTextResponse(
        track["lyrics"],
        headers={
            "Content-Disposition": f'attachment; filename="{track_id}.txt"'
        },
    )


@router.post(
    "/tracks/{track_id}/generate", status_code=status.HTTP_202_ACCEPTED
)
async def generate_track(
    track_id: str,
    payload: schemas.GenerateTrackRequest,
    background_tasks: BackgroundTasks,
):
    require("tracks", track_id, "Track")
    running = db.fetch_one(
        """
        SELECT * FROM jobs
         WHERE resource_type = 'track' AND resource_id = ?
           AND type = 'track_generate' AND status IN ('pending', 'running')
         ORDER BY created_at DESC LIMIT 1
        """,
        (track_id,),
    )
    if running:
        return data({"job_id": running["id"], "status": running["status"]})
    values = dump(payload)
    job = services.create_job("track_generate", "track", track_id, values)
    background_tasks.add_task(
        services.run_track_generation,
        job["id"],
        track_id,
        payload.mode,
        payload.download_audio,
        payload.timeout_seconds,
        payload.poll_interval_seconds,
    )
    return data({"job_id": job["id"], "status": job["status"]})


async def _generate_album_tracks(
    album_job_id: str,
    track_ids: list[str],
    download_audio: bool,
) -> None:
    try:
        services.set_job_running(album_job_id)
        completed = []
        for index, track_id in enumerate(track_ids):
            child = services.create_job(
                "track_generate",
                "track",
                track_id,
                {"mode": "custom", "download_audio": download_audio},
            )
            await services.run_track_generation(
                child["id"], track_id, "custom", download_audio, 600, 10
            )
            child_result = db.get_one("jobs", child["id"])
            if child_result and child_result["status"] == "failed":
                raise RuntimeError(
                    child_result.get("error_message") or "Track generation failed"
                )
            completed.append(track_id)
            services.set_job_progress(
                album_job_id, int(100 * len(completed) / len(track_ids))
            )
        services.set_job_succeeded(album_job_id, {"track_ids": completed})
    except Exception as exc:
        services.set_job_failed(album_job_id, exc, "ALBUM_GENERATION_FAILED")


@router.post(
    "/albums/{album_id}/generate", status_code=status.HTTP_202_ACCEPTED
)
async def generate_album(
    album_id: str,
    payload: schemas.GenerateAlbumRequest,
    background_tasks: BackgroundTasks,
):
    require("albums", album_id, "Album")
    if payload.track_ids:
        track_ids = payload.track_ids
    else:
        track_ids = [
            row["id"]
            for row in db.fetch_all(
                "SELECT id FROM tracks WHERE album_id = ? ORDER BY sequence",
                (album_id,),
            )
        ]
    if not track_ids:
        raise HTTPException(status_code=409, detail="Album has no tracks")
    job = services.create_job(
        "album_generate",
        "album",
        album_id,
        {"track_ids": track_ids, "download_audio": payload.download_audio},
    )
    background_tasks.add_task(
        _generate_album_tracks, job["id"], track_ids, payload.download_audio
    )
    return data({"job_id": job["id"], "status": job["status"]})


@router.get("/tracks/{track_id}/generations")
async def list_generations(track_id: str):
    require("tracks", track_id, "Track")
    return data(
        db.fetch_all(
            "SELECT * FROM generations WHERE track_id = ? ORDER BY created_at DESC",
            (track_id,),
        )
    )


@router.post(
    "/tracks/{track_id}/generations/{generation_id}/select"
)
async def select_generation(track_id: str, generation_id: str):
    require("tracks", track_id, "Track")
    generation = require("generations", generation_id, "Generation")
    if generation["track_id"] != track_id:
        raise HTTPException(status_code=409, detail="Generation belongs to another track")
    is_selected = not generation["is_selected"]
    db.update("generations", generation_id, {"is_selected": int(is_selected)})
    selected_generation_id = generation_id if is_selected else None
    if not is_selected:
        remaining = db.fetch_one(
            """
            SELECT * FROM generations
             WHERE track_id = ? AND is_selected = 1
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (track_id,),
        )
        selected_generation_id = remaining["id"] if remaining else None
    db.update(
        "tracks",
        track_id,
        {
            "selected_generation_id": selected_generation_id,
            "updated_at": db.now_iso(),
        },
    )
    return data(db.get_one("generations", generation_id))


@router.patch("/generations/{generation_id}")
async def update_generation(
    generation_id: str,
    payload: schemas.GenerationUpdate,
):
    require("generations", generation_id, "Generation")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Generation title is required")
    return data(
        db.update(
            "generations",
            generation_id,
            {"title": title},
        )
    )


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    return data(require("jobs", job_id, "Job"))


@router.get("/jobs")
async def list_jobs(
    resource_type: str | None = None,
    resource_id: str | None = None,
    job_status: str | None = None,
):
    clauses = []
    params: list[Any] = []
    for column, value in (
        ("resource_type", resource_type),
        ("resource_id", resource_id),
        ("status", job_status),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return data(
        db.fetch_all(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT 200",
            tuple(params),
        )
    )


@router.get("/albums/{album_id}/video-templates")
async def list_video_templates(album_id: str):
    require("albums", album_id, "Album")
    return data(
        db.fetch_all(
            """
            SELECT * FROM video_templates
             ORDER BY created_at DESC
            """
        )
    )


@router.post(
    "/albums/{album_id}/video-templates",
    status_code=status.HTTP_201_CREATED,
)
async def create_video_template(
    album_id: str,
    payload: schemas.VideoTemplateCreate,
):
    require("albums", album_id, "Album")
    if payload.preview_asset_id:
        preview = require("assets", payload.preview_asset_id, "Preview asset")
        if preview["album_id"] != album_id or preview["type"] != "template_preview":
            raise HTTPException(status_code=409, detail="Invalid template preview asset")
    now = db.now_iso()
    template = db.insert(
        "video_templates",
        {
            "id": db.new_id(),
            "album_id": album_id,
            "name": payload.name.strip(),
            "compose_json": db.encode_json(dump(payload.compose)),
            "image_instruction": payload.image_instruction.strip(),
            "title_source": payload.title_source,
            "artist_source": payload.artist_source,
            "preview_asset_id": payload.preview_asset_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    return data(template)


@router.patch("/video-templates/{template_id}")
async def update_video_template(
    template_id: str,
    payload: schemas.VideoTemplateUpdate,
):
    template = require("video_templates", template_id, "Video template")
    values = dump(payload, exclude_none=True)
    if "preview_asset_id" in values:
        preview = require("assets", values["preview_asset_id"], "Preview asset")
        if preview["type"] != "template_preview":
            raise HTTPException(status_code=409, detail="Invalid template preview asset")
    if "compose" in values:
        values["compose_json"] = db.encode_json(values.pop("compose"))
    if "name" in values:
        values["name"] = values["name"].strip()
    if "image_instruction" in values:
        values["image_instruction"] = values["image_instruction"].strip()
    values["updated_at"] = db.now_iso()
    return data(db.update("video_templates", template_id, values))


@router.post(
    "/albums/{album_id}/template-previews/generate",
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_template_previews(
    album_id: str,
    payload: schemas.ImageGenerateRequest,
    background_tasks: BackgroundTasks,
):
    require("albums", album_id, "Album")
    values = dump(payload)
    job = services.create_job("template_preview_generate", "album", album_id, values)
    background_tasks.add_task(
        services.run_image_generation,
        job["id"],
        album_id,
        None,
        payload.instruction,
        payload.aspect_ratio,
        payload.candidate_count,
        "template_preview",
    )
    return data({"job_id": job["id"], "status": job["status"]})


@router.post(
    "/albums/{album_id}/template-previews/upload",
    status_code=status.HTTP_201_CREATED,
)
async def upload_template_preview(
    album_id: str,
    request: Request,
    filename: str = "template-preview.png",
):
    require("albums", album_id, "Album")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Request body is empty")
    if len(body) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image exceeds 20 MB")
    return data(
        services.save_uploaded_asset(
            album_id,
            body,
            filename,
            request.headers.get("content-type", "application/octet-stream"),
            "template_preview",
        )
    )


@router.get("/albums/{album_id}/template-previews")
async def list_template_previews(album_id: str):
    require("albums", album_id, "Album")
    return data(
        db.fetch_all(
            """
            SELECT * FROM assets
             WHERE album_id = ? AND type = 'template_preview'
             ORDER BY created_at DESC
            """,
            (album_id,),
        )
    )


@router.delete(
    "/video-templates/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_video_template(template_id: str):
    require("video_templates", template_id, "Video template")
    db.delete("video_templates", template_id)


@router.get("/albums/{album_id}/video-template-assignments")
async def list_video_template_assignments(album_id: str):
    require("albums", album_id, "Album")
    rows = db.fetch_all(
        """
        SELECT a.* FROM track_video_templates a
        JOIN tracks t ON t.id = a.track_id
        WHERE t.album_id = ?
        """,
        (album_id,),
    )
    return data({row["track_id"]: row["template_id"] for row in rows})


@router.put("/tracks/{track_id}/video-template")
async def set_track_video_template(
    track_id: str,
    payload: schemas.TrackVideoTemplateUpdate,
):
    require("tracks", track_id, "Track")
    require("video_templates", payload.template_id, "Video template")
    existing = db.fetch_one(
        "SELECT * FROM track_video_templates WHERE track_id = ?",
        (track_id,),
    )
    values = {
        "track_id": track_id,
        "template_id": payload.template_id,
        "updated_at": db.now_iso(),
    }
    if existing:
        db.execute(
            """
            UPDATE track_video_templates
               SET template_id = ?, updated_at = ?
             WHERE track_id = ?
            """,
            (payload.template_id, values["updated_at"], track_id),
        )
    else:
        db.execute(
            """
            INSERT INTO track_video_templates (track_id, template_id, updated_at)
            VALUES (?, ?, ?)
            """,
            (track_id, payload.template_id, values["updated_at"]),
        )
    return data(values)


@router.post(
    "/albums/{album_id}/covers/generate", status_code=status.HTTP_202_ACCEPTED
)
async def generate_images(
    album_id: str,
    payload: schemas.ImageGenerateRequest,
    background_tasks: BackgroundTasks,
):
    require("albums", album_id, "Album")
    if payload.track_id:
        track = require("tracks", payload.track_id, "Track")
        if track["album_id"] != album_id:
            raise HTTPException(status_code=409, detail="Track belongs to another album")
    values = dump(payload)
    job = services.create_job("cover_generate", "album", album_id, values)
    background_tasks.add_task(
        services.run_image_generation,
        job["id"],
        album_id,
        payload.track_id,
        payload.instruction,
        payload.aspect_ratio,
        payload.candidate_count,
    )
    return data({"job_id": job["id"], "status": job["status"]})


@router.post(
    "/albums/{album_id}/covers/upload", status_code=status.HTTP_201_CREATED
)
async def upload_image(
    album_id: str,
    request: Request,
    filename: str = "upload.png",
):
    require("albums", album_id, "Album")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Request body is empty")
    if len(body) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image exceeds 20 MB")
    asset = services.save_uploaded_asset(
        album_id,
        body,
        filename,
        request.headers.get("content-type", "application/octet-stream"),
    )
    return data(asset)


@router.get("/albums/{album_id}/covers")
async def list_images(album_id: str):
    require("albums", album_id, "Album")
    return data(
        db.fetch_all(
            """
            SELECT * FROM assets
             WHERE album_id = ? AND type IN ('cover', 'composed_image')
             ORDER BY created_at DESC
            """,
            (album_id,),
        )
    )


@router.post(
    "/albums/{album_id}/thumbnail-backgrounds/generate",
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_thumbnail_backgrounds(
    album_id: str,
    payload: schemas.ImageGenerateRequest,
    background_tasks: BackgroundTasks,
):
    require("albums", album_id, "Album")
    values = dump(payload)
    job = services.create_job("thumbnail_background_generate", "album", album_id, values)
    background_tasks.add_task(
        services.run_image_generation,
        job["id"],
        album_id,
        payload.track_id,
        payload.instruction,
        payload.aspect_ratio,
        payload.candidate_count,
        "thumbnail_background",
    )
    return data({"job_id": job["id"], "status": job["status"]})


@router.post(
    "/albums/{album_id}/thumbnail-backgrounds/upload",
    status_code=status.HTTP_201_CREATED,
)
async def upload_thumbnail_background(
    album_id: str,
    request: Request,
    filename: str = "thumbnail-background.png",
):
    require("albums", album_id, "Album")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Request body is empty")
    if len(body) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image exceeds 20 MB")
    return data(
        services.save_uploaded_asset(
            album_id,
            body,
            filename,
            request.headers.get("content-type", "application/octet-stream"),
            "thumbnail_background",
        )
    )


@router.get("/albums/{album_id}/thumbnail-backgrounds")
async def list_thumbnail_backgrounds(album_id: str):
    require("albums", album_id, "Album")
    return data(
        db.fetch_all(
            """
            SELECT * FROM assets
             WHERE album_id = ? AND type = 'thumbnail_background'
             ORDER BY created_at DESC
            """,
            (album_id,),
        )
    )


@router.get("/albums/{album_id}/thumbnails")
async def list_thumbnails(album_id: str):
    require("albums", album_id, "Album")
    return data(
        db.fetch_all(
            "SELECT * FROM thumbnails WHERE album_id = ? ORDER BY updated_at DESC",
            (album_id,),
        )
    )


@router.post(
    "/albums/{album_id}/thumbnail-copy/generate",
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_thumbnail_copy(
    album_id: str,
    payload: schemas.ThumbnailCopyGenerateRequest,
    background_tasks: BackgroundTasks,
):
    require("albums", album_id, "Album")
    job = services.create_job(
        "thumbnail_copy_generate",
        "album",
        album_id,
        dump(payload),
    )
    background_tasks.add_task(
        services.run_thumbnail_copy_generation,
        job["id"],
        album_id,
        payload.instruction,
    )
    return data({"job_id": job["id"], "status": job["status"]})


@router.post(
    "/albums/{album_id}/thumbnails",
    status_code=status.HTTP_201_CREATED,
)
async def create_thumbnail(album_id: str, payload: schemas.ThumbnailCreate):
    require("albums", album_id, "Album")
    if payload.background_asset_id:
        background = require("assets", payload.background_asset_id, "Background asset")
        if background["album_id"] != album_id:
            raise HTTPException(status_code=409, detail="Background belongs to another album")
    now = db.now_iso()
    return data(
        db.insert(
            "thumbnails",
            {
                "id": db.new_id(),
                "album_id": album_id,
                "name": payload.name.strip(),
                "background_asset_id": payload.background_asset_id,
                "design_json": db.encode_json(dump(payload.design)),
                "rendered_asset_id": None,
                "created_at": now,
                "updated_at": now,
            },
        )
    )


@router.patch("/thumbnails/{thumbnail_id}")
async def update_thumbnail(
    thumbnail_id: str,
    payload: schemas.ThumbnailUpdate,
):
    thumbnail = require("thumbnails", thumbnail_id, "Thumbnail")
    values = dump(payload, exclude_none=True)
    if "background_asset_id" in values:
        background = require("assets", values["background_asset_id"], "Background asset")
        if background["album_id"] != thumbnail["album_id"]:
            raise HTTPException(status_code=409, detail="Background belongs to another album")
    if "design" in values:
        values["design_json"] = db.encode_json(values.pop("design"))
    values["updated_at"] = db.now_iso()
    return data(db.update("thumbnails", thumbnail_id, values))


@router.delete("/thumbnails/{thumbnail_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thumbnail(thumbnail_id: str):
    require("thumbnails", thumbnail_id, "Thumbnail")
    db.delete("thumbnails", thumbnail_id)


@router.post("/thumbnails/{thumbnail_id}/render", status_code=status.HTTP_201_CREATED)
async def render_thumbnail(thumbnail_id: str):
    thumbnail = require("thumbnails", thumbnail_id, "Thumbnail")
    if not thumbnail.get("background_asset_id"):
        raise HTTPException(status_code=409, detail="Select a thumbnail background first")
    background = require("assets", thumbnail["background_asset_id"], "Background asset")
    source = (db.STORAGE_DIR / background["storage_key"]).resolve()
    relative = (
        Path("albums")
        / thumbnail["album_id"]
        / "thumbnails"
        / f"{thumbnail_id}-{db.new_id()}.png"
    )
    destination = db.STORAGE_DIR / relative
    services.render_thumbnail(source, destination, thumbnail["design"])
    asset = services.create_asset(
        album_id=thumbnail["album_id"],
        track_id=None,
        generation_id=None,
        asset_type="thumbnail",
        path=destination,
        original_name=f"{thumbnail['name']}.png",
        content_type="image/png",
        metadata={"thumbnail_id": thumbnail_id, "design": thumbnail["design"]},
    )
    db.update(
        "thumbnails",
        thumbnail_id,
        {"rendered_asset_id": asset["id"], "updated_at": db.now_iso()},
    )
    return data(asset)


@router.post("/albums/{album_id}/covers/{asset_id}/select")
async def select_cover(album_id: str, asset_id: str):
    require("albums", album_id, "Album")
    asset = require("assets", asset_id, "Asset")
    if asset["album_id"] != album_id:
        raise HTTPException(status_code=409, detail="Asset belongs to another album")
    db.update(
        "albums",
        album_id,
        {"selected_cover_asset_id": asset_id, "updated_at": db.now_iso()},
    )
    return data(asset)


@router.post("/albums/{album_id}/images/{asset_id}/compose")
async def compose_image(
    album_id: str,
    asset_id: str,
    payload: schemas.ImageComposeRequest,
):
    require("albums", album_id, "Album")
    asset = require("assets", asset_id, "Asset")
    if asset["album_id"] != album_id:
        raise HTTPException(status_code=409, detail="Asset belongs to another album")
    metadata = asset.get("metadata") or {}
    metadata["compose"] = dump(payload)
    updated = db.update(
        "assets",
        asset_id,
        {"metadata_json": db.encode_json(metadata)},
    )
    return data(updated)


@router.post("/albums/{album_id}/images/{asset_id}/select-for-track")
async def select_image_for_track(
    album_id: str,
    asset_id: str,
    payload: schemas.ImageSelectionRequest,
):
    require("albums", album_id, "Album")
    track = require("tracks", payload.track_id, "Track")
    generation = require("generations", payload.generation_id, "Generation")
    asset = require("assets", asset_id, "Asset")
    if track["album_id"] != album_id:
        raise HTTPException(status_code=409, detail="Track belongs to another album")
    if generation["track_id"] != payload.track_id:
        raise HTTPException(status_code=409, detail="Generation belongs to another track")
    if asset["album_id"] != album_id:
        raise HTTPException(status_code=409, detail="Asset belongs to another album")

    covers = db.fetch_all(
        "SELECT * FROM assets WHERE album_id = ? AND type = ?",
        (album_id, "cover"),
    )
    for cover in covers:
        metadata = cover.get("metadata") or {}
        if (
            metadata.get("selected_for_track_id") == payload.track_id
            and metadata.get("selected_for_generation_id") == payload.generation_id
        ):
            metadata.pop("selected_for_track_id", None)
            metadata.pop("selected_for_generation_id", None)
            db.update("assets", cover["id"], {"metadata_json": db.encode_json(metadata)})

    metadata = asset.get("metadata") or {}
    metadata["selected_for_track_id"] = payload.track_id
    metadata["selected_for_generation_id"] = payload.generation_id
    updated = db.update(
        "assets",
        asset_id,
        {"metadata_json": db.encode_json(metadata)},
    )
    return data(updated)


@router.post(
    "/albums/{album_id}/videos/render", status_code=status.HTTP_202_ACCEPTED
)
async def render_video(
    album_id: str,
    payload: schemas.VideoRenderRequest,
    background_tasks: BackgroundTasks,
):
    require("albums", album_id, "Album")
    if payload.generation_id:
        generation = require("generations", payload.generation_id, "Generation")
        if generation["track_id"] != payload.track_id:
            raise HTTPException(
                status_code=409,
                detail="Generation belongs to another track",
            )
    job = services.create_job("video_render", "album", album_id, dump(payload))
    background_tasks.add_task(
        services.run_video_render, job["id"], album_id, payload
    )
    return data({"job_id": job["id"], "status": job["status"]})


@router.post(
    "/albums/{album_id}/videos/render-batch",
    status_code=status.HTTP_202_ACCEPTED,
)
async def render_videos_batch(
    album_id: str,
    payload: schemas.BatchVideoRenderRequest,
    background_tasks: BackgroundTasks,
):
    require("albums", album_id, "Album")
    album_track_ids = {
        track["id"]
        for track in db.fetch_all(
            "SELECT id FROM tracks WHERE album_id = ?",
            (album_id,),
        )
    }
    invalid = [track_id for track_id in payload.track_ids if track_id not in album_track_ids]
    if invalid:
        raise HTTPException(status_code=409, detail="Some tracks belong to another album")
    generations = [
        require("generations", generation_id, "Generation")
        for generation_id in payload.generation_ids
    ]
    if any(generation["track_id"] not in album_track_ids for generation in generations):
        raise HTTPException(
            status_code=409,
            detail="Some generations belong to another album",
        )
    if not payload.track_ids and not payload.generation_ids:
        raise HTTPException(status_code=422, detail="Select at least one audio candidate")
    job = services.create_job(
        "video_render_batch",
        "album",
        album_id,
        dump(payload),
    )
    background_tasks.add_task(
        services.run_batch_video_render,
        job["id"],
        album_id,
        payload,
    )
    return data({"job_id": job["id"], "status": job["status"]})


@router.post(
    "/albums/{album_id}/videos/combine",
    status_code=status.HTTP_202_ACCEPTED,
)
async def combine_album_videos(
    album_id: str,
    payload: schemas.AlbumVideoRenderRequest,
    background_tasks: BackgroundTasks,
):
    require("albums", album_id, "Album")
    assets = [db.get_one("assets", asset_id) for asset_id in payload.video_asset_ids]
    invalid = [
        asset_id
        for asset_id, asset in zip(payload.video_asset_ids, assets)
        if not asset
        or asset["album_id"] != album_id
        or asset["type"] != "video"
        or not asset.get("track_id")
    ]
    if invalid:
        raise HTTPException(
            status_code=409,
            detail="Some video assets are not track videos in this album",
        )
    job = services.create_job(
        "album_video_render",
        "album",
        album_id,
        dump(payload),
    )
    background_tasks.add_task(
        services.run_album_video_render,
        job["id"],
        album_id,
        payload,
    )
    return data({"job_id": job["id"], "status": job["status"]})


@router.post("/albums/{album_id}/videos/durations")
async def get_video_durations(
    album_id: str,
    payload: schemas.VideoDurationRequest,
):
    require("albums", album_id, "Album")
    durations: dict[str, float] = {}
    for asset_id in payload.video_asset_ids:
        asset = require("assets", asset_id, "Video asset")
        if asset["album_id"] != album_id or asset["type"] != "video":
            raise HTTPException(status_code=409, detail="Invalid video asset")
        metadata_duration = (asset.get("metadata") or {}).get("duration_seconds")
        if isinstance(metadata_duration, (int, float)) and metadata_duration > 0:
            durations[asset_id] = float(metadata_duration)
            continue
        path = db.STORAGE_DIR / asset["storage_key"]
        durations[asset_id] = services.probe_video_duration(path)
    return data(durations)


@router.get("/assets/{asset_id}/download")
async def download_asset(asset_id: str):
    asset = require("assets", asset_id, "Asset")
    path = (db.STORAGE_DIR / asset["storage_key"]).resolve()
    if not path.is_file() or db.STORAGE_DIR.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="Asset file not found")
    return FileResponse(
        path,
        media_type=asset["content_type"],
        filename=asset["original_name"],
    )


@router.post(
    "/albums/{album_id}/archive", status_code=status.HTTP_201_CREATED
)
async def create_archive(album_id: str):
    album = album_detail(album_id)
    relative = Path("albums") / album_id / "archive" / f"{album_id}.zip"
    destination = db.STORAGE_DIR / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "album.json", json.dumps(album, ensure_ascii=False, indent=2)
        )
        for track in album["tracks"]:
            prefix = f"{int(track['sequence']):02d}-{track['title']}"
            archive.writestr(f"{prefix}.txt", track["lyrics"])
            if track.get("selected_generation_id"):
                generation = db.get_one(
                    "generations", track["selected_generation_id"]
                )
                if generation and generation.get("local_audio_path"):
                    audio = db.STORAGE_DIR / generation["local_audio_path"]
                    if audio.is_file():
                        archive.write(audio, f"{prefix}.mp3")
    asset = services.create_asset(
        album_id=album_id,
        track_id=None,
        generation_id=None,
        asset_type="archive",
        path=destination,
        original_name=f"{album['title']}.zip",
        content_type="application/zip",
    )
    return data(asset)
