from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.services.ffmpeg_render import FFmpegRenderError, run_ffmpeg, which_ffmpeg
from app.services.ffprobe_audio import FfprobeError, ffprobe_duration_seconds
from app.services.gemini_audio_segments import GeminiAudioSegmentsError, gemini_segment_audio_file
from app.services.gemini_image_client import (
    GeminiImageApiError,
    api_aspect_ratio_from_resolution,
    gemini_generate_image,
)
from app.services.gemini_image_model_catalog import GEMINI_IMAGE_MODEL_PRESET_IDS
from app.services.gemini_model_catalog import DEFAULT_GEMINI_MODEL
from app.services.srt_build import build_srt_from_timed_lines
from app.services.subtitle_timing import (
    apply_subtitle_timing_adjustments,
    drop_overlapping_cues,
    prepend_intro_title_cue,
)
from app.services.subtitle_vocal_align import retime_subtitles_with_vocal_audio
from app.services.subtitle_generation_log import log_subtitle_generation_options
from app.services.stt_transcribe import (
    SttTranscribeError,
    refine_timed_lines_with_reference_lyrics,
    transcribe_wav_sentences,
)

# 음악 분석 4단계 가중치(합 100): 인트로·자막·구간·이미지
_STEP_WEIGHTS = (5, 35, 20, 40)
logger = logging.getLogger(__name__)


def _image_ext(mime: str) -> str:
    m = mime.lower()
    if "jpeg" in m or "jpg" in m:
        return ".jpg"
    if "webp" in m:
        return ".webp"
    return ".png"


def _normalize_gemini_model(model: str) -> str:
    m = (model or "").strip()
    if m.startswith("models/"):
        m = m[len("models/") :]
    if "image" in m.lower():
        return DEFAULT_GEMINI_MODEL
    return m or DEFAULT_GEMINI_MODEL


