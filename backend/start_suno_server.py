from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from auth_capture import (
    SunoAuth,
    SunoAuthError,
    capture_auth_with_browser,
    load_auth,
    save_auth,
    validate_auth,
)


ROOT = Path(__file__).resolve().parent
AUTH_DIR = ROOT / ".auth"
AUTH_FILE = AUTH_DIR / "suno-auth.json"
PROFILE_DIR = AUTH_DIR / "native-browser-profile"


def _configure_logging() -> Path:
    log_dir = Path(os.getenv("SUNO_LOG_DIR", ROOT / "logs")).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "suno-backend.log"
    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(
        isinstance(existing, RotatingFileHandler)
        and Path(existing.baseFilename) == log_path
        for existing in root_logger.handlers
    ):
        root_logger.addHandler(handler)
    if not any(
        isinstance(existing, logging.StreamHandler)
        and not isinstance(existing, logging.FileHandler)
        and getattr(existing, "_suno_console_handler", False)
        for existing in root_logger.handlers
    ):
        console_handler._suno_console_handler = True
        root_logger.addHandler(console_handler)
    return log_path


def _load_content_production_settings() -> None:
    if os.name != "nt":
        return
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\ContentProduction\ContentProductionApp\gemini",
        ) as key:
            if not os.getenv("GEMINI_API_KEY", "").strip():
                api_key, _ = winreg.QueryValueEx(key, "api_key")
                if str(api_key).strip():
                    os.environ["GEMINI_API_KEY"] = str(api_key).strip()
            if not os.getenv("GEMINI_MODEL", "").strip():
                try:
                    model, _ = winreg.QueryValueEx(key, "model")
                except FileNotFoundError:
                    model = ""
                if str(model).strip():
                    os.environ["GEMINI_MODEL"] = str(model).strip()
    except (FileNotFoundError, OSError):
        return


def _load_or_capture_auth(
    force_login: bool,
    timeout: float,
    auth_source: str = "auto",
) -> SunoAuth:
    if not force_login:
        load_dotenv(ROOT / ".env", override=True)
        env_auth = SunoAuth(
            session_id=os.getenv("SESSION_ID", "").strip(),
            cookie=os.getenv("COOKIE", "").strip(),
            captured_at=0.0,
        )
        if auth_source in {"auto", "env"} and validate_auth(env_auth):
            print("Using Suno login from .env.")
            return env_auth
        if auth_source == "env":
            raise SunoAuthError("Suno login in .env is not valid.")

        auth = load_auth(AUTH_FILE) if auth_source in {"auto", "saved"} else None
        if auth is not None:
            print("Checking saved Suno login...")
            if validate_auth(auth):
                print("Saved Suno login is valid.")
                return auth
            print("Saved Suno login has expired.")
        if auth_source == "saved":
            raise SunoAuthError("Saved Suno login is missing or not valid.")

    auth = capture_auth_with_browser(
        profile_dir=PROFILE_DIR,
        timeout_sec=timeout,
        wait_for_browser_close=force_login,
    )
    if not validate_auth(auth):
        raise SunoAuthError("Captured Suno login could not be validated.")
    save_auth(AUTH_FILE, auth)
    print(f"Suno login saved to: {AUTH_FILE}")
    return auth


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open Suno login when needed and start the local FastAPI server."
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--login-timeout", type=float, default=600.0)
    parser.add_argument(
        "--auth-source",
        choices=("auto", "env", "saved"),
        default="auto",
        help=(
            "Authentication source: auto prefers .env, env only uses .env, "
            "saved only uses .auth/suno-auth.json."
        ),
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        help="Open the dedicated browser even when saved authentication is valid.",
    )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="Capture and validate login without starting FastAPI.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env", override=True)
    _load_content_production_settings()
    log_path = _configure_logging()
    try:
        auth = _load_or_capture_auth(
            args.force_login,
            args.login_timeout,
            args.auth_source,
        )
    except SunoAuthError as exc:
        print(f"Suno login failed: {exc}")
        return 1

    os.environ["SESSION_ID"] = auth.session_id
    os.environ["COOKIE"] = auth.cookie
    os.environ.setdefault("BASE_URL", "https://studio-api.prod.suno.com")
    os.environ.setdefault("SUNO_AUTH_BASE_URL", "https://auth.suno.com")
    os.environ.setdefault("SUNO_CLERK_API_VERSION", "2025-11-10")
    os.environ.setdefault("SUNO_CLERK_JS_VERSION", "5.117.0")
    cookie_count = sum(
        1
        for part in auth.cookie.split(";")
        if part.strip().partition("=")[1]
    )
    print(
        "Active Suno auth: "
        f"session={auth.session_id[:12]}... cookies={cookie_count}"
    )

    if args.login_only:
        print("Suno login is ready.")
        return 0

    print(f"Starting Suno FastAPI server: http://{args.host}:{args.port}")
    print(f"Backend log file: {log_path}")
    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
