from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import wave
import audioop
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from app.services.gemini_model_catalog import DEFAULT_GEMINI_MODEL

logger = logging.getLogger(__name__)
_STT_RUNTIME_OPTIONS: dict[str, object] = {}
STT_EXTERNAL_CHUNK_SECONDS = 90.0
STT_CHUNK_SPLIT_SEARCH_SECONDS = 10.0
STT_CHUNK_RMS_WINDOW_SECONDS = 0.35
STT_MIN_CHUNK_SECONDS = 30.0


def set_stt_runtime_options(
    *,
    model: str | None = None,
    compute_type: str | None = None,
    vad_filter: bool | None = None,
    vad_threshold: float | None = None,
    vad_min_silence_duration_ms: int | None = None,
    vad_min_speech_duration_ms: int | None = None,
    vad_speech_pad_ms: int | None = None,
    beam_size: int | None = None,
    no_speech_threshold: float | None = None,
    max_no_speech_prob: float | None = None,
    log_prob_threshold: float | None = None,
    condition_on_previous_text: bool | None = None,
    temperature: float | None = None,
    compression_ratio_threshold: float | None = None,
    chunk_length: int | None = None,
) -> None:
    """앱 설정값 기반 STT 런타임 옵션 주입."""
    updates: dict[str, object] = {}
    if model is not None:
        updates["model"] = str(model).strip() or "small"
    if compute_type is not None:
        updates["compute_type"] = str(compute_type).strip() or "int8"
    if vad_filter is not None:
        updates["vad_filter"] = bool(vad_filter)
    if vad_threshold is not None:
        updates["vad_threshold"] = max(0.0, min(1.0, float(vad_threshold)))
    if vad_min_silence_duration_ms is not None:
        updates["vad_min_silence_duration_ms"] = max(0, int(vad_min_silence_duration_ms))
    if vad_min_speech_duration_ms is not None:
        updates["vad_min_speech_duration_ms"] = max(0, int(vad_min_speech_duration_ms))
    if vad_speech_pad_ms is not None:
        updates["vad_speech_pad_ms"] = max(0, int(vad_speech_pad_ms))
    if beam_size is not None:
        updates["beam_size"] = max(1, int(beam_size))
    if no_speech_threshold is not None:
        updates["no_speech_threshold"] = float(no_speech_threshold)
    if max_no_speech_prob is not None:
        updates["max_no_speech_prob"] = float(max_no_speech_prob)
    if log_prob_threshold is not None:
        updates["log_prob_threshold"] = float(log_prob_threshold)
    if condition_on_previous_text is not None:
        updates["condition_on_previous_text"] = bool(condition_on_previous_text)
    if temperature is not None:
        updates["temperature"] = float(temperature)
    if compression_ratio_threshold is not None:
        updates["compression_ratio_threshold"] = float(compression_ratio_threshold)
    if chunk_length is not None:
        updates["chunk_length"] = max(15, int(chunk_length))
    if updates:
        _STT_RUNTIME_OPTIONS.update(updates)
        logger.info("STT 앱 설정 적용: %s", updates)


class SttTranscribeError(RuntimeError):
    pass


def _gemini_url(model_id: str, api_key: str) -> str:
    qs = urlencode({"key": api_key})
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?{qs}"


def _normalize_model_id(model: str) -> str:
    m = (model or "").strip()
    if m.startswith("models/"):
        m = m[len("models/") :]
    return m


def _no_window_kwargs() -> dict[str, int]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _wav_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            rate = float(wf.getframerate() or 0)
            frames = float(wf.getnframes() or 0)
            if rate > 0:
                return max(0.0, frames / rate)
    except Exception:
        return 0.0
    return 0.0


def _wav_rms_at(wf: wave.Wave_read, *, start_sec: float, window_sec: float) -> int | None:
    rate = int(wf.getframerate() or 0)
    width = int(wf.getsampwidth() or 0)
    if rate <= 0 or width <= 0:
        return None
    start_frame = max(0, int(start_sec * rate))
    frame_count = max(1, int(window_sec * rate))
    try:
        wf.setpos(min(start_frame, max(0, wf.getnframes() - 1)))
        data = wf.readframes(frame_count)
        if not data:
            return None
        return int(audioop.rms(data, width))
    except Exception:
        return None


