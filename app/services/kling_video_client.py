from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class KlingVideoApiError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


def _base_url(url: str) -> str:
    value = (url or "https://api.klingai.com").strip().rstrip("/")
    if value == "https://api.klingapi.com":
        return "https://api.klingai.com"
    return value or "https://api.klingai.com"


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


def _request_json(method: str, url: str, token: str, payload: dict[str, Any] | None = None, *, timeout: float = 60.0) -> Any:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise KlingVideoApiError(f"Kling API error {e.code}: {detail}") from e
    except URLError as e:
        raise KlingVideoApiError(f"Kling API is not reachable: {e}") from e
    return json.loads(raw.decode("utf-8")) if raw else {}


def _poll_json(base: str, task_id: str, token: str) -> Any:
    urls = (
        f"{base}/v1/videos/image2video/{task_id}",
        f"{base}/v1/videos/{task_id}",
    )
    last_404: KlingVideoApiError | None = None
    for url in urls:
        logger.debug("Kling polling request url=%s", url)
        try:
            payload = _request_json("GET", url, token, timeout=60.0)
            logger.info("Kling polling endpoint accepted: %s", url)
            return payload
        except KlingVideoApiError as e:
            if "Kling API error 404:" not in str(e):
                raise
            logger.warning("Kling polling endpoint returned 404: %s", url)
            last_404 = e
    if last_404:
        raise last_404
    raise KlingVideoApiError("Kling API polling failed.")


def _image_base64(image_path: Path) -> str:
    if not image_path.is_file():
        raise KlingVideoApiError(f"Image file does not exist: {image_path}")
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


def _duration_for_kling(seconds: int) -> int:
    return 10 if int(seconds) > 5 else 5


def _mode_for_kling(mode: str) -> str:
    value = (mode or "std").strip().lower()
    if value in ("professional", "pro"):
        return "pro"
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


def _find_video_url(value: Any) -> str:
    if isinstance(value, str) and value.startswith(("http://", "https://")) and value.lower().split("?")[0].endswith((".mp4", ".webm", ".mov")):
        return value
    if isinstance(value, dict):
        for key in ("video_url", "url", "download_url", "output_url"):
            found = _find_video_url(value.get(key))
            if found:
                return found
        for key in ("video", "output", "result", "data"):
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
    try:
        with urlopen(req, timeout=300.0) as resp:
            out_video_path.parent.mkdir(parents=True, exist_ok=True)
            with out_video_path.open("wb") as f:
                shutil.copyfileobj(resp, f)
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise KlingVideoApiError(f"Kling video download failed {e.code}: {detail}") from e
    except URLError as e:
        raise KlingVideoApiError(f"Kling video download failed: {e}") from e
    return out_video_path


def generate_video_from_image_kling_api(
    *,
    access_key: str,
    secret_key: str = "",
    base_url: str = "https://api.klingai.com",
    prompt: str,
    image_path: Path,
    out_video_path: Path,
    model: str = "kling-v2.5-turbo",
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
    payload: dict[str, Any] = {
        "model": (model or "kling-v2.5-turbo").strip(),
        "image": image_b64,
        "image_base64": image_b64,
        "prompt": (prompt or "").strip(),
        "duration": _duration_for_kling(duration_seconds),
        "aspect_ratio": aspect_ratio or "16:9",
        "mode": _mode_for_kling(mode),
    }
    if negative_prompt.strip():
        payload["negative_prompt"] = negative_prompt.strip()

    create_url = f"{base}/v1/videos/image2video"
    logger.info("Kling image-to-video create request url=%s model=%s mode=%s duration=%s", create_url, payload["model"], payload["mode"], payload["duration"])
    created = _request_json("POST", create_url, token, payload, timeout=120.0)
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
            raise KlingVideoApiError(f"Kling generation failed: {status_payload}")
        time.sleep(max(1.0, float(poll_interval_sec)))
    raise KlingVideoApiError(f"Kling generation timed out. Last response: {last_payload}")
