from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from app.services.llm_errors import LlmProviderError

logger = logging.getLogger(__name__)


class OllamaHttpError(LlmProviderError):
    pass


def ollama_chat(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    timeout_sec: float = 900.0,
) -> str:
    """Ollama /api/chat 호출(stream=false). assistant 텍스트만 반환."""
    root = base_url.rstrip("/")
    url = f"{root}/api/chat"
    body: dict[str, Any] = {
        "model": model.strip(),
        "messages": messages,
        "stream": False,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    logger.debug(
        "Ollama POST url=%s model=%r msg_count=%s timeout=%ss payload_bytes=%s",
        url,
        model.strip(),
        len(messages),
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
        logger.warning("Ollama HTTPError code=%s url=%s body=%s", e.code, url, err_body[:500])
        raise OllamaHttpError(f"HTTP {e.code}: {err_body or e.reason}") from e
    except urllib.error.URLError as e:
        logger.warning("Ollama URLError url=%s reason=%r", url, e.reason, exc_info=True)
        raise OllamaHttpError(f"연결 실패: {e.reason}") from e

    logger.debug("Ollama 응답 raw_chars=%s", len(raw_txt))
    try:
        payload = json.loads(raw_txt)
    except json.JSONDecodeError as e:
        logger.warning("Ollama 응답 JSON 파싱 실패: %s raw_prefix=%r", e, raw_txt[:400])
        raise OllamaHttpError(f"응답 JSON 파싱 실패: {e}") from e

    msg = payload.get("message")
    if not isinstance(msg, dict):
        raise OllamaHttpError("응답에 message 객체가 없습니다.")
    content = msg.get("content")
    if not isinstance(content, str):
        raise OllamaHttpError("응답 message.content가 문자열이 아닙니다.")
    logger.debug("Ollama assistant_content_chars=%s", len(content))
    return content
