from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.services.ffmpeg_render import FFmpegRenderError, run_ffmpeg, which_ffmpeg
from app.services.ffprobe_audio import FfprobeError, ffprobe_duration_seconds
from app.services.gemini_audio_segments import GeminiAudioSegmentsError
from app.services.srt_build import build_srt_from_timed_lines, parse_srt_content
from app.services.subtitle_timing import (
    apply_subtitle_timing_adjustments,
    drop_overlapping_cues,
    prepend_intro_title_cue,
)
from app.services.subtitle_vocal_align import retime_subtitles_with_vocal_audio
from app.services.subtitle_generation_log import log_subtitle_generation_options
from app.services.stt_transcribe import (
    SttTranscribeError,
    refine_timed_lines_with_gemini,
    refine_timed_lines_with_reference_lyrics,
    transcribe_wav_sentences,
)

SttRefineMode = Literal["none", "polish", "lyrics", "align_only"]
logger = logging.getLogger(__name__)


class SttWavSegmentsWorker(CancellableQThread):
    log_line = Signal(str)
    progress = Signal(int, str)  # percent 0–100, message
    succeeded = Signal(object)  # dict: subtitle_relpath, cue_count
    failed = Signal(str)

    def __init__(
        self,
        *,
        wav_path: Path,
        language: str,
        refine_mode: SttRefineMode,
        reference_lyrics: str,
        gemini_api_key: str,
        gemini_model: str,
        project_parent: Path,
        output_relpath: str,
        max_line_chars: int,
        intro_skip_sec: float = 0.0,
        subtitle_offset_sec: float = 0.0,
        intro_title: str = "",
        intro_title_duration_sec: float = 0.0,
        vocal_retime_with_lyrics: bool = True,
        wav_segments: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._wav_path = wav_path
        self._language = language
        self._refine_mode = refine_mode
        self._reference_lyrics = (reference_lyrics or "").strip()
        self._gemini_api_key = gemini_api_key
        self._gemini_model = gemini_model
        self._project_parent = project_parent
        self._output_relpath = (output_relpath or "").strip().replace("\\", "/")
        self._max_line_chars = max(8, int(max_line_chars))
        self._intro_skip_sec = max(0.0, float(intro_skip_sec or 0.0))
        self._subtitle_offset_sec = float(subtitle_offset_sec or 0.0)
        self._intro_title = (intro_title or "").strip()
        self._intro_title_duration_sec = max(0.0, float(intro_title_duration_sec or 0.0))
        self._vocal_retime_with_lyrics = bool(vocal_retime_with_lyrics)
        self._wav_segments = wav_segments
        self._enable_timing_corrections = (
            (os.environ.get("CONTENT_PRODUCTION_ENABLE_TIMING_CORRECTIONS") or "")
            .strip()
            .lower()
            in ("1", "true", "yes", "on")
        )

    def _lines_to_timed(self, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        timed: list[dict[str, Any]] = []
        for ln in lines:
            try:
                st = float(ln.get("start_sec", 0.0))
                en = float(ln.get("end_sec", 0.0))
            except (TypeError, ValueError):
                continue
            if en <= st:
                continue
            txt = str(ln.get("text", "")).strip()
            if not txt:
                continue
            timed.append({"start_sec": st, "end_sec": en, "text": txt})
        return timed

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

    def run(self) -> None:
        try:
            self.check_cancelled()
            pure_stt_mode = self._refine_mode == "none"
            log_subtitle_generation_options(
                wav_path=self._wav_path,
                language=self._language,
                refine_mode=self._refine_mode,
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
                pure_stt_mode=pure_stt_mode,
            )
            if self._refine_mode == "align_only":
                if not self._output_relpath:
                    raise SttTranscribeError("자막 저장 경로가 비어 있습니다.")
                srt_abs = (self._project_parent / self._output_relpath).resolve()
                if not srt_abs.is_file():
                    raise SttTranscribeError(f"자막 파일이 없습니다: {srt_abs}")
                if not self._reference_lyrics:
                    raise SttTranscribeError("원곡 가사가 비어 있습니다.")
                cues = parse_srt_content(srt_abs.read_text(encoding="utf-8"))
                lines = [
                    {"start_sec": st, "end_sec": en, "text": tx}
                    for st, en, tx in cues
                ]
                self.log_line.emit(f"기존 SRT {len(lines)}구간 → 원곡 가사로 교정...")
                self.progress.emit(20, "기존 SRT 교정")
                stt_raw: list[dict[str, Any]] = [dict(ln) for ln in lines]
            else:
                self.check_cancelled()
                self.log_line.emit(f"STT 시작: {self._wav_path.name}")
                self.progress.emit(10, "STT 추출")
                lines = transcribe_wav_sentences(self._wav_path, language=self._language)
                self.check_cancelled()
                self.log_line.emit(f"STT 추출 완료: {len(lines)}개")
                self.progress.emit(30, "STT 추출 완료")
                stt_raw = [dict(ln) for ln in lines]
                self._log_cue_dump("STT 원본", lines)

            self.check_cancelled()
            if self._refine_mode == "lyrics":
                self.log_line.emit("LLM 원곡 가사 교정 시작(시간값 유지)...")
                self.progress.emit(45, "원곡 가사 교정")
                lines = refine_timed_lines_with_reference_lyrics(
                    lines=lines,
                    reference_lyrics=self._reference_lyrics,
                    api_key=self._gemini_api_key,
                    model=self._gemini_model,
                )
                self.log_line.emit("LLM 원곡 가사 교정 완료")
                self._log_cue_dump("LLM 원곡 교정", lines)
            elif self._refine_mode == "polish":
                self.log_line.emit("LLM 맞춤법 다듬기 시작(시간값 유지)...")
                self.progress.emit(45, "맞춤법 교정")
                lines = refine_timed_lines_with_gemini(
                    lines=lines,
                    api_key=self._gemini_api_key,
                    model=self._gemini_model,
                )
                self.log_line.emit("LLM 맞춤법 다듬기 완료")
                self._log_cue_dump("LLM 맞춤법 교정", lines)

            audio_duration_sec = 0.0
            try:
                audio_duration_sec = float(ffprobe_duration_seconds(self._wav_path.resolve()))
            except (FfprobeError, OSError, ValueError):
                audio_duration_sec = 0.0

            use_vocal_retime = (
                self._vocal_retime_with_lyrics
                and self._reference_lyrics
                and self._refine_mode in ("lyrics", "align_only")
            )
            self.check_cancelled()
            if use_vocal_retime:
                self.log_line.emit(
                    "STT 기반 보컬 구간 자막 타이밍(인트로·간주 무자막) 시작..."
                )
                self.progress.emit(60, "보컬 구간 타이밍")
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
                self.log_line.emit(f"보컬 구간 자막 타이밍 완료: {len(lines)}개")
                self._log_cue_dump("보컬 타이밍 후", lines)

            if pure_stt_mode:
                self.log_line.emit("순수 STT 모드: 인트로/오프셋/제목 보정 건너뜀")
            elif self._intro_skip_sec > 0 or self._subtitle_offset_sec != 0.0:
                before = len(lines)
                lines = apply_subtitle_timing_adjustments(
                    lines,
                    intro_skip_sec=self._intro_skip_sec,
                    offset_sec=self._subtitle_offset_sec,
                    audio_duration_sec=audio_duration_sec,
                )
                lines = drop_overlapping_cues(lines)
                self.log_line.emit(
                    f"자막 시간 보정(인트로 {self._intro_skip_sec:.1f}초 무자막, "
                    f"지연 {self._subtitle_offset_sec:+.1f}초): {before}→{len(lines)}구간"
                )

            if (not pure_stt_mode) and self._intro_title:
                lines = prepend_intro_title_cue(
                    lines,
                    title=self._intro_title,
                    duration_sec=self._intro_title_duration_sec,
                    intro_skip_sec=self._intro_skip_sec,
                    audio_duration_sec=audio_duration_sec,
                )
                lines = drop_overlapping_cues(lines)
                self.log_line.emit(f"인트로 제목 자막 추가: {self._intro_title!r}")

            self._log_cue_dump("최종전", lines)

            timed = self._lines_to_timed(lines)
            if not timed:
                raise SttTranscribeError("유효한 STT 자막 구간이 없습니다.")

            if not self._output_relpath:
                raise SttTranscribeError("자막 저장 경로가 비어 있습니다.")
            self.progress.emit(85, "SRT 저장")
            body = build_srt_from_timed_lines(timed, max_line_chars=self._max_line_chars)
            out_abs = (self._project_parent / self._output_relpath).resolve()
            out_abs.parent.mkdir(parents=True, exist_ok=True)
            out_abs.write_text(body, encoding="utf-8")
            self.log_line.emit(f"자막 SRT 저장: {self._output_relpath}")
            self.progress.emit(100, "자막 생성 완료")
            self.succeeded.emit(
                {
                    "subtitle_relpath": self._output_relpath,
                    "cue_count": len(timed),
                }
            )
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
        except (
            SttTranscribeError,
            GeminiAudioSegmentsError,
            FFmpegRenderError,
            OSError,
            RuntimeError,
            ValueError,
        ) as e:
            self.failed.emit(str(e))
        except Exception as e:  # pragma: no cover
            self.failed.emit(f"예상치 못한 오류: {e}")
