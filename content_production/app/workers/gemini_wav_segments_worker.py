from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.services.ffmpeg_render import FFmpegRenderError, run_ffmpeg, which_ffmpeg
from app.services.ffprobe_audio import FfprobeError, ffprobe_duration_seconds
from app.services.gemini_audio_segments import (
    GeminiAudioSegmentsError,
    gemini_segment_audio_file,
)
from app.services.gemini_image_client import (
    GeminiImageApiError,
    api_aspect_ratio_from_resolution,
    gemini_generate_image,
)
from app.services.gemini_image_model_catalog import GEMINI_IMAGE_MODEL_PRESET_IDS


class GeminiWavSegmentsWorker(CancellableQThread):
    log_line = Signal(str)
    progress = Signal(int, str)  # percent 0–100, message
    updated = Signal(object)  # list[dict[str, Any]] incremental update
    succeeded = Signal(object)  # list[dict[str, Any]]
    failed = Signal(str)

    def __init__(
        self,
        *,
        wav_path: Path,
        api_key: str,
        model: str,
        generate_images: bool,
        image_api_key: str,
        image_model: str,
        resolution: str,
        project_parent: Path,
        reference_lyrics: str = "",
    ) -> None:
        super().__init__()
        self._wav_path = wav_path
        self._api_key = api_key
        self._model = model
        self._reference_lyrics = (reference_lyrics or "").strip()
        self._generate_images = generate_images
        self._image_api_key = image_api_key
        self._image_model = image_model
        self._resolution = resolution
        self._project_parent = project_parent

    def _image_ext(self, mime: str) -> str:
        m = mime.lower()
        if "jpeg" in m or "jpg" in m:
            return ".jpg"
        if "webp" in m:
            return ".webp"
        return ".png"

    def _image_prompt_for_segment(self, seg: dict[str, Any]) -> str:
        prompt = str(seg.get("image_prompt", seg.get("narration", ""))).strip()
        label = str(seg.get("label", "")).strip()
        core = prompt or label or "음악 구간의 분위기를 담은 장면"
        return (
            "한국어 음악 영상 배경 이미지를 생성하세요.\n"
            "요구: 텍스트/자막/워터마크/로고 금지, 배경용 단일 장면.\n"
            "중요: 설명 문장만 주지 말고 반드시 이미지 1장을 생성하세요.\n"
            f"구간 설명: {core}\n"
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
            self.log_line.emit(f"Gemini 구간 분석 시작: {self._wav_path.name}")
            self.progress.emit(10, "구간 분석 준비")
            audio_duration_sec = 0.0
            try:
                audio_duration_sec = float(ffprobe_duration_seconds(self._wav_path.resolve()))
            except (FfprobeError, OSError, ValueError):
                audio_duration_sec = 0.0
            if audio_duration_sec > 0:
                self.log_line.emit(f"오디오 길이: {audio_duration_sec:.2f}초")
            if self._reference_lyrics:
                self.log_line.emit(
                    f"원곡 가사 참고: {len(self._reference_lyrics)}자 (구간·이미지 프롬프트 생성에 활용)"
                )
            ffmpeg = which_ffmpeg()
            with tempfile.TemporaryDirectory(prefix="gemini_wav_segments_") as td:
                mp3 = Path(td) / "audio_preview.mp3"
                cmd = [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(self._wav_path.resolve()),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-b:a",
                    "64k",
                    str(mp3.resolve()),
                ]
                run_ffmpeg(cmd, timeout_sec=1800.0)
                self.progress.emit(25, "오디오 변환 완료")
                self.progress.emit(40, "Gemini 구간 분석")
                segs = gemini_segment_audio_file(
                    audio_path=mp3,
                    mime_type="audio/mpeg",
                    api_key=self._api_key,
                    model=self._model,
                    reference_lyrics=self._reference_lyrics,
                    audio_duration_sec=audio_duration_sec,
                    timeout_sec=240.0,
                )
            self.updated.emit([dict(seg) for seg in segs])
            if self._generate_images:
                images_root = self._project_parent / "images" / "wav_segments" / self._wav_path.stem
                images_root.mkdir(parents=True, exist_ok=True)
                ar = api_aspect_ratio_from_resolution(self._resolution)
                model_candidates = self._image_models_to_try()
                for i, seg in enumerate(segs, start=1):
                    self.check_cancelled()
                    prompt = self._image_prompt_for_segment(seg)
                    raw: bytes | None = None
                    mime: str = "image/png"
                    last_err = ""
                    for m in model_candidates:
                        try:
                            raw, mime = gemini_generate_image(
                                self._image_api_key,
                                m,
                                prompt=prompt,
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
                    rel = f"images/wav_segments/{self._wav_path.stem}/seg_{i:03d}{self._image_ext(mime)}"
                    (self._project_parent / rel).write_bytes(raw)
                    seg["image_relpath"] = rel
                    self.log_line.emit(f"구간 {i}: 이미지 저장됨 → {rel}")
                    self.updated.emit([dict(s) for s in segs])
            self.log_line.emit(f"Gemini 구간 분석 완료: {len(segs)}개")
            self.progress.emit(100, f"구간 {len(segs)}개")
            self.succeeded.emit(segs)
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
        except (GeminiAudioSegmentsError, GeminiImageApiError, FFmpegRenderError, OSError, RuntimeError, ValueError) as e:
            self.failed.emit(str(e))
        except Exception as e:  # pragma: no cover
            self.failed.emit(f"예상치 못한 오류: {e}")
