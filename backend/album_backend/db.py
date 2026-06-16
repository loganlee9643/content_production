from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STORAGE_DIR = ROOT / "storage"
DB_PATH = DATA_DIR / "album_backend.sqlite3"
_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS albums (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        artist_name TEXT,
        description TEXT,
        genre TEXT NOT NULL DEFAULT '',
        vocal_style TEXT NOT NULL DEFAULT '',
        tempo TEXT NOT NULL DEFAULT '',
        lyrics_language TEXT NOT NULL DEFAULT 'ko',
        mood TEXT NOT NULL DEFAULT '',
        instruments_json TEXT NOT NULL DEFAULT '[]',
        keywords TEXT NOT NULL DEFAULT '',
        additional_instructions TEXT NOT NULL DEFAULT '',
        style_prompt TEXT NOT NULL DEFAULT '',
        visual_concept TEXT NOT NULL DEFAULT '',
        thumbnail_image_prompt TEXT NOT NULL DEFAULT '',
        track_count INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'draft',
        selected_cover_asset_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tracks (
        id TEXT PRIMARY KEY,
        album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL,
        title TEXT NOT NULL,
        concept TEXT NOT NULL DEFAULT '',
        lyrics TEXT NOT NULL DEFAULT '',
        style_prompt TEXT NOT NULL DEFAULT '',
        image_prompt TEXT NOT NULL DEFAULT '',
        negative_tags TEXT NOT NULL DEFAULT '',
        instrumental INTEGER NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT 'chirp-fenix',
        status TEXT NOT NULL DEFAULT 'draft',
        selected_generation_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        progress INTEGER NOT NULL DEFAULT 0,
        attempt INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        error_code TEXT,
        error_message TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        result_json TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS generations (
        id TEXT PRIMARY KEY,
        track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        request_id TEXT,
        clip_id TEXT NOT NULL,
        status TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        audio_url TEXT,
        image_url TEXT,
        local_audio_path TEXT,
        generated_lyrics TEXT,
        tags TEXT,
        raw_response_json TEXT NOT NULL DEFAULT '{}',
        is_selected INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        completed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS assets (
        id TEXT PRIMARY KEY,
        album_id TEXT REFERENCES albums(id) ON DELETE CASCADE,
        track_id TEXT REFERENCES tracks(id) ON DELETE CASCADE,
        generation_id TEXT REFERENCES generations(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        storage_key TEXT NOT NULL,
        original_name TEXT NOT NULL,
        content_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS video_templates (
        id TEXT PRIMARY KEY,
        album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        compose_json TEXT NOT NULL DEFAULT '{}',
        image_instruction TEXT NOT NULL DEFAULT '',
        title_source TEXT NOT NULL DEFAULT 'track',
        artist_source TEXT NOT NULL DEFAULT 'album',
        preview_asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS track_video_templates (
        track_id TEXT PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
        template_id TEXT NOT NULL REFERENCES video_templates(id) ON DELETE CASCADE,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS thumbnails (
        id TEXT PRIMARY KEY,
        album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        background_asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
        design_json TEXT NOT NULL DEFAULT '{}',
        rendered_asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_album_sequence
        ON tracks(album_id, sequence);
    CREATE INDEX IF NOT EXISTS idx_jobs_resource
        ON jobs(resource_type, resource_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_generations_track
        ON generations(track_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_video_templates_album
        ON video_templates(album_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_thumbnails_album
        ON thumbnails(album_id, updated_at DESC);
    """
    with _LOCK, closing(connect()) as connection:
        connection.executescript(schema)
        template_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(video_templates)")
        }
        if "title_source" not in template_columns:
            connection.execute(
                "ALTER TABLE video_templates "
                "ADD COLUMN title_source TEXT NOT NULL DEFAULT 'track'"
            )
        if "artist_source" not in template_columns:
            connection.execute(
                "ALTER TABLE video_templates "
                "ADD COLUMN artist_source TEXT NOT NULL DEFAULT 'album'"
            )
        if "preview_asset_id" not in template_columns:
            connection.execute(
                "ALTER TABLE video_templates ADD COLUMN preview_asset_id TEXT"
            )
        album_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(albums)")
        }
        if "visual_concept" not in album_columns:
            connection.execute(
                "ALTER TABLE albums "
                "ADD COLUMN visual_concept TEXT NOT NULL DEFAULT ''"
            )
        if "thumbnail_image_prompt" not in album_columns:
            connection.execute(
                "ALTER TABLE albums "
                "ADD COLUMN thumbnail_image_prompt TEXT NOT NULL DEFAULT ''"
            )
        connection.commit()


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with _LOCK, closing(connect()) as connection:
        connection.execute(sql, params)
        connection.commit()


def insert(table: str, values: dict[str, Any]) -> dict[str, Any]:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    return get_one(table, values["id"])


def update(table: str, row_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
    if not values:
        return get_one(table, row_id)
    assignments = ", ".join(f"{column} = ?" for column in values)
    execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?",
        (*values.values(), row_id),
    )
    return get_one(table, row_id)


def get_one(table: str, row_id: str) -> dict[str, Any] | None:
    with closing(connect()) as connection:
        row = connection.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,)
        ).fetchone()
    return decode_row(dict(row)) if row else None


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [decode_row(dict(row)) for row in rows]


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with closing(connect()) as connection:
        row = connection.execute(sql, params).fetchone()
    return decode_row(dict(row)) if row else None


def delete(table: str, row_id: str) -> None:
    execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def decode_row(row: dict[str, Any]) -> dict[str, Any]:
    for key in list(row):
        if key.endswith("_json"):
            decoded_key = key[:-5]
            try:
                row[decoded_key] = json.loads(row.pop(key) or "null")
            except json.JSONDecodeError:
                row[decoded_key] = None
    for key in ("instrumental", "is_selected"):
        if key in row:
            row[key] = bool(row[key])
    return row
