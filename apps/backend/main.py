import os
import re
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from bootstrap import ensure_local_database
from video_analysis import initialize_analysis, router as video_analysis_router
from production import initialize_production, router as production_router
from character_library import router as character_router
from collection import connection as collection_connection, initialize_collection, router as collection_router
from config import get_settings


@contextmanager
def connect():
    with psycopg.connect(get_settings().database_dsn, row_factory=dict_row) as db:
        yield db, "%s", "ILIKE"


@asynccontextmanager
async def lifespan(app):
    ensure_local_database()
    with collection_connection() as collection_db:
        initialize_collection(collection_db)
        initialize_analysis(collection_db)
        initialize_production(collection_db)
    with connect() as (db, placeholder, _):
        postgres = placeholder == "%s"
        timestamp = "timestamptz DEFAULT now()" if postgres else "text DEFAULT CURRENT_TIMESTAMP"
        project_id = "uuid" if postgres else "text"
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS reference (
                shortcode text PRIMARY KEY, url text NOT NULL,
                category text NOT NULL, notes text NOT NULL,
                created_at {timestamp} NOT NULL
            )
        """)
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS project (
                id {project_id} PRIMARY KEY,
                shortcode text NOT NULL REFERENCES reference(shortcode),
                created_at {timestamp} NOT NULL
            )
        """)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(collection_router)
app.include_router(video_analysis_router)
app.include_router(production_router)
app.include_router(character_router)


class ReelLink(BaseModel):
    url: str = Field(max_length=2048)


class ImportReel(ReelLink):
    category: str = Field(default="미분류", max_length=60)
    notes: str = Field(default="", max_length=3000)


class ProjectRequest(BaseModel):
    referenceId: str


def normalize(raw_url):
    try:
        parts = urlsplit(raw_url.strip())
        match = re.fullmatch(r"/reel/([A-Za-z0-9_-]{1,64})/?", parts.path)
        if parts.scheme == "https" and parts.netloc.lower() in {
            "instagram.com", "www.instagram.com"
        } and match:
            code = match[1]
            return code, f"https://www.instagram.com/reel/{code}/"
    except ValueError:
        pass
    raise HTTPException(422, "공개 릴스 링크를 입력해 주세요.")


@app.post("/api/references/instagram/preview")
async def preview(link: ReelLink):
    code, url = normalize(link.url)
    version = os.environ.get("META_GRAPH_VERSION", "v27.0")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            result = await client.get(
                f"https://graph.facebook.com/{version}/instagram_oembed",
                params={"url": url, "omitscript": "true"},
            )
        if result.is_success and isinstance(result.json().get("html"), str):
            status = "available"
        elif result.status_code in {400, 404}:
            status = "unavailable"
        else:
            status = "retryable_error"
    except (httpx.RequestError, ValueError):
        status = "retryable_error"
    return {"shortcode": code, "canonicalUrl": url, "previewStatus": status}


@app.post("/api/references/import/instagram")
def save_reference(item: ImportReel):
    code, url = normalize(item.url)
    with connect() as (db, parameter, _):
        row = db.execute(
            f"""INSERT INTO reference (shortcode, url, category, notes)
               VALUES ({parameter}, {parameter}, {parameter}, {parameter})
               ON CONFLICT (shortcode) DO NOTHING
               RETURNING shortcode""",
            (code, url, item.category.strip() or "미분류", item.notes.strip()),
        ).fetchone()
    return {"referenceId": code, "created": row is not None}


@app.get("/api/references")
def references(q: str = ""):
    pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with connect() as (db, parameter, like):
        rows = db.execute(
            f"""SELECT * FROM reference WHERE category {like} {parameter} ESCAPE '\\'
               OR notes {like} {parameter} ESCAPE '\\'
               OR shortcode {like} {parameter} ESCAPE '\\'
               ORDER BY created_at DESC, shortcode LIMIT 100""",
            (pattern, pattern, pattern),
        ).fetchall()
        return [dict(row) for row in rows]


@app.post("/api/projects", status_code=201)
def create_project(item: ProjectRequest):
    project_id = uuid4()
    with connect() as (db, parameter, _):
        row = db.execute(
            f"""INSERT INTO project (id, shortcode)
               SELECT {parameter}, shortcode FROM reference
               WHERE shortcode = {parameter} RETURNING id""",
            (str(project_id), item.referenceId),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "레퍼런스를 찾을 수 없습니다.")
    return {"projectId": row["id"]}


@app.get("/api/projects/{project_id}")
def project(project_id: str):
    from uuid import UUID

    try:
        key = UUID(project_id)
    except ValueError:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    with connect() as (db, parameter, _):
        row = db.execute(
            f"""SELECT project.id, reference.* FROM project JOIN reference USING (shortcode)
               WHERE project.id = {parameter}""",
            (str(key),),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    return dict(row)
