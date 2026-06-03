from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import shutil
import socket
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class KlingVideoApiError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


def _base_url(url: str) -> str:
    value = (url or "https://api-singapore.klingai.com").strip().rstrip("/")
    if value == "https://api.klingapi.com":
        return "https://api-singapore.klingai.com"
    return value or "https://api-singapore.klingai.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _jwt_token(access_key: str, secret_key: str) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": access_key,
        "exp": now + 1800,
        "nbf": now - 5,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    sig = hmac.new(secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def _auth_token(access_key: str, secret_key: str = "") -> str:
    access = (access_key or "").strip()
    secret = (secret_key or "").strip()
    if not access:
        raise KlingVideoApiError("Kling Access Key is required.")
    if secret:
        return _jwt_token(access, secret)
    return access


def _request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
) -> Any:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method)
    last_error: BaseException | None = None
    attempts = max(1, int(max_retries))
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        except HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise KlingVideoApiError(f"Kling API error {e.code}: {detail}") from e
        except (TimeoutError, socket.timeout, URLError) as e:
            last_error = e
            if attempt >= attempts:
                raise KlingVideoApiError(f"Kling API request timed out or failed after {attempts} attempts: {e}") from e
            logger.warning(
                "Kling API request failed attempt %s/%s method=%s url=%s error=%s",
                attempt,
                attempts,
                method,
                url,
                e,
            )
            time.sleep(min(10.0, 1.5 * attempt))
    else:
        raise KlingVideoApiError(f"Kling API request failed: {last_error}")
    decoded = json.loads(raw.decode("utf-8")) if raw else {}
    if isinstance(decoded, dict):
        code = decoded.get("code")
        if code not in (None, 0, "0"):
            message = str(decoded.get("message", "") or "").strip()
            request_id = str(decoded.get("request_id", "") or "").strip()
            suffix = f" request_id={request_id}" if request_id else ""
            raise KlingVideoApiError(f"Kling API returned code {code}: {message or decoded}{suffix}")
    return decoded


def _poll_json(base: str, task_id: str, token: str) -> Any:
    url = f"{base}/v1/videos/image2video/{task_id}"
    logger.debug("Kling polling request url=%s", url)
    return _request_json("GET", url, token, timeout=120.0, max_retries=3)


def _image_base64(image_path: Path) -> str:
    if not image_path.is_file():
        raise KlingVideoApiError(f"Image file does not exist: {image_path}")
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


def _duration_for_kling(seconds: int, *, model_name: str = "") -> str:
    value = max(1, int(seconds or 5))
    return "10" if value > 5 else "5"


def _model_name_for_kling(model: str) -> str:
    value = (model or "kling-v2-5-turbo").strip()
    aliases = {
        "kling-v2.5-turbo": "kling-v2-5-turbo",
        "kling-v2.6": "kling-v2-6",
        "kling-v2.6-std": "kling-v2-6",
        "kling-v2.6-pro": "kling-v2-6",
        "kling-v3.0": "kling-v3",
        "kling-3.0": "kling-v3",
        "kling-v3-0": "kling-v3",
    }
    return aliases.get(value, value)


def _mode_for_kling(mode: str, *, model: str = "") -> str:
    model_value = (model or "").strip().lower()
    if model_value.endswith("-pro"):
        return "pro"
    if model_value.endswith("-std"):
        return "std"
    value = (mode or "std").strip().lower()
    if value in ("professional", "pro"):
        return "pro"
    if value in ("4k", "uhd"):
        return "4k"
    return "std"


