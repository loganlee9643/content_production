import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BASE_URL")
LEGACY_GENERATE_PATH = "/api/generate/v2/"
WEB_GENERATE_PATH = "/api/generate/v2-web/"
logger = logging.getLogger("suno_api.utils")
SUNO_USER_AGENT = os.getenv(
    "SUNO_USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
)

COMMON_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": SUNO_USER_AGENT,
    "Referer": "https://suno.com",
    "Origin": "https://suno.com",
}
DEVICE_ID_FILE = Path(__file__).resolve().parent / ".auth" / "suno-device-id"
LEGACY_GENERATION_HEADERS = {
    "Content-Type": "text/plain;charset=UTF-8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://suno.com",
    "Origin": "https://suno.com",
}


class SunoAPIError(RuntimeError):
    def __init__(self, status_code, method, url, body):
        self.status_code = status_code
        self.method = method
        self.url = url
        self.body = body
        self.error_type = None
        self.upstream_detail = None
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                self.error_type = payload.get("error_type")
                self.upstream_detail = payload.get("detail_fallback") or payload.get(
                    "detail"
                )
        except json.JSONDecodeError:
            pass
        super().__init__(
            f"Suno API failed: HTTP {status_code} {method} {url}: {body[:1000]}"
        )


class SunoGenerationVerificationError(RuntimeError):
    pass


async def fetch(
    url,
    headers=None,
    data=None,
    method="POST",
    merge_common_headers=True,
    not_found_as_none=False,
):
    if headers is None:
        headers = {}
    request_headers = (
        {**COMMON_HEADERS, **headers}
        if merge_common_headers
        else dict(headers)
    )
    if data is not None:
        data = json.dumps(data)

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method=method, url=url, data=data, headers=request_headers
        ) as resp:
            body = await resp.text()
            if resp.status == 404 and not_found_as_none:
                return None
            if resp.status >= 400:
                raise SunoAPIError(resp.status, method, url, body)
            stripped = body.strip()
            if not stripped or stripped == "null":
                return None
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Suno API returned non-JSON data for {method} {url}: {body[:1000]}"
                ) from exc


def browser_token(now_ms=None):
    """Per-request Suno browser-token: JSON {token: btoa({timestamp: ms})}."""
    timestamp_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    payload = json.dumps({"timestamp": timestamp_ms}, separators=(",", ":"))
    encoded = base64.b64encode(payload.encode()).decode()
    return json.dumps({"token": encoded}, separators=(",", ":"))


def persist_device_id(value):
    """Remember the browser suno_device_id. Empty input is ignored."""
    value = str(value or "").strip()
    if not value:
        return ""
    DEVICE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEVICE_ID_FILE.write_text(value + "\n", encoding="utf-8")
    return value


def device_id_from_cookie(cookie):
    return _cookie_value(cookie, "suno_device_id")


def device_id(cookie=""):
    """Prefer the browser suno_device_id cookie; otherwise reuse the saved UUID."""
    configured = os.getenv("SUNO_DEVICE_ID", "").strip()
    if configured:
        return configured
    from_cookie = device_id_from_cookie(cookie)
    if from_cookie:
        persist_device_id(from_cookie)
        return from_cookie
    try:
        saved = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        saved = ""
    if saved:
        return saved
    return persist_device_id(str(uuid.uuid4()))


def web_client_headers(cookie=""):
    """Headers the suno.com SPA sends with studio-api and CDN requests."""
    return {
        "device-id": device_id(cookie),
        "browser-token": browser_token(),
    }


async def get_feed(ids, token, cookie=None):
    cookie = cookie if cookie is not None else os.getenv("COOKIE", "")
    headers = {
        **_auth_headers(token, cookie=cookie),
        **web_client_headers(cookie),
    }
    api_url = f"{BASE_URL}/api/feed/?ids={ids}"
    response = await fetch(api_url, headers, method="GET")
    return response


