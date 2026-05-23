"""콘솔(stderr) 로깅. 환경 변수 CONTENT_PRODUCTION_LOG 로 레벨 조절.

- 기본: DEBUG (터미널에서 `python main.py` 실행 시 상세 로그)
- CONTENT_PRODUCTION_LOG=INFO | WARNING | ERROR
- CONTENT_PRODUCTION_LOG=off  → 콘솔 상세 로그 최소화(루트 WARNING)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_configured = False


def configure_console_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    raw = os.environ.get("CONTENT_PRODUCTION_LOG", "DEBUG").strip().lower()
    if raw in ("0", "off", "none"):
        logging.basicConfig(level=logging.WARNING, force=True)
        return

    level_name = raw.upper()
    level = getattr(logging, level_name, logging.DEBUG)

    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    datefmt = "%H:%M:%S"
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    # 파일 로그: 기본 logs/content_production.log
    # 필요 시 CONTENT_PRODUCTION_LOG_FILE 로 경로 지정 가능
    file_path_raw = os.environ.get("CONTENT_PRODUCTION_LOG_FILE", "").strip()
    if file_path_raw:
        file_path = Path(file_path_raw).expanduser().resolve()
    else:
        file_path = (Path.cwd() / "logs" / "content_production.log").resolve()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.setLevel(level)
    logging.getLogger(__name__).info("로그 파일 경로: %s", file_path)
