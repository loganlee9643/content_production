from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import cast

from app.services.llm_errors import LlmProviderError

logger = logging.getLogger(__name__)


class GeminiImageApiError(LlmProviderError):
    pass


def _normalize_model_id(model: str) -> str:
    m = model.strip()
    if m.startswith("models/"):
        m = m[len("models/") :]
    return m


def _gemini_generate_url(model_id: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model_id}:generateContent"
    )


def api_aspect_ratio_from_resolution(resolution: str) -> str:
    """문서에 나온 aspectRatio 문자열 (1:1, 16:9, 9:16 등)."""
    r = (resolution or "").strip()
    if r == "1080x1920":
        return "9:16"
    return "16:9"


def _generation_config_for_model(model_id: str, aspect_ratio: str) -> dict[str, Any]:
    """generationConfig: 비율은 항상 imageConfig (proto의 문자열 aspect_ratio).

    responseFormat.image.* 는 ImageResponseFormat enum 이라 REST에 "16:9" 문자열을 넣으면 400.
    Gemini 3.x 이미지: responseModalities 추가.
    """
    return {
        "responseModalities": ["IMAGE"],
        "imageConfig": {"aspectRatio": aspect_ratio},
    }


def _image_part_from_path(path: Path) -> dict[str, Any]:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "inlineData": {
            "mimeType": mime,
            "data": data,
        }
    }


_MAX_IMAGE_HTTP_RETRIES = 6
_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})


def _is_quota_exhausted_error(message: str) -> bool:
    msg = (message or "").lower()
    return (
        "quota exceeded" in msg
        or "exceeded your current quota" in msg
        or "free_tier" in msg
        or "resource_exhausted" in msg
        or "rate-limit" in msg
    )


def _inline_payload(part: dict[str, Any]) -> tuple[bytes, str] | None:
    inline = part.get("inlineData") or part.get("inline_data")
    if not isinstance(inline, dict):
        return None
    b64 = inline.get("data")
    mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
    if not isinstance(b64, str) or not b64.strip():
        return None
    try:
        raw = base64.b64decode(b64, validate=False)
    except (ValueError, TypeError):
        return None
    if not raw:
        return None
    return raw, mime


def _summarize_parts(parts: list[Any]) -> str:
    bits: list[str] = []
    for i, p in enumerate(parts[:16]):
        if not isinstance(p, dict):
            bits.append(f"[{i}]:non-dict")
            continue
        keys = sorted(p.keys())
        bits.append(f"[{i}]:{keys}")
        t = p.get("text")
        if isinstance(t, str) and t.strip():
            bits.append(f" text={t.strip()[:200]!r}")
    return " | ".join(bits)


def _extract_first_image(payload: dict[str, Any]) -> tuple[bytes, str]:
    fb = payload.get("promptFeedback")
    if isinstance(fb, dict):
        br = fb.get("blockReason")
        if br:
            raise GeminiImageApiError(f"프롬프트가 차단되었습니다: {br}")

    cands = payload.get("candidates")
    if not isinstance(cands, list) or not cands:
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            raise GeminiImageApiError(str(err["message"]))
        raise GeminiImageApiError("응답에 candidates가 없습니다.")

    text_only_hint = ""
    for ci, cand in enumerate(cands):
        if not isinstance(cand, dict):
            continue
        fr = cand.get("finishReason")
        content = cand.get("content")
        if not isinstance(content, dict):
            logger.warning("Gemini image candidate[%s] content 없음 finishReason=%r", ci, fr)
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            logger.warning("Gemini image candidate[%s] parts 없음 finishReason=%r", ci, fr)
            continue

        for p in parts:
            if not isinstance(p, dict):
                continue
            got = _inline_payload(p)
            if got is not None:
                return got

        # 진단: 텍스트만 온 경우
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip():
                text_only_hint = p["text"].strip()[:400]
                break
        logger.warning(
            "Gemini image candidate[%s]에 inlineData 없음 finishReason=%r parts=%s",
            ci,
            fr,
            _summarize_parts(parts),
        )

    msg = (
        "응답에 이미지(inlineData)가 없습니다. "
        "씬 편집 탭의「배경 이미지 모델」이 이미지 전용 모델인지 확인하세요 "
        "(예: gemini-2.5-flash-image, gemini-3.1-flash-image-preview). "
        "터미널 로그(CONTENT_PRODUCTION_LOG=DEBUG)에 응답 요약이 더 출력됩니다."
    )
    if text_only_hint:
        msg += f"\n\n모델 텍스트 응답 일부:\n{text_only_hint}"
    raise GeminiImageApiError(msg)


