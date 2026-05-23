from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from app.services.gemini_model_catalog import DEFAULT_GEMINI_MODEL

logger = logging.getLogger(__name__)
_STT_RUNTIME_OPTIONS: dict[str, object] = {}


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
    }


def log_effective_stt_options(*, heading: str = "STT 옵션") -> None:
    opts = resolve_effective_stt_options()
    logger.info(
        "%s: model=%s compute_type=%s vad_filter=%s beam_size=%s "
        "no_speech_threshold=%.3f max_no_speech_prob=%.3f log_prob_threshold=%.3f",
        heading,
        opts["model"],
        opts["compute_type"],
        opts["vad_filter"],
        opts["beam_size"],
        float(opts["no_speech_threshold"]),
        float(opts["max_no_speech_prob"]),
        float(opts["log_prob_threshold"]),
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


def _transcribe_with_faster_whisper(wav_path: Path, language: str) -> list[dict[str, Any]]:
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
    model = WhisperModel(model_size, device="cpu", compute_type=compute_type)
    transcribe_kw: dict[str, object] = {
        "language": language,
        "vad_filter": vad_filter,
        "beam_size": beam_size,
    }
    if vad_filter and isinstance(vad_parameters, dict):
        transcribe_kw["vad_parameters"] = vad_parameters
    segments, _info = model.transcribe(
        str(wav_path.resolve()),
        **transcribe_kw,
        word_timestamps=False,
        no_speech_threshold=no_speech_threshold,
        log_prob_threshold=log_prob_threshold,
    )
    out: list[dict[str, Any]] = []
    for seg in segments:
        st = float(getattr(seg, "start", 0.0))
        en = float(getattr(seg, "end", 0.0))
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
        out.append({"start_sec": st, "end_sec": en, "text": tx})
    return out


def _transcribe_with_whisper_cli(wav_path: Path, language: str) -> list[dict[str, Any]]:
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
        return out


def transcribe_wav_sentences(wav_path: Path, *, language: str = "ko") -> list[dict[str, Any]]:
    if not wav_path.is_file():
        raise SttTranscribeError(f"WAV 파일이 없습니다: {wav_path}")
    errs: list[str] = []
    try:
        lines = _transcribe_with_faster_whisper(wav_path, language)
        if lines:
            return _merge_lines(lines)
        errs.append("faster-whisper 결과 비어 있음")
    except Exception as e:
        errs.append(f"faster-whisper 실패: {e}")
    try:
        lines = _transcribe_with_whisper_cli(wav_path, language)
        if lines:
            return _merge_lines(lines)
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
            return lines
        parsed = json.loads(txt)
        if not isinstance(parsed, list):
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
        for i, x in enumerate(lines, start=1):
            y = dict(x)
            if i in by_idx:
                y["text"] = by_idx[i]
            out.append(y)
        return out
    except Exception:
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
    except (urllib.error.HTTPError, urllib.error.URLError):
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
