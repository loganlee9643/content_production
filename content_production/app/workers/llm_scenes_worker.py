from __future__ import annotations

import json
import logging
from typing import Literal

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.models.storyboard import Scene
from app.services.gemini_client import GeminiApiError, gemini_generate_content
from app.services.ollama_client import OllamaHttpError, ollama_chat
from app.services.scene_generation import (
    SYSTEM_SCENE_JSON_KO,
    build_user_message,
    parse_scenes_json_payload,
)

logger = logging.getLogger(__name__)

LlmProvider = Literal["ollama", "gemini"]


class LlmScenesWorker(CancellableQThread):
    log_line = Signal(str)
    succeeded = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        *,
        provider: LlmProvider,
        prompt_ko: str,
        target_minutes: int,
        resolution: str,
        fps: int,
        ollama_base_url: str = "",
        ollama_model: str = "",
        gemini_api_key: str = "",
        gemini_model: str = "",
    ) -> None:
        super().__init__()
        self._provider: LlmProvider = provider
        self._ollama_base_url = ollama_base_url.strip()
        self._ollama_model = ollama_model.strip()
        self._gemini_api_key = gemini_api_key.strip()
        self._gemini_model = gemini_model.strip()
        self._prompt_ko = prompt_ko
        self._target_minutes = target_minutes
        self._resolution = resolution
        self._fps = fps

    def run(self) -> None:
        try:
            self.check_cancelled()
            logger.info(
                "씬 생성 시작 provider=%s target_min=%s",
                self._provider,
                self._target_minutes,
            )
            user_msg = build_user_message(
                prompt_ko=self._prompt_ko,
                target_minutes=self._target_minutes,
                resolution=self._resolution,
                fps=self._fps,
            )
            if self._provider == "ollama":
                self.log_line.emit("Ollama에 씬 생성 요청 중…")
                messages = [
                    {"role": "system", "content": SYSTEM_SCENE_JSON_KO},
                    {"role": "user", "content": user_msg},
                ]
                base = self._ollama_base_url or "http://127.0.0.1:11434"
                content = ollama_chat(base, self._ollama_model, messages)
            else:
                self.log_line.emit("Gemini API에 씬 생성 요청 중…")
                content = gemini_generate_content(
                    self._gemini_api_key,
                    self._gemini_model,
                    system_instruction=SYSTEM_SCENE_JSON_KO,
                    user_text=user_msg,
                )

            self.log_line.emit("응답 수신, JSON 파싱 중…")
            scenes: list[Scene] = parse_scenes_json_payload(content)
            for s in scenes:
                if s.transition not in ("fade", "cut"):
                    s.transition = "fade"
            self.log_line.emit(f"씬 {len(scenes)}개 생성됨.")
            logger.info("씬 생성 완료 count=%s", len(scenes))
            self.succeeded.emit(scenes)
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
        except (OllamaHttpError, GeminiApiError) as e:
            logger.warning("씬 생성 API 오류: %s", e)
            self.failed.emit(str(e))
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("씬 생성 파싱 실패: %s", e, exc_info=True)
            self.failed.emit(f"응답 처리 실패: {e}")
        except Exception as e:
            logger.exception("씬 생성 예외")
            self.failed.emit(f"오류: {e}")
