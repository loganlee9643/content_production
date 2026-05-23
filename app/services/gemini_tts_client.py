from __future__ import annotations

import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.srt_build import seconds_to_srt_timestamp, split_narration_lines


class GeminiTtsApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeminiTtsResult:
    audio_path: Path
    srt_path: Path


def _import_genai() -> tuple[Any, Any]:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise GeminiTtsApiError("google-genai is required for Gemini TTS.") from e
    return genai, types


def synthesize_gemini_speech(
    *,
    text: str,
    out_audio_path: Path,
    out_srt_path: Path,
    api_key: str = "",
    model_id: str = "gemini-2.5-flash-preview-tts",
    voice_name: str = "Kore",
    style_prompt: str = "Read naturally in clear Korean narration.",
    max_line_chars: int = 24,
) -> GeminiTtsResult:
    clean_text = "\n".join(line.strip() for line in (text or "").splitlines() if line.strip()).strip()
    if not clean_text:
        raise GeminiTtsApiError("Text is empty.")

    genai, types = _import_genai()
    key = (api_key or os.environ.get("GEMINI_API_KEY", "") or "").strip()
    client = genai.Client(api_key=key) if key else genai.Client()
    prompt = f"{style_prompt.strip()}\n\n{clean_text}" if style_prompt.strip() else clean_text
    try:
        response = client.models.generate_content(
            model=(model_id or "gemini-2.5-flash-preview-tts").strip(),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=(voice_name or "Kore").strip(),
                        )
                    )
                ),
            ),
        )
    except Exception as e:
        raise GeminiTtsApiError(f"Gemini TTS request failed: {e}") from e

    try:
        pcm = response.candidates[0].content.parts[0].inline_data.data
    except (AttributeError, IndexError, TypeError) as e:
        raise GeminiTtsApiError("Gemini TTS response did not include audio data.") from e
    if not pcm:
        raise GeminiTtsApiError("Gemini TTS returned empty audio.")

    out_audio_path.parent.mkdir(parents=True, exist_ok=True)
    _write_wave(out_audio_path, pcm)
    duration_sec = _wav_duration_seconds(out_audio_path)
    out_srt_path.parent.mkdir(parents=True, exist_ok=True)
    out_srt_path.write_text(
        _build_srt_by_duration(clean_text, duration_sec, max_line_chars=max_line_chars),
        encoding="utf-8",
    )
    return GeminiTtsResult(audio_path=out_audio_path, srt_path=out_srt_path)


def _write_wave(path: Path, pcm: bytes, *, channels: int = 1, rate: int = 24000, sample_width: int = 2) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def _wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
    if frames <= 0 or rate <= 0:
        return 0.04
    return max(0.04, frames / float(rate))


def _build_srt_by_duration(text: str, duration_sec: float, *, max_line_chars: int) -> str:
    lines = split_narration_lines(text, max_line_chars)
    if not lines:
        raise GeminiTtsApiError("No subtitle cues could be built.")
    step = max(0.04, float(duration_sec)) / len(lines)
    blocks: list[str] = []
    t = 0.0
    for idx, line in enumerate(lines, start=1):
        end = t + step
        blocks.extend(
            [
                str(idx),
                f"{seconds_to_srt_timestamp(t)} --> {seconds_to_srt_timestamp(end)}",
                line,
                "",
            ]
        )
        t = end
    return "\n".join(blocks).rstrip() + "\n"
