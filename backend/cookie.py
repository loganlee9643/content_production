# -*- coding:utf-8 -*-

import os
import time
import json
from pathlib import Path
from threading import Thread

import requests

from utils import COMMON_HEADERS


AUTH_BASE_URL = os.getenv("SUNO_AUTH_BASE_URL", "https://auth.suno.com").rstrip("/")
CLERK_API_VERSION = os.getenv("SUNO_CLERK_API_VERSION", "2025-11-10")
CLERK_JS_VERSION = os.getenv("SUNO_CLERK_JS_VERSION", "5.117.0")


class SunoCookie:
    def __init__(self):
        self.cookie: dict[str, str] = {}
        self.session_id = None
        self.token = None

    def load_cookie(self, cookie_str):
        if not cookie_str:
            return
        for part in cookie_str.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                self.cookie[name] = value

    def get_cookie(self):
        return "; ".join(f"{name}={value}" for name, value in self.cookie.items())

    def set_session_id(self, session_id):
        self.session_id = session_id

    def get_session_id(self):
        return self.session_id

    def get_token(self):
        return self.token

    def set_token(self, token: str):
        self.token = token


suno_auth = SunoCookie()


def _initial_auth() -> tuple[str, str]:
    session_id = (os.getenv("SESSION_ID") or "").strip()
    cookie = (os.getenv("COOKIE") or "").strip()
    if session_id and cookie:
        return session_id, cookie

    auth_path = Path(__file__).resolve().parent / ".auth" / "suno-auth.json"
    if not auth_path.is_file():
        return session_id, cookie
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
        return (
            str(payload.get("session_id", "") or "").strip(),
            str(payload.get("cookie", "") or "").strip(),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return session_id, cookie


initial_session_id, initial_cookie = _initial_auth()
suno_auth.set_session_id(initial_session_id)
suno_auth.load_cookie(initial_cookie)


def update_token(suno_cookie: SunoCookie):
    cookie = suno_cookie.get_cookie()
    client_token = suno_cookie.cookie.get("__client")
    if client_token is None:
        client_token = next(
            (
                value
                for name, value in suno_cookie.cookie.items()
                if name.startswith("__client_") and not name.startswith("__client_uat")
            ),
            None,
        )
    if not cookie or client_token is None:
        raise RuntimeError(
            "COOKIE must include __client or __client_<suffix> from Suno request headers"
        )

    headers = {
        "cookie": cookie,
        "authorization": client_token,
        "accept": "application/json",
    }
    headers.update(COMMON_HEADERS)
    session_id = suno_cookie.get_session_id()
    params = {
        "__clerk_api_version": CLERK_API_VERSION,
        "_clerk_js_version": CLERK_JS_VERSION,
    }
    if not session_id:
        client_resp = requests.get(
            f"{AUTH_BASE_URL}/v1/client",
            params=params,
            headers=headers,
            timeout=30,
        )
        client_resp.raise_for_status()
        client_data = client_resp.json()
        response = client_data.get("response", client_data)
        sessions = response.get("sessions", []) if isinstance(response, dict) else []
        active = next(
            (
                item
                for item in sessions
                if isinstance(item, dict)
                and str(item.get("status", "")).lower() == "active"
            ),
            sessions[0] if sessions else None,
        )
        if not isinstance(active, dict) or not active.get("id"):
            raise RuntimeError("Could not find an active Suno session")
        session_id = str(active["id"])
        suno_cookie.set_session_id(session_id)

    resp = requests.post(
        url=f"{AUTH_BASE_URL}/v1/client/sessions/{session_id}/tokens",
        params=params,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("jwt")
    if not token:
        raise RuntimeError("Suno authentication response did not include a JWT")
    suno_cookie.set_token(token)


def keep_alive(suno_cookie: SunoCookie):
    last_error = ""
    while True:
        try:
            update_token(suno_cookie)
            last_error = ""
        except Exception as e:
            message = str(e)
            if message != last_error:
                print(f"Suno token refresh failed: {message}")
                last_error = message
        finally:
            time.sleep(45)


def start_keep_alive(suno_cookie: SunoCookie):
    t = Thread(target=keep_alive, args=(suno_cookie,), daemon=True)
    t.start()


try:
    update_token(suno_auth)
except Exception as exc:
    print(f"Initial Suno authentication failed: {exc}")
start_keep_alive(suno_auth)
