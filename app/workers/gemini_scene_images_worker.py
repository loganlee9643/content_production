from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.models.storyboard import Scene
from app.services.gemini_image_client import (
    GeminiImageApiError,
    api_aspect_ratio_from_resolution,
    gemini_generate_image,
)

logger = logging.getLogger(__name__)


def _aspect_phrase(resolution: str) -> str:
    r = (resolution or "").strip()
    if r == "1080x1920":
        return "세로형(약 9:16) 영상 배경에 맞게."
    if r == "1280x720":
        return "가로형 16:9(1280x720급) 영상 배경에 맞게."
    return "가로형 16:9(1920x1080급) 영상 배경에 맞게."


def _build_image_prompt(*, visual: str, narration: str, resolution: str) -> str:
    core = visual.strip() or narration.strip()
    asp = _aspect_phrase(resolution)
    return (
        "YouTube 한국어 영상용 정적 배경·자료화면 이미지를 생성하세요.\n"
        "요구: 사진/일러스트 품질, 화면에 글자·자막·워터마크·로고는 넣지 마세요.\n"
        f"화면 비율: {asp}\n\n"
        f"장면 설명:\n{core}\n"
    )


def _suffix_for_mime(mime: str) -> str:
    m = mime.lower()
    if "jpeg" in m or "jpg" in m:
        return ".jpg"
    if "webp" in m:
        return ".webp"
    return ".png"


class GeminiSceneImagesWorker(CancellableQThread):
    log_line = Signal(str)
    progress = Signal(int, int)
    succeeded = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        *,
        scenes: list[Scene],
        project_parent: Path,
        api_key: str,
        image_model: str,
        resolution: str,
        pause_sec: float = 0.35,
    ) -> None:
        super().__init__()
        self._scenes = scenes
        self._parent = project_parent
        self._api_key = api_key.strip()
        self._image_model = image_model.strip()
        self._resolution = resolution.strip()
        self._pause_sec = pause_sec

    def run(self) -> None:
        try:
            todo = [s for s in self._scenes if s.visual_prompt_ko.strip() or s.narration_ko.strip()]
            if not todo:
                self.failed.emit("비주얼 프롬프트 또는 나레이션이 있는 씬이 없습니다.")
                return

            total = len(todo)
            out_pairs: list[tuple[int, str]] = []
            images_dir = self._parent / "images"
            images_dir.mkdir(parents=True, exist_ok=True)

            for i, s in enumerate(todo, start=1):
                self.check_cancelled()
                self.progress.emit(i, total)
                prompt = _build_image_prompt(
                    visual=s.visual_prompt_ko,
                    narration=s.narration_ko,
                    resolution=self._resolution,
                )
                self.log_line.emit(f"씬 {s.scene_id}: Gemini 이미지 요청 중…")
                logger.info("씬 이미지 생성 scene_id=%s model=%r", s.scene_id, self._image_model)
                ar = api_aspect_ratio_from_resolution(self._resolution)
                raw, mime = gemini_generate_image(
                    self._api_key,
                    self._image_model,
                    prompt=prompt,
                    aspect_ratio=ar,
                )
                ext = _suffix_for_mime(mime)
                rel = f"images/scene_{s.scene_id:03d}{ext}"
                path = self._parent / rel
                path.write_bytes(raw)
                out_pairs.append((s.scene_id, rel))
                self.log_line.emit(f"씬 {s.scene_id}: 저장됨 → {rel}")
                if self._pause_sec > 0 and i < total:
                    time.sleep(self._pause_sec)

            self.succeeded.emit(out_pairs)
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
        except GeminiImageApiError as e:
            logger.warning("씬 이미지 API 오류: %s", e)
            self.failed.emit(str(e))
        except OSError as e:
            logger.warning("씬 이미지 파일 쓰기 실패: %s", e, exc_info=True)
            self.failed.emit(f"파일 저장 실패: {e}")
        except Exception as e:
            logger.exception("씬 이미지 예외")
            self.failed.emit(f"오류: {e}")
