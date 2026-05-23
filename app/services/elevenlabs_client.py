from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.srt_build import seconds_to_srt_timestamp, split_narration_lines


class ElevenLabsApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class ElevenLabsSpeechResult:
    audio_path: Path
    srt_path: Path


def _api_key(api_key: str) -> str:
    key = (api_key or os.environ.get("ELEVENLABS_API_KEY", "") or "").strip()
    if not key:
        raise ElevenLabsApiError("ElevenLabs API key is empty.")
    return key


def synthesize_speech_with_timestamps(
    *,
    text: str,
    voice_id: str,
    out_audio_path: Path,
    out_srt_path: Path,
    api_key: str = "",
    model_id: str = "eleven_multilingual_v2",
    language_code: str = "ko",
    output_format: str = "mp3_44100_128",
    max_line_chars: int = 24,
    timeout_sec: float = 300.0,
) -> ElevenLabsSpeechResult:
    clean_text = " ".join((text or "").split()).strip()
    if not clean_text:
        raise ElevenLabsApiError("Text is empty.")
    voice = (voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "") or "").strip()
    if not voice:
        raise ElevenLabsApiError("ElevenLabs voice_id is empty.")

    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{urllib.parse.quote(voice)}/with-timestamps?"
        + urllib.parse.urlencode({"output_format": output_format})
    )
    body: dict[str, Any] = {
        "text": clean_text,
        "model_id": model_id,
        "language_code": language_code,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "xi-api-key": _api_key(api_key),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise ElevenLabsApiError(f"ElevenLabs HTTP {e.code}: {err[:800] or e.reason}") from e
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        raise ElevenLabsApiError(f"ElevenLabs request failed: {e}") from e
    if not isinstance(payload, dict):
        raise ElevenLabsApiError("ElevenLabs response root is not an object.")

    audio_b64 = payload.get("audio_base64")
    if not isinstance(audio_b64, str) or not audio_b64.strip():
        raise ElevenLabsApiError("ElevenLabs response did not include audio_base64.")
    audio = base64.b64decode(audio_b64, validate=False)
    out_audio_path.parent.mkdir(parents=True, exist_ok=True)
    out_audio_path.write_bytes(audio)

    alignment = payload.get("normalized_alignment") or payload.get("alignment")
    out_srt_path.parent.mkdir(parents=True, exist_ok=True)
    out_srt_path.write_text(
        srt_from_character_alignment(clean_text, alignment, max_line_chars=max_line_chars),
        encoding="utf-8",
    )
    return ElevenLabsSpeechResult(out_audio_path, out_srt_path)


def srt_from_character_alignment(text: str, alignment: Any, *, max_line_chars: int = 24) -> str:
    if not isinstance(alignment, dict):
        raise ElevenLabsApiError("ElevenLabs response did not include alignment.")
    chars = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if not isinstance(chars, list) or not isinstance(starts, list) or not isinstance(ends, list):
        raise ElevenLabsApiError("ElevenLabs alignment shape is invalid.")
    if len(chars) != len(starts) or len(chars) != len(ends):
        raise ElevenLabsApiError("ElevenLabs alignment lengths do not match.")

    blocks: list[str] = []
    cursor = 0
    for idx, cue in enumerate(split_narration_lines(text, max_line_chars), start=1):
        target_len = len("".join(cue.split()))
        start_idx: int | None = None
        end_idx: int | None = None
        seen = 0
        for i in range(cursor, len(chars)):
            ch = str(chars[i])
            if start_idx is None and ch.strip():
                start_idx = i
            if ch.strip():
                seen += 1
            if start_idx is not None and seen >= target_len:
                end_idx = i
                cursor = i + 1
                break
        if start_idx is None or end_idx is None:
            break
        st = float(starts[start_idx])
        en = max(st + 0.04, float(ends[end_idx]))
        blocks.extend(
            [
                str(idx),
                f"{seconds_to_srt_timestamp(st)} --> {seconds_to_srt_timestamp(en)}",
                cue,
                "",
            ]
        )
    if not blocks:
        raise ElevenLabsApiError("No subtitle cues could be built from alignment.")
    return "\n".join(blocks).rstrip() + "\n"
