from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from app.services.gemini_model_catalog import DEFAULT_GEMINI_MODEL
from app.services.llm_errors import LlmProviderError

logger = logging.getLogger(__name__)


class GeminiApiError(LlmProviderError):
    pass


def _normalize_model_id(model: str) -> str:
    m = model.strip()
    if m.startswith("models/"):
        m = m[len("models/") :]
    return m


def _gemini_url(model_id: str, api_key: str) -> str:
    qs = urlencode({"key": api_key})
    return (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model_id}:generateContent?{qs}"
    )


def _extract_text(payload: dict[str, Any]) -> str:
    fb = payload.get("promptFeedback")
    if isinstance(fb, dict):
        br = fb.get("blockReason")
        if br:
            raise GeminiApiError(f"프롬프트가 차단되었습니다: {br}")

    cands = payload.get("candidates")
    if not isinstance(cands, list) or not cands:
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            raise GeminiApiError(str(err["message"]))
        raise GeminiApiError("응답에 candidates가 없습니다.")

    first = cands[0]
    if not isinstance(first, dict):
        raise GeminiApiError("candidate 형식이 올바르지 않습니다.")

    fr = first.get("finishReason")

    content = first.get("content")
    if not isinstance(content, dict):
        raise GeminiApiError("응답에 content가 없습니다.")
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise GeminiApiError("응답 content.parts가 없습니다.")

    chunks: list[str] = []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            chunks.append(p["text"])
    out = "".join(chunks).strip()
    if not out:
        hint = f" (finishReason={fr})" if fr else ""
        raise GeminiApiError(f"모델이 빈 텍스트를 반환했습니다.{hint}")
    if fr and fr not in ("STOP", "MAX_TOKENS"):
        logger.warning("Gemini finishReason=%s (텍스트는 있음, 파싱 시도)", fr)
    return out


def gemini_generate_content(
    api_key: str,
    model: str,
    *,
    system_instruction: str,
    user_text: str,
    timeout_sec: float = 300.0,
) -> str:
    """Gemini generateContent. assistant 텍스트만 반환."""
    key = (api_key or os.environ.get("GEMINI_API_KEY", "") or "").strip()
    if not key:
        raise GeminiApiError("Gemini API 키가 비어 있습니다. 입력란에 넣거나 환경 변수 GEMINI_API_KEY를 설정하세요.")

    model_id = _normalize_model_id(model)
    if not model_id:
        raise GeminiApiError(f"Gemini 모델 ID를 입력하세요. (예: {DEFAULT_GEMINI_MODEL})")

    url = _gemini_url(model_id, key)
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192,
        },
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    logger.debug(
        "Gemini POST model=%r timeout=%ss payload_bytes=%s (API key redacted)",
        model_id,
        timeout_sec,
        len(data),
    )
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw_txt = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        logger.warning("Gemini HTTPError code=%s body_prefix=%r", e.code, err_body[:500])
        msg = err_body[:800]
        try:
            ej = json.loads(err_body)
            em = ej.get("error") if isinstance(ej, dict) else None
            if isinstance(em, dict) and em.get("message"):
                msg = str(em["message"])
        except json.JSONDecodeError:
            pass
        raise GeminiApiError(f"HTTP {e.code}: {msg or e.reason}") from e
    except urllib.error.URLError as e:
        logger.warning("Gemini URLError reason=%r", e.reason, exc_info=True)
        raise GeminiApiError(f"연결 실패: {e.reason}") from e

    logger.debug("Gemini 응답 raw_chars=%s", len(raw_txt))
    try:
        payload = json.loads(raw_txt)
    except json.JSONDecodeError as e:
        logger.warning("Gemini JSON 파싱 실패: %s raw_prefix=%r", e, raw_txt[:400])
        raise GeminiApiError(f"응답 JSON 파싱 실패: {e}") from e

    if not isinstance(payload, dict):
        raise GeminiApiError("응답 루트가 객체가 아닙니다.")

    text = _extract_text(payload)
    logger.debug("Gemini assistant_text_chars=%s", len(text))
    return text