async def generate_music(data, token, cookie=None):
    cookie = cookie if cookie is not None else os.getenv("COOKIE", "")
    configured_path = os.getenv("SUNO_GENERATE_PATH", "").strip()
    if configured_path:
        generate_path = configured_path
    elif str(data.get("mv") or "").lower() == "chirp-fenix":
        generate_path = WEB_GENERATE_PATH
    else:
        generate_path = LEGACY_GENERATE_PATH
    if not generate_path.startswith("/"):
        generate_path = f"/{generate_path}"
    is_legacy = generate_path.rstrip("/") == LEGACY_GENERATE_PATH.rstrip("/")
    if is_legacy:
        # Preserve the original repository's successful v3 request shape:
        # text/plain JSON body, bearer JWT only, and its browser headers.
        headers = {
            **_auth_headers(token),
            **LEGACY_GENERATION_HEADERS,
        }
    else:
        headers = _auth_headers(token, cookie=cookie)
    api_url = f"{BASE_URL.rstrip('/')}{generate_path}"
    cookie_names = _cookie_names(cookie)
    logger.info(
        "Suno generation request endpoint=%s model=%s transport=%s "
        "cookie_forwarded=%s cookie_count=%s has_client_cookie=%s",
        generate_path,
        data.get("mv"),
        "legacy-original" if is_legacy else "web",
        not is_legacy and bool(cookie),
        len(cookie_names),
        any(
            name == "__client"
            or (name.startswith("__client_") and not name.startswith("__client_uat"))
            for name in cookie_names
        ),
    )
    response = await fetch(
        api_url,
        headers,
        data,
        merge_common_headers=not is_legacy,
    )
    return response


async def generate_lyrics(prompt, token):
    headers = _auth_headers(token)
    api_url = f"{BASE_URL}/api/generate/lyrics/"
    data = {"prompt": prompt}
    return await fetch(api_url, headers, data)


async def get_lyrics(lid, token):
    headers = _auth_headers(token)
    api_url = f"{BASE_URL}/api/generate/lyrics/{lid}"
    return await fetch(api_url, headers, method="GET")


async def request_wav_convert(clip_id, token, cookie=None):
    """Start the suno.com Download .wav job. 2xx body may be empty."""
    cookie = cookie if cookie is not None else os.getenv("COOKIE", "")
    headers = {
        **_auth_headers(token, cookie=cookie),
        **web_client_headers(cookie),
    }
    api_url = f"{BASE_URL.rstrip('/')}/api/gen/{clip_id}/convert_wav/"
    logger.info(
        "Suno WAV convert clip_id=%s has_device_id=%s",
        clip_id,
        bool(device_id(cookie)),
    )
    return await fetch(api_url, headers, method="POST")


async def get_wav_file(clip_id, token, cookie=None):
    """Pending render is None; ready payload has wav_file_url."""
    cookie = cookie if cookie is not None else os.getenv("COOKIE", "")
    headers = {
        **_auth_headers(token, cookie=cookie),
        **web_client_headers(cookie),
    }
    api_url = f"{BASE_URL.rstrip('/')}/api/gen/{clip_id}/wav_file/"
    return await fetch(api_url, headers, method="GET", not_found_as_none=True)


def wav_file_url(payload):
    if not isinstance(payload, dict):
        return ""
    value = payload.get("wav_file_url")
    if isinstance(value, dict):
        value = value.get("url") or value.get("wav_file_url")
    return str(value or "").strip()


async def get_credits(token):
    if not token:
        raise RuntimeError("Suno authentication token is not available")
    headers = _auth_headers(token)
    api_url = f"{BASE_URL}/api/billing/info/"
    response = await fetch(api_url, headers, method="GET")
    return {
        "credits_left": response.get("total_credits_left", response.get("credits_left")),
        "period": response.get("period"),
        "monthly_limit": response.get("monthly_limit"),
        "monthly_usage": response.get("monthly_usage"),
    }


def _auth_headers(token, cookie=""):
    """Bearer (+ Cookie). A minted device-id/browser-token pair fails generate; those go on feed/CDN only."""
    if not token:
        raise RuntimeError("Suno authentication token is not available")
    headers = {"Authorization": f"Bearer {token}"}
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _cookie_value(cookie, name):
    for part in str(cookie or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value.strip()
    return ""


def _cookie_names(cookie):
    names = []
    for part in str(cookie or "").split(";"):
        name, separator, _ = part.strip().partition("=")
        if separator and name:
            names.append(name)
    return names
