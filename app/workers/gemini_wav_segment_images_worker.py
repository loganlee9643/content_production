from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.services.gemini_image_client import (
    GeminiImageApiError,
    api_aspect_ratio_from_resolution,
    gemini_generate_image,
)
from app.services.gemini_image_model_catalog import GEMINI_IMAGE_MODEL_PRESET_IDS


def _image_ext(mime: str) -> str:
    m = mime.lower()
    if "jpeg" in m or "jpg" in m:
        return ".jpg"
    if "webp" in m:
        return ".webp"
    return ".png"


class GeminiWavSegmentImagesWorker(CancellableQThread):
    log_line = Signal(str)
    progress = Signal(int, str)  # percent 0–100, message
    updated = Signal(object)  # list[dict[str, Any]] incremental update
    succeeded = Signal(object)  # list[dict[str, Any]]
    failed = Signal(str)

    def __init__(
        self,
        *,
        wav_path: Path,
        segments: list[dict[str, Any]],
        api_key: str,
        image_model: str,
        resolution: str,
        project_parent: Path,
    ) -> None:
        super().__init__()
        self._wav_path = wav_path
        self._segments = segments
        self._api_key = api_key.strip()
        self._image_model = image_model.strip()
        self._resolution = resolution.strip()
        self._project_parent = project_parent

    def _prompt(self, seg: dict[str, Any]) -> str:
        scene = str(seg.get("image_prompt", seg.get("narration", ""))).strip()
        return (
            "한국어 음악 영상용 배경 이미지를 생성하세요.\n"
            "요구: 텍스트/자막/워터마크/로고 금지, 단일 장면 배경.\n"
            "중요: 설명 문장만 주지 말고 반드시 이미지 1장을 생성하세요.\n"
            f"장면 설명: {scene or '음악 분위기에 맞는 풍경'}\n"
        )

    def _image_models_to_try(self) -> list[str]:
        first = (self._image_model or "").strip()
        models: list[str] = []
        if first:
            models.append(first)
        for m in GEMINI_IMAGE_MODEL_PRESET_IDS:
            if m not in models:
                models.append(m)
        return models

    def run(self) -> None:
        try:
            self.check_cancelled()
            if not self._wav_path.is_file():
                raise RuntimeError(f"WAV 파일이 없습니다: {self._wav_path}")
            if not self._segments:
                raise RuntimeError("생성할 구간이 없습니다.")
            out = [dict(seg) for seg in self._segments]
            target_dir = self._project_parent / "images" / "wav_segments" / self._wav_path.stem
            target_dir.mkdir(parents=True, exist_ok=True)
            ar = api_aspect_ratio_from_resolution(self._resolution)
            model_candidates = self._image_models_to_try()
            total = len(out)
            for i, seg in enumerate(out, start=1):
                self.check_cancelled()
                pct = int(round((i / total) * 100.0)) if total > 0 else 0
                msg = f"구간별 이미지 자동 생성 ({i}/{total})"
                self.progress.emit(pct, msg)
                self.log_line.emit(f"구간 이미지 생성 {i}/{total}...")
                raw: bytes | None = None
                mime: str = "image/png"
                last_err = ""
                for m in model_candidates:
                    try:
                        raw, mime = gemini_generate_image(
                            self._api_key,
                            m,
                            prompt=self._prompt(seg),
                            aspect_ratio=ar,
                        )
                        if m != self._image_model:
                            self.log_line.emit(f"구간 {i}: 이미지 모델 fallback 사용 → {m}")
                        break
                    except GeminiImageApiError as e:
                        last_err = str(e)
                if raw is None:
                    self.log_line.emit(f"구간 {i}: 이미지 생성 실패(스킵) — {last_err}")
                    continue
                rel = f"images/wav_segments/{self._wav_path.stem}/seg_{i:03d}{_image_ext(mime)}"
                (self._project_parent / rel).write_bytes(raw)
                seg["image_relpath"] = rel
                self.updated.emit([dict(s) for s in out])
            self.progress.emit(100, "구간별 이미지 생성 완료")
            self.succeeded.emit(out)
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
        except (GeminiImageApiError, OSError, RuntimeError, ValueError) as e:
            self.failed.emit(str(e))
        except Exception as e:  # pragma: no cover
            self.failed.emit(f"예상치 못한 오류: {e}")
