# -*- coding:utf-8 -*-

import json
import logging
import os
import re

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware

import schemas
from album_backend.db import init_db
from album_backend.router import router as album_router
from deps import get_token
from utils import (
    SunoAPIError,
    SunoGenerationVerificationError,
    generate_lyrics,
    generate_music,
    get_feed,
    get_lyrics,
    get_credits,
)

app = FastAPI()
app.include_router(album_router)
logger = logging.getLogger("suno_api.main")
job_poll_logger = logging.getLogger("album_backend.job_poll")
JOB_POLL_PATH = re.compile(r"^/api/v1/jobs/[^/?]+$")


class JobPollingAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not record.args or len(record.args) < 3:
            return True
        return not bool(JOB_POLL_PATH.match(str(record.args[2])))

cors_origins = [
    value.strip()
    for value in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if value.strip()
]
cors_origin_regex = os.getenv(
    "CORS_ORIGIN_REGEX",
    r"https?://(localhost|127\.0\.0\.1|10(?:\.\d{1,3}){3}|"
    r"192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])"
    r"(?:\.\d{1,3}){2})(:\d+)?",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def initialize_album_backend():
    init_db()
    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(existing, JobPollingAccessFilter)
        for existing in access_logger.filters
    ):
        access_logger.addFilter(JobPollingAccessFilter())


@app.middleware("http")
async def log_job_poll_at_debug(request: Request, call_next):
    response = await call_next(request)
    if JOB_POLL_PATH.match(request.url.path):
        job_poll_logger.debug(
            "%s %s status=%s client=%s",
            request.method,
            request.url.path,
            response.status_code,
            request.client.host if request.client else "-",
        )
    return response


def generation_error(exc):
    if isinstance(exc, SunoGenerationVerificationError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    if (
        isinstance(exc, SunoAPIError)
        and exc.error_type == "token_validation_failed"
    ):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Suno가 음악 생성 요청에 필요한 브라우저 검증을 거부했습니다. "
                "로그인 JWT와 쿠키 문제 또는 모델 입력 오류가 아니라, "
                "Suno 웹 생성 과정에서 발급되는 별도 검증이 필요한 상태입니다."
            ),
        )
    return HTTPException(
        detail=str(exc), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
    )


@app.get("/")
async def get_root():
    return schemas.Response()


@app.post("/generate")
async def generate(
    data: schemas.CustomModeGenerateParam, token: str = Depends(get_token)
):
    try:
        resp = await generate_music(data.dict(), token)
        return resp
    except Exception as e:
        logger.exception(
            "Direct custom generation failed model=%s error_type=%s error=%s",
            data.mv,
            type(e).__name__,
            e,
        )
        raise generation_error(e)


@app.post("/generate/description-mode")
async def generate_with_song_description(
    data: schemas.DescriptionModeGenerateParam, token: str = Depends(get_token)
):
    try:
        resp = await generate_music(data.dict(), token)
        return resp
    except Exception as e:
        logger.exception(
            "Direct description generation failed model=%s instrumental=%s "
            "prompt_length=%s error_type=%s error=%s",
            data.mv,
            data.make_instrumental,
            len(data.gpt_description_prompt),
            type(e).__name__,
            e,
        )
        raise generation_error(e)


@app.get("/feed/{aid}")
async def fetch_feed(aid: str, token: str = Depends(get_token)):
    try:
        resp = await get_feed(aid, token)
        return resp
    except Exception as e:
        raise HTTPException(
            detail=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@app.post("/generate/lyrics/")
async def generate_lyrics_post(request: Request, token: str = Depends(get_token)):
    req = await request.json()
    prompt = req.get("prompt")
    if prompt is None:
        raise HTTPException(
            detail="prompt is required", status_code=status.HTTP_400_BAD_REQUEST
        )

    try:
        resp = await generate_lyrics(prompt, token)
        return resp
    except Exception as e:
        raise HTTPException(
            detail=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@app.get("/lyrics/{lid}")
async def fetch_lyrics(lid: str, token: str = Depends(get_token)):
    try:
        resp = await get_lyrics(lid, token)
        return resp
    except Exception as e:
        raise HTTPException(
            detail=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@app.get("/get_credits")
async def fetch_credits(token: str = Depends(get_token)):
    try:
        resp = await get_credits(token)
        return resp
    except Exception as e:
        raise HTTPException(
            detail=str(e), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