def _find_low_energy_split(path: Path, *, target_sec: float, duration_sec: float) -> float:
    search_start = max(STT_MIN_CHUNK_SECONDS, target_sec - STT_CHUNK_SPLIT_SEARCH_SECONDS)
    search_end = min(duration_sec - STT_MIN_CHUNK_SECONDS, target_sec + STT_CHUNK_SPLIT_SEARCH_SECONDS)
    if search_end <= search_start:
        return max(STT_MIN_CHUNK_SECONDS, min(duration_sec - STT_MIN_CHUNK_SECONDS, target_sec))
    best_sec = target_sec
    best_rms: int | None = None
    step_sec = 0.10
    try:
        with wave.open(str(path), "rb") as wf:
            pos = search_start
            while pos <= search_end:
                rms = _wav_rms_at(wf, start_sec=pos, window_sec=STT_CHUNK_RMS_WINDOW_SECONDS)
                if rms is not None and (best_rms is None or rms < best_rms):
                    best_rms = rms
                    best_sec = pos + (STT_CHUNK_RMS_WINDOW_SECONDS / 2.0)
                pos += step_sec
    except Exception:
        return max(STT_MIN_CHUNK_SECONDS, min(duration_sec - STT_MIN_CHUNK_SECONDS, target_sec))
    return max(STT_MIN_CHUNK_SECONDS, min(duration_sec - STT_MIN_CHUNK_SECONDS, best_sec))


def _waveform_chunk_ranges(path: Path, *, duration_sec: float) -> list[tuple[float, float]]:
    if duration_sec <= STT_EXTERNAL_CHUNK_SECONDS * 1.5:
        return [(0.0, duration_sec)]
    split_points: list[float] = []
    target = STT_EXTERNAL_CHUNK_SECONDS
    while target < duration_sec - STT_MIN_CHUNK_SECONDS:
        split = _find_low_energy_split(path, target_sec=target, duration_sec=duration_sec)
        if split_points and split <= split_points[-1] + STT_MIN_CHUNK_SECONDS:
            split = min(duration_sec - STT_MIN_CHUNK_SECONDS, split_points[-1] + STT_EXTERNAL_CHUNK_SECONDS)
        if split >= duration_sec - STT_MIN_CHUNK_SECONDS:
            break
        split_points.append(split)
        target = split + STT_EXTERNAL_CHUNK_SECONDS
    points = [0.0, *split_points, duration_sec]
    return [(points[i], points[i + 1]) for i in range(len(points) - 1) if points[i + 1] > points[i]]


def _extract_wav_chunk(src: Path, dst: Path, *, start_sec: float, duration_sec: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SttTranscribeError("ffmpeg를 찾지 못해 STT 청크를 만들 수 없습니다.")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start_sec):.3f}",
        "-t",
        f"{max(0.04, duration_sec):.3f}",
        "-i",
        str(src.resolve()),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        str(dst.resolve()),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        **_no_window_kwargs(),
    )
    if proc.returncode != 0:
        raise SttTranscribeError((proc.stderr or proc.stdout or "STT 청크 생성 실패").strip())


def _cleanup_text(t: str) -> str:
    out = " ".join((t or "").split())
    out = re.sub(r"\s+([,.!?])", r"\1", out)
    return out.strip()


