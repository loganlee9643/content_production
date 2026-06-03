from __future__ import annotations

import os
import wave
import audioop
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.services.srt_build import seconds_to_srt_timestamp, split_narration_lines


class GeminiTtsApiError(RuntimeError):
    pass


GEMINI_TTS_PCM_RATE = 24000
OUTPUT_WAV_RATE = 48000
OUTPUT_WAV_CHANNELS = 1
OUTPUT_WAV_SAMPLE_WIDTH = 2


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
    pcm = _request_gemini_tts_pcm(
        client=client,
        types=types,
        text=clean_text,
        model_id=model_id,
        voice_name=voice_name,
        style_prompt=style_prompt,
    )
    pcm = _resample_pcm_16bit_mono(pcm, source_rate=GEMINI_TTS_PCM_RATE, target_rate=OUTPUT_WAV_RATE)

    out_audio_path.parent.mkdir(parents=True, exist_ok=True)
    _write_wave(out_audio_path, pcm)
    duration_sec = _wav_duration_seconds(out_audio_path)
    out_srt_path.parent.mkdir(parents=True, exist_ok=True)
    out_srt_path.write_text(
        _build_srt_by_duration(clean_text, duration_sec, max_line_chars=max_line_chars),
        encoding="utf-8",
    )
    return GeminiTtsResult(audio_path=out_audio_path, srt_path=out_srt_path)


def synthesize_gemini_speech_segments(
    *,
    text_segments: list[str],
    out_audio_path: Path,
    out_srt_path: Path,
    api_key: str = "",
    model_id: str = "gemini-2.5-flash-preview-tts",
    voice_name: str = "Kore",
    style_prompt: str = "Read naturally in clear Korean narration.",
    max_line_chars: int = 24,
    progress_callback: Callable[[int, int], None] | None = None,
) -> GeminiTtsResult:
    clean_segments = [
        "\n".join(line.strip() for line in segment.splitlines() if line.strip()).strip()
        for segment in text_segments
    ]
    clean_segments = [segment for segment in clean_segments if segment]
    if not clean_segments:
        raise GeminiTtsApiError("Text is empty.")

    genai, types = _import_genai()
    key = (api_key or os.environ.get("GEMINI_API_KEY", "") or "").strip()
    client = genai.Client(api_key=key) if key else genai.Client()

    pcm_parts: list[bytes] = []
    timed_segments: list[tuple[str, float]] = []
    total = len(clean_segments)
    cache_dir = out_audio_path.parent / f".{out_audio_path.stem}_gemini_parts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for index, segment in enumerate(clean_segments, start=1):
        if progress_callback is not None:
            progress_callback(index, total)
        part_path = _segment_cache_path(
            cache_dir,
            index,
            segment,
            model_id=model_id,
            voice_name=voice_name,
            style_prompt=style_prompt,
        )
        if part_path.is_file():
            pcm, duration_sec = _read_wave_pcm(part_path)
        else:
            pcm = _request_gemini_tts_pcm(
                client=client,
                types=types,
                text=segment,
                model_id=model_id,
                voice_name=voice_name,
                style_prompt=style_prompt,
            )
            pcm = _resample_pcm_16bit_mono(pcm, source_rate=GEMINI_TTS_PCM_RATE, target_rate=OUTPUT_WAV_RATE)
            _write_wave(part_path, pcm)
            duration_sec = _pcm_duration_seconds(
                pcm,
                rate=OUTPUT_WAV_RATE,
                channels=OUTPUT_WAV_CHANNELS,
                sample_width=OUTPUT_WAV_SAMPLE_WIDTH,
            )
        pcm_parts.append(pcm)
        timed_segments.append((segment, duration_sec))

    out_audio_path.parent.mkdir(parents=True, exist_ok=True)
    _write_wave(out_audio_path, b"".join(pcm_parts))
    out_srt_path.parent.mkdir(parents=True, exist_ok=True)
    out_srt_path.write_text(
        _build_srt_by_segments(timed_segments, max_line_chars=max_line_chars),
        encoding="utf-8",
    )
    return GeminiTtsResult(audio_path=out_audio_path, srt_path=out_srt_path)


def _segment_cache_path(
    cache_dir: Path,
    index: int,
    text: str,
    *,
    model_id: str,
    voice_name: str,
    style_prompt: str,
) -> Path:
    cache_key = "\n".join(
        [
            str(model_id or ""),
            str(voice_name or ""),
            str(style_prompt or ""),
            text,
        ]
    )
    digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:12]
    return cache_dir / f"part_{index:03d}_{digest}.wav"


def _request_gemini_tts_pcm(
    *,
    client: Any,
    types: Any,
    text: str,
    model_id: str,
    voice_name: str,
    style_prompt: str,
) -> bytes:
    consistency = (
        "Voice consistency instruction: Use the same single narrator throughout. "
        "Keep pitch, pace, loudness, emotion, and timbre steady. "
        "Do not change character, accent, age, gender, or performance style between paragraphs."
    )
    prompt_parts = [consistency]
    if style_prompt.strip():
        prompt_parts.append(style_prompt.strip())
    prompt_parts.append(text)
    prompt = "\n\n".join(prompt_parts)
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
    return pcm


def _resample_pcm_16bit_mono(pcm: bytes, *, source_rate: int, target_rate: int) -> bytes:
    if source_rate == target_rate:
        return pcm
    converted, _state = audioop.ratecv(pcm, OUTPUT_WAV_SAMPLE_WIDTH, OUTPUT_WAV_CHANNELS, source_rate, target_rate, None)
    return converted


def _write_wave(
    path: Path,
    pcm: bytes,
    *,
    channels: int = OUTPUT_WAV_CHANNELS,
    rate: int = OUTPUT_WAV_RATE,
    sample_width: int = OUTPUT_WAV_SAMPLE_WIDTH,
) -> None:
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


def _read_wave_pcm(path: Path) -> tuple[bytes, float]:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.getnframes()
        pcm = wf.readframes(frames)
    if channels != OUTPUT_WAV_CHANNELS or sample_width != OUTPUT_WAV_SAMPLE_WIDTH or rate != OUTPUT_WAV_RATE:
        raise GeminiTtsApiError(f"Cached Gemini TTS WAV has unexpected format: {path}")
    duration = max(0.04, frames / float(rate)) if frames > 0 and rate > 0 else 0.04
    return pcm, duration


def _pcm_duration_seconds(pcm: bytes, *, rate: int, channels: int, sample_width: int) -> float:
    bytes_per_second = max(1, int(rate) * int(channels) * int(sample_width))
    return max(0.04, len(pcm) / float(bytes_per_second))


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


def _build_srt_by_segments(timed_segments: list[tuple[str, float]], *, max_line_chars: int) -> str:
    blocks: list[str] = []
    cue_index = 1
    timeline = 0.0
    for text, duration_sec in timed_segments:
        lines = split_narration_lines(text, max_line_chars)
        if not lines:
            timeline += max(0.04, float(duration_sec))
            continue
        step = max(0.04, float(duration_sec)) / len(lines)
        t = timeline
        for line in lines:
            end = t + step
            blocks.extend(
                [
                    str(cue_index),
                    f"{seconds_to_srt_timestamp(t)} --> {seconds_to_srt_timestamp(end)}",
                    line,
                    "",
                ]
            )
            cue_index += 1
            t = end
        timeline += max(0.04, float(duration_sec))
    if not blocks:
        raise GeminiTtsApiError("No subtitle cues could be built.")
    return "\n".join(blocks).rstrip() + "\n"
