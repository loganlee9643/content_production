from __future__ import annotations

import logging
from pathlib import Path

from app.services.stt_transcribe import log_effective_stt_options

logger = logging.getLogger(__name__)


def log_subtitle_generation_options(
    *,
    wav_path: Path,
    language: str,
    refine_mode: str,
    output_relpath: str,
    max_line_chars: int,
    intro_skip_sec: float,
    subtitle_offset_sec: float,
    intro_title: str,
    intro_title_duration_sec: float,
    vocal_retime_with_lyrics: bool,
    enable_timing_corrections: bool,
    reference_lyrics_len: int,
    wav_segments_count: int,
    gemini_model: str = "",
    pure_stt_mode: bool = False,
) -> None:
    """자막 생성 작업 시작 시 터미널에 적용 옵션을 출력한다."""
    logger.info("--- 자막 생성 옵션 ---")
    logger.info("wav=%s", wav_path.resolve())
    logger.info("language=%s refine_mode=%s pure_stt_mode=%s", language, refine_mode, pure_stt_mode)
    logger.info("output_relpath=%s max_line_chars=%s", output_relpath, max_line_chars)
    logger.info(
        "intro_skip_sec=%.2f subtitle_offset_sec=%+.2f intro_title=%r intro_title_duration_sec=%.2f",
        intro_skip_sec,
        subtitle_offset_sec,
        intro_title or "",
        intro_title_duration_sec,
    )
    logger.info(
        "vocal_retime_with_lyrics=%s enable_timing_corrections=%s reference_lyrics_chars=%s wav_segments=%s",
        vocal_retime_with_lyrics,
        enable_timing_corrections,
        reference_lyrics_len,
        wav_segments_count,
    )
    if gemini_model:
        logger.info("gemini_model=%s", gemini_model)
    log_effective_stt_options(heading="STT (faster-whisper)")