def _env_bool(name: str, *, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, *, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _runtime_bool(key: str, env_name: str, *, default: bool) -> bool:
    if key in _STT_RUNTIME_OPTIONS:
        return bool(_STT_RUNTIME_OPTIONS[key])
    return _env_bool(env_name, default=default)


def _runtime_float(key: str, env_name: str, *, default: float) -> float:
    if key in _STT_RUNTIME_OPTIONS:
        try:
            return float(_STT_RUNTIME_OPTIONS[key])
        except (TypeError, ValueError):
            return default
    return _env_float(env_name, default=default)


def _runtime_int(key: str, env_name: str, *, default: int) -> int:
    if key in _STT_RUNTIME_OPTIONS:
        try:
            return int(_STT_RUNTIME_OPTIONS[key])
        except (TypeError, ValueError):
            return default
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_vad_parameters(*, vad_filter: bool) -> dict[str, object] | None:
    if not vad_filter:
        return None
    threshold = _runtime_float("vad_threshold", "CONTENT_PRODUCTION_STT_VAD_THRESHOLD", default=0.5)
    min_silence = _runtime_int(
        "vad_min_silence_duration_ms",
        "CONTENT_PRODUCTION_STT_VAD_MIN_SILENCE_MS",
        default=2000,
    )
    min_speech = _runtime_int(
        "vad_min_speech_duration_ms",
        "CONTENT_PRODUCTION_STT_VAD_MIN_SPEECH_MS",
        default=0,
    )
    speech_pad = _runtime_int(
        "vad_speech_pad_ms",
        "CONTENT_PRODUCTION_STT_VAD_SPEECH_PAD_MS",
        default=400,
    )
    params: dict[str, object] = {
        "threshold": max(0.0, min(1.0, threshold)),
        "min_silence_duration_ms": max(0, min_silence),
        "min_speech_duration_ms": max(0, min_speech),
        "speech_pad_ms": max(0, speech_pad),
    }
    return params


def _merge_lines(lines: list[dict[str, Any]], *, max_chars: int = 28, max_gap_sec: float = 0.28) -> list[dict[str, Any]]:
    if not lines:
        return []
    merged: list[dict[str, Any]] = []
    cur = dict(lines[0])
    for nxt in lines[1:]:
        try:
            cur_en = float(cur["end_sec"])
            nxt_st = float(nxt["start_sec"])
        except (TypeError, ValueError, KeyError):
            continue
        gap = max(0.0, nxt_st - cur_en)
        cand = f"{str(cur.get('text', '')).strip()} {str(nxt.get('text', '')).strip()}".strip()
        if gap <= max_gap_sec and len(cand) <= max_chars:
            cur["end_sec"] = float(nxt.get("end_sec", cur_en))
            cur["text"] = cand
        else:
            merged.append(cur)
            cur = dict(nxt)
    merged.append(cur)
    return merged


def _word_timed_lines(words: list[dict[str, Any]], *, max_chars: int = 28, max_duration_sec: float = 3.8) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur_words: list[str] = []
    cur_start = 0.0
    cur_end = 0.0

    def flush() -> None:
        nonlocal cur_words, cur_start, cur_end
        text = _cleanup_text(" ".join(cur_words).strip())
        if text and cur_end > cur_start:
            out.append({"start_sec": cur_start, "end_sec": cur_end, "text": text, "_source": "faster_word"})
        cur_words = []
        cur_start = 0.0
        cur_end = 0.0

    for word in words:
        try:
            st = float(word["start_sec"])
            en = float(word["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        text = _cleanup_text(str(word.get("text", "")).strip())
        if not text or en <= st:
            continue
        if not cur_words:
            cur_words = [text]
            cur_start = st
            cur_end = en
            continue

        gap = max(0.0, st - cur_end)
        candidate = f"{' '.join(cur_words)} {text}".strip()
        duration = en - cur_start
        should_flush = (
            gap >= 0.42
            or len(candidate) > max_chars
            or duration > max_duration_sec
            or (cur_words[-1].endswith(('.', '?', '!', '。', '？', '！')) and len(candidate) > max_chars * 0.65)
        )
        if should_flush:
            flush()
            cur_words = [text]
            cur_start = st
            cur_end = en
        else:
            cur_words.append(text)
            cur_end = en
    flush()
    return out


def _line_overlaps_segment(line: dict[str, Any], segment: dict[str, Any], *, min_overlap_sec: float = 0.08) -> bool:
    try:
        line_start = float(line["start_sec"])
        line_end = float(line["end_sec"])
        seg_start = float(segment["start_sec"])
        seg_end = float(segment["end_sec"])
    except (KeyError, TypeError, ValueError):
        return False
    return min(line_end, seg_end) - max(line_start, seg_start) >= min_overlap_sec


def _merge_word_lines_with_segment_fallbacks(
    word_lines: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not word_lines:
        return segments
    merged = [dict(line) for line in word_lines]
    fallback_count = 0
    for segment in segments:
        if any(_line_overlaps_segment(line, segment) for line in word_lines):
            continue
        fallback = dict(segment)
        fallback["_source"] = "faster_segment_fallback"
        merged.append(fallback)
        fallback_count += 1
    merged.sort(key=lambda x: (float(x.get("start_sec", 0.0)), float(x.get("end_sec", 0.0))))
    if fallback_count:
        logger.info("faster-whisper segment fallback 사용: %s개", fallback_count)
    return merged


def _log_large_timing_gaps(lines: list[dict[str, Any]], *, label: str, min_gap_sec: float = 6.0) -> None:
    prev: dict[str, Any] | None = None
    for cur in lines:
        if prev is None:
            prev = cur
            continue
        try:
            prev_end = float(prev["end_sec"])
            cur_start = float(cur["start_sec"])
        except (KeyError, TypeError, ValueError):
            prev = cur
            continue
        gap = cur_start - prev_end
        if gap >= min_gap_sec:
            logger.warning(
                "%s STT timing gap: %.2fs -> %.2fs (gap %.2fs), prev=%r next=%r",
                label,
                prev_end,
                cur_start,
                gap,
                str(prev.get("text", ""))[:80],
                str(cur.get("text", ""))[:80],
            )
        prev = cur


def resolve_effective_stt_options() -> dict[str, object]:
    """앱 설정·환경 변수·기본값을 합친 실제 STT transcribe 옵션."""
    try:
        if "beam_size" in _STT_RUNTIME_OPTIONS:
            beam_size = max(1, int(_STT_RUNTIME_OPTIONS["beam_size"]))
        else:
            beam_size = max(1, int((os.environ.get("CONTENT_PRODUCTION_STT_BEAM_SIZE") or "5").strip()))
    except ValueError:
        beam_size = 5
    vad_on = _runtime_bool("vad_filter", "CONTENT_PRODUCTION_STT_VAD_FILTER", default=False)
    return {
        "model": str(_STT_RUNTIME_OPTIONS.get("model", os.environ.get("CONTENT_PRODUCTION_STT_MODEL", "small"))).strip()
        or "small",
        "compute_type": str(
            _STT_RUNTIME_OPTIONS.get("compute_type", os.environ.get("CONTENT_PRODUCTION_STT_COMPUTE", "int8"))
        ).strip()
        or "int8",
        "vad_filter": vad_on,
        "vad_parameters": _resolve_vad_parameters(vad_filter=vad_on),
        "beam_size": beam_size,
        "no_speech_threshold": _runtime_float(
            "no_speech_threshold", "CONTENT_PRODUCTION_STT_NO_SPEECH_THRESHOLD", default=0.99
        ),
        "max_no_speech_prob": _runtime_float(
            "max_no_speech_prob", "CONTENT_PRODUCTION_STT_MAX_NO_SPEECH_PROB", default=1.0
        ),
        "log_prob_threshold": _runtime_float(
            "log_prob_threshold", "CONTENT_PRODUCTION_STT_LOG_PROB_THRESHOLD", default=-2.0
        ),
        "condition_on_previous_text": _runtime_bool(
            "condition_on_previous_text", "CONTENT_PRODUCTION_STT_CONDITION_ON_PREVIOUS_TEXT", default=False
        ),
        "temperature": _runtime_float("temperature", "CONTENT_PRODUCTION_STT_TEMPERATURE", default=0.0),
        "compression_ratio_threshold": _runtime_float(
            "compression_ratio_threshold", "CONTENT_PRODUCTION_STT_COMPRESSION_RATIO_THRESHOLD", default=2.4
        ),
        "chunk_length": max(15, _runtime_int("chunk_length", "CONTENT_PRODUCTION_STT_CHUNK_LENGTH", default=30)),
    }


def log_effective_stt_options(*, heading: str = "STT 옵션") -> None:
    opts = resolve_effective_stt_options()
    logger.info(
        "%s: model=%s compute_type=%s vad_filter=%s beam_size=%s "
        "no_speech_threshold=%.3f max_no_speech_prob=%.3f log_prob_threshold=%.3f "
        "condition_on_previous_text=%s temperature=%.2f compression_ratio_threshold=%.2f chunk_length=%s",
        heading,
        opts["model"],
        opts["compute_type"],
        opts["vad_filter"],
        opts["beam_size"],
        float(opts["no_speech_threshold"]),
        float(opts["max_no_speech_prob"]),
        float(opts["log_prob_threshold"]),
        opts["condition_on_previous_text"],
        float(opts["temperature"]),
        float(opts["compression_ratio_threshold"]),
        opts["chunk_length"],
    )
    vad_params = opts.get("vad_parameters")
    if isinstance(vad_params, dict) and vad_params:
        logger.info(
            "VAD(Silero): threshold=%.2f min_silence_ms=%s min_speech_ms=%s speech_pad_ms=%s",
            float(vad_params.get("threshold", 0.5)),
            vad_params.get("min_silence_duration_ms"),
            vad_params.get("min_speech_duration_ms"),
            vad_params.get("speech_pad_ms"),
        )


SttProgressCallback = Callable[[float, float, str], None]


def _collect_faster_whisper_lines(
    *,
    model: Any,
    wav_path: Path,
    language: str,
    opts: dict[str, object],
    time_offset_sec: float = 0.0,
    total_duration_sec: float = 0.0,
    progress_callback: SttProgressCallback | None = None,
) -> list[dict[str, Any]]:
    vad_filter = bool(opts["vad_filter"])
    vad_parameters = opts.get("vad_parameters")
    beam_size = int(opts["beam_size"])
    no_speech_threshold = float(opts["no_speech_threshold"])
    log_prob_threshold = float(opts["log_prob_threshold"])
    max_no_speech = float(opts["max_no_speech_prob"])
    transcribe_kw: dict[str, object] = {
        "language": language,
        "vad_filter": vad_filter,
        "beam_size": beam_size,
        "condition_on_previous_text": bool(opts["condition_on_previous_text"]),
        "temperature": float(opts["temperature"]),
        "compression_ratio_threshold": float(opts["compression_ratio_threshold"]),
        "chunk_length": int(opts["chunk_length"]),
    }
    if vad_filter and isinstance(vad_parameters, dict):
        transcribe_kw["vad_parameters"] = vad_parameters
    segments, info = model.transcribe(
        str(wav_path.resolve()),
        **transcribe_kw,
        word_timestamps=True,
        no_speech_threshold=no_speech_threshold,
        log_prob_threshold=log_prob_threshold,
    )
    local_duration = float(getattr(info, "duration", 0.0) or 0.0)
    progress_total = total_duration_sec if total_duration_sec > 0 else local_duration
    if progress_callback is not None:
        progress_callback(time_offset_sec, progress_total, "STT 시작")
    out: list[dict[str, Any]] = []
    word_items: list[dict[str, Any]] = []
    for seg in segments:
        st = float(getattr(seg, "start", 0.0)) + time_offset_sec
        en = float(getattr(seg, "end", 0.0)) + time_offset_sec
        if progress_callback is not None:
            progress_callback(st, progress_total, "STT 처리 중")
        tx = _cleanup_text(str(getattr(seg, "text", "")).strip())
        if en <= st or not tx:
            continue
        if max_no_speech < 1.0:
            nsp = getattr(seg, "no_speech_prob", None)
            if nsp is not None:
                try:
                    if float(nsp) > max_no_speech:
                        continue
                except (TypeError, ValueError):
                    pass
        words = getattr(seg, "words", None)
        if isinstance(words, list):
            for w in words:
                try:
                    w_st = float(getattr(w, "start", 0.0)) + time_offset_sec
                    w_en = float(getattr(w, "end", 0.0)) + time_offset_sec
                except (TypeError, ValueError):
                    continue
                w_text = _cleanup_text(str(getattr(w, "word", "")).strip())
                if w_en > w_st and w_text:
                    word_items.append({"start_sec": w_st, "end_sec": w_en, "text": w_text})
        out.append({"start_sec": st, "end_sec": en, "text": tx})
    word_lines = _word_timed_lines(word_items)
    if word_lines:
        return _merge_word_lines_with_segment_fallbacks(word_lines, out)
    return out


def _transcribe_with_faster_whisper_chunked(
    wav_path: Path,
    language: str,
    *,
    progress_callback: SttProgressCallback | None = None,
) -> list[dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as e:  # pragma: no cover
        raise SttTranscribeError(f"faster-whisper 미설치: {e}") from e

    duration = _wav_duration_seconds(wav_path)
    if duration <= STT_EXTERNAL_CHUNK_SECONDS * 1.5:
        return []
    opts = resolve_effective_stt_options()
    model = WhisperModel(str(opts["model"]), device="cpu", compute_type=str(opts["compute_type"]))
    lines: list[dict[str, Any]] = []
    ranges = _waveform_chunk_ranges(wav_path, duration_sec=duration)
    if len(ranges) <= 1:
        return []
    logger.info(
        "faster-whisper waveform chunk split: %s chunks, duration=%.2fs",
        len(ranges),
        duration,
    )
    with tempfile.TemporaryDirectory(prefix="stt_fw_chunks_") as td:
        tmp = Path(td)
        for chunk_index, (extract_start, extract_end) in enumerate(ranges, start=1):
            chunk_path = tmp / f"chunk_{chunk_index:04d}.wav"
            _extract_wav_chunk(
                wav_path,
                chunk_path,
                start_sec=extract_start,
                duration_sec=extract_end - extract_start,
            )
            logger.info(
                "faster-whisper chunk STT: %s %.2fs -> %.2fs",
                chunk_index,
                extract_start,
                extract_end,
            )
            chunk_lines = _collect_faster_whisper_lines(
                model=model,
                wav_path=chunk_path,
                language=language,
                opts=opts,
                time_offset_sec=extract_start,
                total_duration_sec=duration,
                progress_callback=progress_callback,
            )
            lines.extend(chunk_lines)
    lines.sort(key=lambda x: (float(x.get("start_sec", 0.0)), float(x.get("end_sec", 0.0))))
    logger.info("faster-whisper chunk STT 사용: duration=%.2fs cues=%s", duration, len(lines))
    _log_large_timing_gaps(lines, label="faster-whisper chunk")
    if progress_callback is not None:
        progress_callback(duration, duration, "STT 완료")
    return lines


def _transcribe_with_faster_whisper(
    wav_path: Path,
    language: str,
    *,
    progress_callback: SttProgressCallback | None = None,
) -> list[dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as e:  # pragma: no cover
        raise SttTranscribeError(f"faster-whisper 미설치: {e}") from e

    opts = resolve_effective_stt_options()
    model_size = str(opts["model"])
    compute_type = str(opts["compute_type"])
    vad_filter = bool(opts["vad_filter"])
    vad_parameters = opts.get("vad_parameters")
    beam_size = int(opts["beam_size"])
    no_speech_threshold = float(opts["no_speech_threshold"])
    log_prob_threshold = float(opts["log_prob_threshold"])
    max_no_speech = float(opts["max_no_speech_prob"])
    condition_on_previous_text = bool(opts["condition_on_previous_text"])
    temperature = float(opts["temperature"])
    compression_ratio_threshold = float(opts["compression_ratio_threshold"])
    chunk_length = int(opts["chunk_length"])
    model = WhisperModel(model_size, device="cpu", compute_type=compute_type)
    transcribe_kw: dict[str, object] = {
        "language": language,
        "vad_filter": vad_filter,
        "beam_size": beam_size,
        "condition_on_previous_text": condition_on_previous_text,
        "temperature": temperature,
        "compression_ratio_threshold": compression_ratio_threshold,
        "chunk_length": chunk_length,
    }
    if vad_filter and isinstance(vad_parameters, dict):
        transcribe_kw["vad_parameters"] = vad_parameters
    segments, info = model.transcribe(
        str(wav_path.resolve()),
        **transcribe_kw,
        word_timestamps=True,
        no_speech_threshold=no_speech_threshold,
        log_prob_threshold=log_prob_threshold,
    )
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    if progress_callback is not None:
        progress_callback(0.0, duration, "STT 시작")
    out: list[dict[str, Any]] = []
    word_items: list[dict[str, Any]] = []
    for seg in segments:
        st = float(getattr(seg, "start", 0.0))
        en = float(getattr(seg, "end", 0.0))
        if progress_callback is not None:
            progress_callback(st, duration, "STT 처리 중")
        tx = _cleanup_text(str(getattr(seg, "text", "")).strip())
        if en <= st or not tx:
            continue
        if max_no_speech < 1.0:
            nsp = getattr(seg, "no_speech_prob", None)
            if nsp is not None:
                try:
                    if float(nsp) > max_no_speech:
                        continue
                except (TypeError, ValueError):
                    pass
        words = getattr(seg, "words", None)
        if isinstance(words, list):
            for w in words:
                try:
                    w_st = float(getattr(w, "start", 0.0))
                    w_en = float(getattr(w, "end", 0.0))
                except (TypeError, ValueError):
                    continue
                w_text = _cleanup_text(str(getattr(w, "word", "")).strip())
                if w_en > w_st and w_text:
                    word_items.append({"start_sec": w_st, "end_sec": w_en, "text": w_text})
        out.append({"start_sec": st, "end_sec": en, "text": tx})
    if progress_callback is not None:
        progress_callback(duration, duration, "STT 완료")
    word_lines = _word_timed_lines(word_items)
    if word_lines:
        merged_lines = _merge_word_lines_with_segment_fallbacks(word_lines, out)
        logger.info(
            "faster-whisper word timestamps 사용: segments=%s words=%s cues=%s",
            len(out),
            len(word_items),
            len(merged_lines),
        )
        _log_large_timing_gaps(merged_lines, label="faster-whisper")
        return merged_lines
    logger.info("faster-whisper segment timestamps 사용: cues=%s", len(out))
    _log_large_timing_gaps(out, label="faster-whisper")
    return out


def _transcribe_with_whisper_cli(
    wav_path: Path,
    language: str,
    *,
    progress_callback: SttProgressCallback | None = None,
) -> list[dict[str, Any]]:
    if progress_callback is not None:
        progress_callback(0.0, 0.0, "whisper CLI 시작")
    with tempfile.TemporaryDirectory(prefix="stt_whisper_") as td:
        out_dir = Path(td)
        cmd = [
            "whisper",
            str(wav_path.resolve()),
            "--task",
            "transcribe",
            "--language",
            language,
            "--output_format",
            "json",
            "--output_dir",
            str(out_dir.resolve()),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3600,
                check=False,
                **_no_window_kwargs(),
            )
        except FileNotFoundError as e:
            raise SttTranscribeError("whisper CLI를 찾지 못했습니다.") from e
        except subprocess.TimeoutExpired as e:
            raise SttTranscribeError("whisper CLI 시간 초과") from e
        if proc.returncode != 0:
            raise SttTranscribeError((proc.stderr or proc.stdout or "whisper CLI 실패").strip())

        json_path = out_dir / f"{wav_path.stem}.json"
        if not json_path.is_file():
            raise SttTranscribeError(f"whisper JSON 출력이 없습니다: {json_path}")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        segments = payload.get("segments")
        if not isinstance(segments, list):
            raise SttTranscribeError("whisper 응답에 segments가 없습니다.")
        out: list[dict[str, Any]] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            try:
                st = float(seg.get("start", 0.0))
                en = float(seg.get("end", 0.0))
            except (TypeError, ValueError):
                continue
            tx = _cleanup_text(str(seg.get("text", "")).strip())
            if en <= st or not tx:
                continue
            out.append({"start_sec": st, "end_sec": en, "text": tx})
        if progress_callback is not None:
            progress_callback(1.0, 1.0, "whisper CLI 완료")
        return out


def transcribe_wav_sentences(
    wav_path: Path,
    *,
    language: str = "ko",
    progress_callback: SttProgressCallback | None = None,
) -> list[dict[str, Any]]:
    if not wav_path.is_file():
        raise SttTranscribeError(f"WAV 파일이 없습니다: {wav_path}")
    errs: list[str] = []
    try:
        lines = _transcribe_with_faster_whisper_chunked(wav_path, language, progress_callback=progress_callback)
        if lines:
            return lines
        errs.append("faster-whisper chunk result empty")
    except Exception as e:
        errs.append(f"faster-whisper chunk failed: {e}")
    try:
        lines = _transcribe_with_faster_whisper(wav_path, language, progress_callback=progress_callback)
        if lines:
            return lines
        errs.append("faster-whisper 결과 비어 있음")
    except Exception as e:
        errs.append(f"faster-whisper 실패: {e}")
    try:
        lines = _transcribe_with_whisper_cli(wav_path, language, progress_callback=progress_callback)
        if lines:
            return lines
        errs.append("whisper CLI 결과 비어 있음")
    except Exception as e:
        errs.append(f"whisper CLI 실패: {e}")
    raise SttTranscribeError(" / ".join(errs))


def _compact_timed_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "idx": i + 1,
            "start_sec": float(x["start_sec"]),
            "end_sec": float(x["end_sec"]),
            "text": str(x.get("text", "")).strip(),
        }
        for i, x in enumerate(lines)
    ]


def _apply_gemini_timed_line_response(
    lines: list[dict[str, Any]],
    raw_response: str,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw_response)
        cands = payload.get("candidates")
        if not isinstance(cands, list) or not cands:
            logger.warning("Gemini 자막 텍스트 교정 응답에 candidates가 없습니다.")
            return lines
        txt = ""
        c0 = cands[0]
        if isinstance(c0, dict):
            content = c0.get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list):
                    txt = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
        if not txt:
            logger.warning("Gemini 자막 텍스트 교정 응답 text가 비어 있습니다.")
            return lines
        parsed = json.loads(txt)
        missing_candidates: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            raw_missing = parsed.get("missing_candidates")
            if isinstance(raw_missing, list):
                missing_candidates = [x for x in raw_missing if isinstance(x, dict)]
            parsed = parsed.get("segments")
        if not isinstance(parsed, list):
            logger.warning("Gemini 자막 텍스트 교정 응답이 JSON 배열 또는 segments 배열이 아닙니다.")
            return lines
        by_idx: dict[int, str] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("idx", 0))
            except (TypeError, ValueError):
                continue
            tx = _cleanup_text(str(item.get("text", "")).strip())
            if idx > 0 and tx:
                by_idx[idx] = tx
        out: list[dict[str, Any]] = []
        changed: list[tuple[int, str, str]] = []
        for i, x in enumerate(lines, start=1):
            y = dict(x)
            if i in by_idx:
                before = _cleanup_text(str(y.get("text", "")).strip())
                after = by_idx[i]
                y["text"] = by_idx[i]
                if before != after:
                    changed.append((i, before, after))
            out.append(y)
        if changed:
            logger.info("Gemini 자막 텍스트 교정 적용: %s/%s개", len(changed), len(lines))
            for idx, before, after in changed[:8]:
                logger.info("Gemini 자막 교정 예: #%s %r -> %r", idx, before[:80], after[:80])
        else:
            logger.info("Gemini 자막 텍스트 교정 변경 없음: %s개 구간", len(lines))
        if missing_candidates:
            logger.warning("Gemini STT 누락 의심 후보: %s개", len(missing_candidates))
            for item in missing_candidates[:10]:
                idx = item.get("idx")
                missing = _cleanup_text(str(item.get("missing_text", "")).strip())
                reason = _cleanup_text(str(item.get("reason", "")).strip())
                if missing:
                    logger.warning("Gemini STT 누락 의심: #%s missing=%r reason=%r", idx, missing[:120], reason[:120])
        return out
    except Exception as e:
        logger.warning("Gemini 자막 텍스트 교정 응답 처리 실패: %s", e)
        return lines


def _gemini_timed_lines_request(
    *,
    prompt: str,
    api_key: str,
    model: str,
    lines: list[dict[str, Any]],
    timeout_sec: float = 180.0,
    max_output_tokens: int = 8192,
) -> list[dict[str, Any]]:
    key = (api_key or os.environ.get("GEMINI_API_KEY", "") or "").strip()
    if not key or not lines:
        return lines
    model_id = _normalize_model_id(model) or DEFAULT_GEMINI_MODEL
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    req = urllib.request.Request(
        _gemini_url(model_id, key),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(e)
        logger.warning("Gemini 자막 텍스트 교정 HTTP 실패: %s %s", e.code, detail[:500])
        return lines
    except urllib.error.URLError as e:
        logger.warning("Gemini 자막 텍스트 교정 연결 실패: %s", e)
        return lines
    return _apply_gemini_timed_line_response(lines, raw)


def refine_timed_lines_with_gemini(
    *,
    lines: list[dict[str, Any]],
    api_key: str,
    model: str,
    timeout_sec: float = 120.0,
) -> list[dict[str, Any]]:
    if not lines:
        return lines
    compact = _compact_timed_lines(lines)
    prompt = (
        "아래 JSON 배열의 text만 한국어 맞춤법/띄어쓰기/문장부호를 다듬어 주세요.\n"
        "절대 시간(start_sec/end_sec)과 idx는 변경하지 마세요.\n"
        "반드시 JSON 배열만 반환하세요. 필드는 idx,start_sec,end_sec,text 그대로 유지.\n\n"
        f"{json.dumps(compact, ensure_ascii=False)}"
    )
    return _gemini_timed_lines_request(
        prompt=prompt,
        api_key=api_key,
        model=model,
        lines=lines,
        timeout_sec=timeout_sec,
        max_output_tokens=4096,
    )


def refine_timed_lines_with_reference_lyrics(
    *,
    lines: list[dict[str, Any]],
    reference_lyrics: str,
    api_key: str,
    model: str,
    timeout_sec: float = 180.0,
) -> list[dict[str, Any]]:
    """STT 구간 text를 원곡 가사에 맞게 교정(타임스탬프·구간 개수 유지)."""
    ref = (reference_lyrics or "").strip()
    if not ref or not lines:
        return lines
    compact = _compact_timed_lines(lines)
    prompt = (
        "당신은 음악 자막 교정기입니다.\n"
        "1) 'stt_segments'는 음성인식(STT) 결과입니다. 일부 단어가 틀릴 수 있습니다.\n"
        "2) 'reference_lyrics'는 정답 원곡 가사 전문입니다.\n"
        "3) 각 구간의 text를 원곡 가사 순서·표현에 맞게 고치세요.\n"
        "4) start_sec, end_sec, idx는 절대 변경하지 마세요. 구간 개수도 유지하세요.\n"
        "5) 원곡에 없는 내용은 가장 가까운 원곡 구절로 바꾸세요. 의미가 크게 다르면 STT를 우선하되 표기만 원곡에 맞춥니다.\n"
        "6) 반드시 stt_segments와 같은 길이의 JSON 배열만 반환하세요.\n\n"
        f"{json.dumps({'reference_lyrics': ref, 'stt_segments': compact}, ensure_ascii=False)}"
    )
    return _gemini_timed_lines_request(
        prompt=prompt,
        api_key=api_key,
        model=model,
        lines=lines,
        timeout_sec=timeout_sec,
        max_output_tokens=8192,
    )


def refine_timed_lines_with_reference_script(
    *,
    lines: list[dict[str, Any]],
    reference_script: str,
    api_key: str,
    model: str,
    timeout_sec: float = 180.0,
) -> list[dict[str, Any]]:
    """Correct STT text against the narration script without changing timing."""
    ref = (reference_script or "").strip()
    if not ref or not lines:
        return lines
    compact = _compact_timed_lines(lines)
    prompt = (
        "당신은 내레이션 자막의 텍스트 교정기입니다.\n"
        "목표: faster-whisper가 만든 각 구간의 text만 reference_script 표기에 맞게 교정합니다.\n"
        "절대 규칙:\n"
        "1) idx, start_sec, end_sec, 순서, 구간 개수는 절대 변경하지 마세요.\n"
        "2) 구간을 합치거나 나누지 마세요. 다른 구간으로 문장을 이동하지 마세요.\n"
        "3) 시간 싱크를 맞추려고 text를 늘리거나 줄이지 마세요.\n"
        "4) STT가 통째로 빠뜨린 긴 구절을 text에 새로 채워 넣지 마세요.\n"
        "   대신 reference_script와 비교해 누락이 의심되면 missing_candidates에 보고하세요.\n"
        "5) 다만 STT가 들은 단어를 잘못 표기한 경우에는 reference_script의 정확한 표기로 고치세요.\n"
        "6) 맞춤법, 띄어쓰기, 조사, 고유명사, 반복 오인식은 적극적으로 교정하세요.\n"
        "   예: 오늘을은 -> 오늘은, 풀립 -> 풀잎, 꿰꼬리 -> 꾀꼬리, 새하루 -> 새 하루.\n"
        "7) 발화량, 싱크, 구간 길이를 추정하지 마세요. text의 오인식/오탈자만 교정하세요.\n"
        "8) 반드시 JSON 객체만 반환하세요.\n"
        "9) segments는 stt_segments와 같은 길이의 배열이어야 하며, 각 항목 필드는 idx,start_sec,end_sec,text를 유지하세요.\n"
        "10) missing_candidates는 배열입니다. 누락 의심이 없으면 빈 배열을 반환하세요.\n"
        "    각 항목은 idx, missing_text, reason 필드를 사용하세요.\n\n"
        f"{json.dumps({'reference_script': ref, 'stt_segments': compact}, ensure_ascii=False)}"
    )
    return _gemini_timed_lines_request(
        prompt=prompt,
        api_key=api_key,
        model=model,
        lines=lines,
        timeout_sec=timeout_sec,
        max_output_tokens=16384,
    )