def gemini_generate_image(
    api_key: str,
    model: str,
    *,
    prompt: str,
    reference_image_paths: list[Path] | None = None,
    aspect_ratio: str = "16:9",
    timeout_sec: float = 180.0,
) -> tuple[bytes, str]:
    """Gemini 이미지 모델 generateContent. 바이트·MIME 반환."""
    key = (api_key or os.environ.get("GEMINI_API_KEY", "") or "").strip()
    if not key:
        raise GeminiImageApiError(
            "Gemini API 키가 비어 있습니다. 프롬프트 탭의 API 키 또는 GEMINI_API_KEY를 설정하세요."
        )

    model_id = _normalize_model_id(model)
    if not model_id:
        raise GeminiImageApiError("이미지 생성용 모델 ID를 입력하세요.")

    url = _gemini_generate_url(model_id)
    gen_cfg = _generation_config_for_model(model_id, aspect_ratio)
    parts: list[dict[str, Any]] = [{"text": prompt.strip()}]
    for ref_path in reference_image_paths or []:
        if ref_path.is_file():
            parts.append(_image_part_from_path(ref_path))

    body: dict[str, Any] = {
        # 공식 curl 예시와 동일하게 role 생략 가능
        "contents": [{"parts": parts}],
        "generationConfig": gen_cfg,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    dump_chars_raw = (os.environ.get("CONTENT_PRODUCTION_GEMINI_DUMP_CHARS", "") or "").strip()
    try:
        dump_chars = int(dump_chars_raw) if dump_chars_raw else 1000
    except ValueError:
        dump_chars = 1000
    prompt_trimmed = (prompt or "").strip()
    logger.debug(
        "Gemini image request url=%s model=%r aspect_ratio=%r gen_cfg_keys=%s payload_bytes=%s prompt=%r",
        url,
        model_id,
        aspect_ratio,
        list(gen_cfg.keys()),
        len(data),
        prompt_trimmed[: min(len(prompt_trimmed), dump_chars)],
    )
    def make_request(payload: bytes) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-goog-api-key": key,
            },
            method="POST",
        )

    req = make_request(data)
    raw_txt = ""
    aspect_retry_used = False
    for attempt in range(_MAX_IMAGE_HTTP_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw_txt = resp.read().decode("utf-8", errors="replace")
                break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            logger.warning("Gemini image HTTPError code=%s body_prefix=%r", e.code, err_body[:500])
            msg = err_body[:800]
            try:
                ej = json.loads(err_body)
                em = ej.get("error") if isinstance(ej, dict) else None
                if isinstance(em, dict) and em.get("message"):
                    msg = str(em["message"])
            except json.JSONDecodeError:
                pass
            if e.code == 429 and _is_quota_exhausted_error(msg):
                raise GeminiImageApiError(
                    "Gemini 이미지 생성 할당량을 초과했습니다. Google AI Studio의 결제/쿼터를 확인하거나, "
                    "잠시 후 다시 시도하거나, 이미지 생성 모델/API 키를 변경하세요.\n"
                    f"원본 오류: {msg}"
                ) from e
            if e.code == 400 and not aspect_retry_used and "aspect" in msg.lower():
                aspect_retry_used = True
                logger.warning("Gemini image aspect ratio config rejected; retrying without explicit aspect ratio.")
                body["generationConfig"] = {"responseModalities": ["IMAGE"]}
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                req = make_request(data)
                continue
            if e.code in _RETRYABLE_HTTP and attempt < _MAX_IMAGE_HTTP_RETRIES - 1:
                wait = min(45.0, 1.5 * (2**attempt))
                logger.warning(
                    "Gemini image HTTP %s (일시적). %.0fs 후 재시도 (%s/%s)",
                    e.code,
                    wait,
                    attempt + 2,
                    _MAX_IMAGE_HTTP_RETRIES,
                )
                time.sleep(wait)
                continue
            raise GeminiImageApiError(f"HTTP {e.code}: {msg or e.reason}") from e
        except urllib.error.URLError as e:
            logger.warning("Gemini image URLError reason=%r", e.reason, exc_info=True)
            raise GeminiImageApiError(f"연결 실패: {e.reason}") from e

    if not raw_txt:
        raise GeminiImageApiError("빈 응답입니다.")

    logger.debug("Gemini image 응답 raw_chars=%s", len(raw_txt))
    try:
        payload = cast(dict[str, Any], json.loads(raw_txt))
    except json.JSONDecodeError as e:
        logger.warning("Gemini image JSON 파싱 실패: %s raw_prefix=%r", e, raw_txt[:400])
        raise GeminiImageApiError(f"응답 JSON 파싱 실패: {e}") from e

    if not isinstance(payload, dict):
        raise GeminiImageApiError("응답 루트가 객체가 아닙니다.")

    try:
        img_bytes, mime = _extract_first_image(payload)
    except GeminiImageApiError:
        logger.debug(
            "Gemini image 전체 응답(디버그): %s",
            raw_txt[:4000] if len(raw_txt) > 4000 else raw_txt,
        )
        raise

    logger.debug("Gemini image bytes=%s mime=%r", len(img_bytes), mime)
    return img_bytes, mime