def _task_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("task_id", "id"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("task_id", "id"):
            value = str(data.get(key, "") or "").strip()
            if value:
                return value
    return ""


def _status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("status", "state", "task_status"):
        value = str(payload.get(key, "") or "").strip().lower()
        if value:
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        return _status(data)
    return ""


def _failure_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("task_status_msg", "message", "status_msg", "error"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        return _failure_message(data)
    return ""


def _find_video_url(value: Any) -> str:
    if isinstance(value, str) and value.startswith(("http://", "https://")) and value.lower().split("?")[0].endswith((".mp4", ".webm", ".mov")):
        return value
    if isinstance(value, dict):
        for key in ("video_url", "url", "download_url", "output_url"):
            found = _find_video_url(value.get(key))
            if found:
                return found
        for key in ("video", "videos", "output", "result", "task_result", "data"):
            found = _find_video_url(value.get(key))
            if found:
                return found
        for child in value.values():
            found = _find_video_url(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_video_url(child)
            if found:
                return found
    return ""


def _download(url: str, out_video_path: Path) -> Path:
    req = Request(url, method="GET")
    attempts = 3
    for attempt in range(1, attempts + 1):
        tmp_path = out_video_path.with_name(f"{out_video_path.name}.download")
        try:
            with urlopen(req, timeout=600.0) as resp:
                out_video_path.parent.mkdir(parents=True, exist_ok=True)
                with tmp_path.open("wb") as f:
                    shutil.copyfileobj(resp, f)
            tmp_path.replace(out_video_path)
            return out_video_path
        except HTTPError as e:
            tmp_path.unlink(missing_ok=True)
            detail = e.read().decode("utf-8", errors="replace")
            raise KlingVideoApiError(f"Kling video download failed {e.code}: {detail}") from e
        except (TimeoutError, socket.timeout, URLError) as e:
            tmp_path.unlink(missing_ok=True)
            if attempt >= attempts:
                raise KlingVideoApiError(f"Kling video download timed out or failed after {attempts} attempts: {e}") from e
            logger.warning("Kling video download failed attempt %s/%s url=%s error=%s", attempt, attempts, url, e)
            time.sleep(min(10.0, 2.0 * attempt))
    return out_video_path


def generate_video_from_image_kling_api(
    *,
    access_key: str,
    secret_key: str = "",
    base_url: str = "https://api-singapore.klingai.com",
    prompt: str,
    image_path: Path,
    out_video_path: Path,
    model: str = "kling-v2-5-turbo",
    mode: str = "standard",
    aspect_ratio: str = "16:9",
    duration_seconds: int = 5,
    negative_prompt: str = "",
    poll_interval_sec: float = 5.0,
    timeout_sec: float = 3600.0,
) -> Path:
    token = _auth_token(access_key, secret_key)
    base = _base_url(base_url)
    image_b64 = _image_base64(image_path)
    model_name = _model_name_for_kling(model)
    clean_prompt = (prompt or "").strip()
    if len(clean_prompt) > 2500:
        logger.warning("Kling prompt truncated from %s to 2500 characters.", len(clean_prompt))
        clean_prompt = clean_prompt[:2500]
    payload: dict[str, Any] = {
        "model_name": model_name,
        "image": image_b64,
        "prompt": clean_prompt,
        "duration": _duration_for_kling(duration_seconds, model_name=model_name),
        "mode": _mode_for_kling(mode, model=model),
        "sound": "off",
        "watermark_info": {"enabled": False},
    }
    if negative_prompt.strip():
        payload["negative_prompt"] = negative_prompt.strip()[:2500]

    create_url = f"{base}/v1/videos/image2video"
    logger.info(
        "Kling image-to-video create request url=%s model_name=%s mode=%s duration=%s",
        create_url,
        payload["model_name"],
        payload["mode"],
        payload["duration"],
    )
    created = _request_json("POST", create_url, token, payload, timeout=240.0, max_retries=3)
    task_id = _task_id(created)
    if not task_id:
        direct_url = _find_video_url(created)
        if direct_url:
            return _download(direct_url, out_video_path)
        raise KlingVideoApiError(f"Kling API did not return a task_id: {created}")

    deadline = time.monotonic() + max(30.0, float(timeout_sec))
    last_payload: Any = created
    while time.monotonic() <= deadline:
        status_payload = _poll_json(base, task_id, token)
        last_payload = status_payload
        video_url = _find_video_url(status_payload)
        status = _status(status_payload)
        if video_url and status in ("", "completed", "complete", "succeeded", "succeed", "success", "finished", "done"):
            return _download(video_url, out_video_path)
        if status in ("failed", "failure", "error", "cancelled", "canceled"):
            detail = _failure_message(status_payload)
            raise KlingVideoApiError(f"Kling generation failed: {detail or status_payload}")
        time.sleep(max(1.0, float(poll_interval_sec)))
    raise KlingVideoApiError(f"Kling generation timed out. Last response: {last_payload}")