class MusicAnalysisWorker(CancellableQThread):
    log_line = Signal(str)
    progress = Signal(int, str)  # percent 0–100, message
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        wav_path: Path,
        reference_lyrics: str,
        project_parent: Path,
        output_relpath: str,
        max_line_chars: int,
        intro_title: str,
        intro_title_duration_sec: float,
        intro_skip_sec: float,
        subtitle_offset_sec: float,
        gemini_api_key: str,
        gemini_model: str,
        gemini_image_model: str,
        resolution: str,
        vocal_retime_with_lyrics: bool = True,
        wav_segments: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._wav_path = wav_path
        self._reference_lyrics = (reference_lyrics or "").strip()
        self._project_parent = project_parent
        self._output_relpath = (output_relpath or "").strip().replace("\\", "/")
        self._max_line_chars = max(8, int(max_line_chars))
        self._intro_title = (intro_title or "").strip()
        self._intro_title_duration_sec = max(0.0, float(intro_title_duration_sec or 0.0))
        self._intro_skip_sec = max(0.0, float(intro_skip_sec or 0.0))
        self._subtitle_offset_sec = float(subtitle_offset_sec or 0.0)
        self._gemini_api_key = (gemini_api_key or "").strip()
        self._gemini_model = _normalize_gemini_model(gemini_model)
        self._gemini_image_model = (gemini_image_model or "").strip()
        self._resolution = resolution.strip()
        self._vocal_retime_with_lyrics = bool(vocal_retime_with_lyrics)
        self._wav_segments = wav_segments
        self._enable_timing_corrections = (
            (os.environ.get("CONTENT_PRODUCTION_ENABLE_TIMING_CORRECTIONS") or "")
            .strip()
            .lower()
            in ("1", "true", "yes", "on")
        )

    def _progress_percent(
        self,
        step: int,
        *,
        at_end: bool = False,
        sub_index: int = 0,
        sub_total: int = 0,
    ) -> int:
        step = max(1, min(4, int(step)))
        completed = sum(_STEP_WEIGHTS[: step - 1])
        weight = _STEP_WEIGHTS[step - 1]
        if step < 4:
            return min(100, completed + (weight if at_end else 0))
        if sub_total <= 0:
            return min(100, completed)
        frac = max(0.0, min(1.0, float(sub_index) / float(sub_total)))
        return min(100, completed + int(round(frac * weight)))

    def _emit_progress(
        self,
        step: int,
        message: str,
        *,
        at_end: bool = False,
        sub_index: int = 0,
        sub_total: int = 0,
    ) -> None:
        pct = self._progress_percent(
            step, at_end=at_end, sub_index=sub_index, sub_total=sub_total
        )
        self.progress.emit(pct, message)
        self.log_line.emit(message)

    def _log_cue_dump(self, title: str, lines: list[dict[str, Any]]) -> None:
        logger.info("%s (%s개)", title, len(lines))
        for i, ln in enumerate(lines):
            try:
                st = float(ln.get("start_sec", 0.0))
                en = float(ln.get("end_sec", 0.0))
            except (TypeError, ValueError):
                continue
            txt = str(ln.get("text", "")).strip()
            logger.info("%s cue[%02d] %.2f->%.2f %r", title, i, st, en, txt)

    def _run_stt(self, audio_duration_sec: float) -> str:
        if not self._reference_lyrics:
            raise SttTranscribeError("원곡 가사가 비어 있습니다.")
        log_subtitle_generation_options(
            wav_path=self._wav_path,
            language="ko",
            refine_mode="lyrics",
            output_relpath=self._output_relpath,
            max_line_chars=self._max_line_chars,
            intro_skip_sec=self._intro_skip_sec,
            subtitle_offset_sec=self._subtitle_offset_sec,
            intro_title=self._intro_title,
            intro_title_duration_sec=self._intro_title_duration_sec,
            vocal_retime_with_lyrics=self._vocal_retime_with_lyrics,
            enable_timing_corrections=self._enable_timing_corrections,
            reference_lyrics_len=len(self._reference_lyrics),
            wav_segments_count=len(self._wav_segments or []),
            gemini_model=self._gemini_model,
            pure_stt_mode=False,
        )
        lines = transcribe_wav_sentences(self._wav_path, language="ko")
        self.log_line.emit(f"STT 추출 완료: {len(lines)}개")
        self._log_cue_dump("STT 원본", lines)
        stt_raw = [dict(ln) for ln in lines]
        lines = refine_timed_lines_with_reference_lyrics(
            lines=lines,
            reference_lyrics=self._reference_lyrics,
            api_key=self._gemini_api_key,
            model=self._gemini_model,
        )
        self._log_cue_dump("LLM 원곡 교정", lines)
        if self._vocal_retime_with_lyrics and self._reference_lyrics:
            self.log_line.emit("STT 기반 보컬 구간 자막 타이밍 시작...")
            lines = retime_subtitles_with_vocal_audio(
                audio_path=self._wav_path,
                mime_type="audio/wav",
                reference_lyrics=self._reference_lyrics,
                stt_hints=stt_raw,
                audio_duration_sec=audio_duration_sec,
                api_key=self._gemini_api_key,
                model=self._gemini_model,
                wav_segments=self._wav_segments,
                use_stt_timing=True,
                refined_lines=lines,
                enable_timing_corrections=self._enable_timing_corrections,
                debug_log=None,
            )
            self._log_cue_dump("보컬 타이밍 후", lines)
        if self._intro_skip_sec > 0 or self._subtitle_offset_sec != 0.0:
            lines = apply_subtitle_timing_adjustments(
                lines,
                intro_skip_sec=self._intro_skip_sec,
                offset_sec=self._subtitle_offset_sec,
                audio_duration_sec=audio_duration_sec,
            )
            lines = drop_overlapping_cues(lines)
        if self._intro_title:
            lines = prepend_intro_title_cue(
                lines,
                title=self._intro_title,
                duration_sec=self._intro_title_duration_sec,
                intro_skip_sec=self._intro_skip_sec,
                audio_duration_sec=audio_duration_sec,
            )
            lines = drop_overlapping_cues(lines)
        self._log_cue_dump("최종전", lines)
        timed: list[dict[str, Any]] = []
        for ln in lines:
            try:
                st = float(ln.get("start_sec", 0.0))
                en = float(ln.get("end_sec", 0.0))
            except (TypeError, ValueError):
                continue
            txt = str(ln.get("text", "")).strip()
            if en <= st or not txt:
                continue
            timed.append({"start_sec": st, "end_sec": en, "text": txt})
        if not timed:
            raise SttTranscribeError("유효한 자막 구간이 없습니다.")
        if not self._output_relpath:
            raise SttTranscribeError("자막 저장 경로가 비어 있습니다.")
        body = build_srt_from_timed_lines(timed, max_line_chars=self._max_line_chars)
        out_abs = (self._project_parent / self._output_relpath).resolve()
        out_abs.parent.mkdir(parents=True, exist_ok=True)
        out_abs.write_text(body, encoding="utf-8")
        self.log_line.emit(f"자막 SRT 저장: {self._output_relpath}")
        return self._output_relpath

    def _run_segments(self, audio_duration_sec: float) -> list[dict[str, Any]]:
        ffmpeg = which_ffmpeg()
        with tempfile.TemporaryDirectory(prefix="music_segments_") as td:
            mp3 = Path(td) / "audio.mp3"
            run_ffmpeg(
                [
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
                ],
                timeout_sec=1800.0,
            )
            raw = gemini_segment_audio_file(
                audio_path=mp3,
                mime_type="audio/mpeg",
                api_key=self._gemini_api_key,
                model=self._gemini_model,
                reference_lyrics=self._reference_lyrics,
                audio_duration_sec=audio_duration_sec,
            )
        segs: list[dict[str, Any]] = []
        for item in raw:
            try:
                st = float(item["start_sec"])
                en = float(item["end_sec"])
            except (TypeError, ValueError, KeyError):
                continue
            if en <= st:
                continue
            prompt = str(
                item.get("image_prompt", item.get("narration", item.get("label", "")))
            ).strip()
            segs.append(
                {
                    "start_sec": st,
                    "end_sec": en,
                    "image_prompt": prompt,
                    "transition": str(item.get("transition", "fade")).strip() or "fade",
                    "image_relpath": "",
                }
            )
        segs.sort(key=lambda x: float(x["start_sec"]))
        if not segs:
            raise GeminiAudioSegmentsError("유효한 구간이 없습니다.")
        return segs

    def _image_models_to_try(self) -> list[str]:
        models: list[str] = []
        if self._gemini_image_model:
            models.append(self._gemini_image_model)
        for m in GEMINI_IMAGE_MODEL_PRESET_IDS:
            if m not in models:
                models.append(m)
        return models

    def _run_segment_images(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = [dict(seg) for seg in segments]
        target_dir = self._project_parent / "images" / "wav_segments" / self._wav_path.stem
        target_dir.mkdir(parents=True, exist_ok=True)
        ar = api_aspect_ratio_from_resolution(self._resolution)
        model_candidates = self._image_models_to_try()
        total = len(out)
        if total <= 0:
            return out
        for i, seg in enumerate(out, start=1):
            self.check_cancelled()
            self._emit_progress(
                4,
                f"구간별 이미지 자동 생성 ({i}/{total})",
                sub_index=i,
                sub_total=total,
            )
            scene = str(seg.get("image_prompt", "")).strip()
            prompt = (
                "한국어 음악 영상용 배경 이미지를 생성하세요.\n"
                "요구: 텍스트/자막/워터마크/로고 금지, 단일 장면 배경.\n"
                "중요: 설명 문장만 주지 말고 반드시 이미지 1장을 생성하세요.\n"
                f"장면 설명: {scene or '음악 분위기에 맞는 풍경'}\n"
            )
            raw: bytes | None = None
            mime = "image/png"
            last_err = ""
            for m in model_candidates:
                try:
                    raw, mime = gemini_generate_image(
                        self._gemini_api_key,
                        m,
                        prompt=prompt,
                        aspect_ratio=ar,
                    )
                    break
                except GeminiImageApiError as e:
                    last_err = str(e)
            if raw is None:
                self.log_line.emit(f"구간 {i}: 이미지 생성 실패(스킵) — {last_err}")
                continue
            rel = f"images/wav_segments/{self._wav_path.stem}/seg_{i:03d}{_image_ext(mime)}"
            (self._project_parent / rel).write_bytes(raw)
            seg["image_relpath"] = rel
            self.log_line.emit(f"구간 {i}: 이미지 저장 → {rel}")
        return out

    def run(self) -> None:
        try:
            self.check_cancelled()
            if not self._wav_path.is_file():
                raise RuntimeError(f"WAV 파일이 없습니다: {self._wav_path}")
            if not self._gemini_api_key:
                raise RuntimeError("Gemini API 키가 필요합니다.")
            try:
                audio_duration_sec = float(ffprobe_duration_seconds(self._wav_path.resolve()))
            except (FfprobeError, OSError, ValueError):
                audio_duration_sec = 0.0

            self._emit_progress(1, "인트로 제목 설정", at_end=True)
            self._emit_progress(2, "자막 생성")
            subtitle_rel = self._run_stt(audio_duration_sec)
            self.check_cancelled()
            self._emit_progress(2, "자막 생성", at_end=True)
            self._emit_progress(3, "자동 구간 분석")
            segments = self._run_segments(audio_duration_sec)
            self.check_cancelled()
            self._emit_progress(3, "자동 구간 분석", at_end=True)
            segments = self._run_segment_images(segments)
            self.check_cancelled()
            self._emit_progress(
                4, "음악 분석 완료", sub_index=1, sub_total=1
            )

            self.succeeded.emit(
                {
                    "subtitle_relpath": subtitle_rel,
                    "cue_count": 0,
                    "segments": segments,
                    "intro_title": self._intro_title,
                }
            )
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
        except (
            SttTranscribeError,
            GeminiAudioSegmentsError,
            GeminiImageApiError,
            FFmpegRenderError,
            OSError,
            RuntimeError,
            ValueError,
        ) as e:
            self.failed.emit(str(e))
        except Exception as e:  # pragma: no cover
            self.failed.emit(f"예상치 못한 오류: {e}")
