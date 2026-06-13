from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests


AUTH_BASE_URL = "https://auth.suno.com"
CLERK_API_VERSION = "2025-11-10"
CLERK_JS_VERSION = "5.117.0"
SUNO_CREATE_URL = "https://suno.com/create"


class SunoAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class SunoAuth:
    session_id: str
    cookie: str
    captured_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "cookie": self.cookie,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SunoAuth":
        return cls(
            session_id=str(value.get("session_id", "")).strip(),
            cookie=str(value.get("cookie", "")).strip(),
            captured_at=float(value.get("captured_at", 0.0) or 0.0),
        )


def _cookie_pairs(cookie_header: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            pairs[name] = value
    return pairs


def _client_token(cookie_header: str) -> str:
    pairs = _cookie_pairs(cookie_header)
    if pairs.get("__client"):
        return pairs["__client"]
    for name, value in pairs.items():
        if name.startswith("__client_") and not name.startswith("__client_uat"):
            return value
    raise SunoAuthError("Suno authentication cookie was not found.")


def _headers(cookie_header: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": _client_token(cookie_header),
        "Cookie": cookie_header,
        "Origin": "https://suno.com",
        "Referer": "https://suno.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0.0.0 Safari/537.36"
        ),
    }


def _params() -> dict[str, str]:
    return {
        "__clerk_api_version": CLERK_API_VERSION,
        "_clerk_js_version": CLERK_JS_VERSION,
    }


def find_active_session(cookie_header: str) -> str:
    response = requests.get(
        f"{AUTH_BASE_URL}/v1/client",
        params=_params(),
        headers=_headers(cookie_header),
        timeout=30,
    )
    response.raise_for_status()
    return _active_session_id(response.json())


def _active_session_id(payload: Any) -> str:
    data = payload.get("response", payload) if isinstance(payload, dict) else payload
    sessions = data.get("sessions", []) if isinstance(data, dict) else []
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
        raise SunoAuthError("No active Suno session was found.")
    return str(active["id"])


def _is_clerk_client_response(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.netloc == "auth.suno.com" and parsed.path == "/v1/client"


def validate_auth(auth: SunoAuth) -> bool:
    if not auth.session_id or not auth.cookie:
        return False
    try:
        response = requests.post(
            f"{AUTH_BASE_URL}/v1/client/sessions/{auth.session_id}/tokens",
            params=_params(),
            headers=_headers(auth.cookie),
            timeout=30,
        )
        response.raise_for_status()
        return bool(response.json().get("jwt"))
    except (requests.RequestException, ValueError, SunoAuthError):
        return False


def load_auth(path: Path) -> SunoAuth | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return SunoAuth.from_dict(payload)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_auth(path: Path, auth: SunoAuth) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(auth.to_dict(), ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _cookie_header(cookies: list[dict[str, Any]]) -> str:
    preferred: dict[str, str] = {}
    for cookie in cookies:
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", "")).strip()
        domain = str(cookie.get("domain", "")).lower()
        if name and value and domain.endswith("suno.com"):
            preferred[name] = value
    return "; ".join(f"{name}={value}" for name, value in preferred.items())


def _browser_executable() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SunoAuthError("Google Chrome or Microsoft Edge was not found.")


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_debugger(port: int, process: subprocess.Popen[Any]) -> None:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + 30.0
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = requests.get(endpoint, timeout=1)
            response.raise_for_status()
            if response.json().get("webSocketDebuggerUrl"):
                return
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    exit_detail = (
        f" Browser launcher exit code: {process.returncode}."
        if process.poll() is not None
        else ""
    )
    raise SunoAuthError(
        f"Could not connect to the login browser.{exit_detail} {last_error}"
    )


def capture_auth_with_browser(
    *,
    profile_dir: Path,
    timeout_sec: float = 600.0,
    wait_for_browser_close: bool = False,
) -> SunoAuth:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SunoAuthError(
            "Playwright is not installed. Run: python -m pip install playwright"
        ) from exc

    profile_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(30.0, timeout_sec)
    port = _free_local_port()
    executable = _browser_executable()
    browser_profile_dir = profile_dir / executable.stem
    browser_profile_dir.mkdir(parents=True, exist_ok=True)
    browser_process = subprocess.Popen(
        [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={browser_profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            SUNO_CREATE_URL,
        ],
    )
    connected_browser = None
    try:
        _wait_for_debugger(port, browser_process)
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
            connected_browser = browser
            if not browser.contexts:
                raise SunoAuthError("The login browser did not expose a browser context.")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            captured_auth: list[SunoAuth] = []

            def capture_clerk_auth(response: Any) -> None:
                if captured_auth or not _is_clerk_client_response(response.url):
                    return
                try:
                    headers = response.request.all_headers()
                    cookie_header = str(headers.get("cookie", "")).strip()
                    if not cookie_header:
                        return
                    captured_auth.append(
                        SunoAuth(
                            session_id=_active_session_id(response.json()),
                            cookie=cookie_header,
                            captured_at=time.time(),
                        )
                    )
                except Exception:
                    return

            context.on("response", capture_clerk_auth)
            if "suno.com" not in page.url:
                page.goto(SUNO_CREATE_URL, wait_until="domcontentloaded", timeout=60000)
            else:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            if wait_for_browser_close:
                print("Review or complete Suno login in the opened browser window.")
                print("When ready, close that browser window to continue.")
            else:
                print("Complete Suno login in the opened Chrome or Edge window.")
                print("Authentication will be saved automatically after login is detected.")
            last_error = ""
            latest_auth: SunoAuth | None = None
            login_detected = False
            while time.monotonic() < deadline:
                if captured_auth:
                    latest_auth = captured_auth[0]
                if browser_process.poll() is not None or page.is_closed():
                    if latest_auth is not None:
                        return latest_auth
                    raise SunoAuthError(
                        "The login browser was closed before Suno login was detected."
                    )
                try:
                    cookies = context.cookies()
                    cookie_header = _cookie_header(cookies)
                    if cookie_header:
                        session_id = find_active_session(cookie_header)
                        latest_auth = SunoAuth(
                            session_id=session_id,
                            cookie=cookie_header,
                            captured_at=time.time(),
                        )
                except (
                    requests.RequestException,
                    ValueError,
                    SunoAuthError,
                    PlaywrightError,
                ) as exc:
                    last_error = str(exc)
                if latest_auth is not None:
                    if not wait_for_browser_close:
                        return latest_auth
                    if not login_detected:
                        print("Suno login detected. Close the browser window when ready.")
                        login_detected = True
                time.sleep(1)
            detail = f" Last error: {last_error}" if last_error else ""
            raise SunoAuthError(f"Suno login was not detected in time.{detail}")
    finally:
        if connected_browser is not None:
            try:
                connected_browser.close()
            except Exception:
                pass
        try:
            if browser_process.poll() is None:
                browser_process.terminate()
                browser_process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            if browser_process.poll() is None:
                browser_process.kill()
