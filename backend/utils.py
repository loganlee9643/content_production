import json
import logging
import os
import time

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
            if resp.status >= 400:
                raise SunoAPIError(resp.status, method, url, body)
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Suno API returned non-JSON data for {method} {url}: {body[:1000]}"
                ) from exc


async def get_feed(ids, token):
    headers = _auth_headers(token)
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
    if not token:
        raise RuntimeError("Suno authentication token is not available")
    headers = {"Authorization": f"Bearer {token}"}
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _cookie_names(cookie):
    names = []
    for part in str(cookie or "").split(";"):
        name, separator, _ = part.strip().partition("=")
        if separator and name:
            names.append(name)
    return names
