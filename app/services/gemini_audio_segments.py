from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from app.services.gemini_model_catalog import DEFAULT_GEMINI_MODEL
from app.services.llm_errors import LlmProviderError

logger = logging.getLogger(__name__)


class GeminiAudioSegmentsError(LlmProviderError):
    pass


def _normalize_model_id(model: str) -> str:
    m = model.strip()
    if m.startswith("models/"):
        m = m[len("models/") :]
    return m


def _gemini_url(model_id: str, api_key: str) -> str:
    qs = urlencode({"key": api_key})
    return (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model_id}:generateContent?{qs}"
    )


def _extract_text(payload: dict[str, Any]) -> str:
    err = payload.get("error")
    if isinstance(err, dict) and err.get("message"):
        raise GeminiAudioSegmentsError(str(err["message"]))
    fb = payload.get("promptFeedback")
    if isinstance(fb, dict):
        br = fb.get("blockReason")
        if br:
            raise GeminiAudioSegmentsError(f"프롬프트가 차단되었습니다: {br}")

    cands = payload.get("candidates")
    if not isinstance(cands, list) or not cands:
        raise GeminiAudioSegmentsError("응답에 candidates가 없습니다.")
    saw_max_tokens = False
    for cand in cands:
        if not isinstance(cand, dict):
            continue
        if cand.get("finishReason") == "MAX_TOKENS":
            saw_max_tokens = True
        content = cand.get("content")
        if isinstance(content, dict):
            parts = content.get("parts")
            if isinstance(parts, list):
                out: list[str] = []
                for p in parts:
                    if isinstance(p, dict) and isinstance(p.get("text"), str):
                        out.append(p["text"])
                txt = "".join(out).strip()
                if txt:
                    return txt
        t = cand.get("text")
        if isinstance(t, str) and t.strip():
            return t.strip()
        o = cand.get("output")
        if isinstance(o, str) and o.strip():
            return o.strip()
    first = cands[0] if cands else {}
    if isinstance(first, dict):
        keys = ",".join(sorted(first.keys()))
        fr = first.get("finishReason")
        if saw_max_tokens:
            raise GeminiAudioSegmentsError(
                f"응답이 토큰 제한에 걸렸습니다(finishReason={fr}). "
                "더 짧은 출력으로 재시도하세요."
            )
        raise GeminiAudioSegmentsError(
            f"응답에서 텍스트를 찾지 못했습니다. candidate keys=[{keys}], finishReason={fr}"
        )
    raise GeminiAudioSegmentsError("응답에서 텍스트를 찾지 못했습니다.")


def _strip_markdown_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _extract_json_array(text: str) -> list[Any]:
    s = _strip_markdown_fence(text)
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for k in ("segments", "items", "data", "result"):
                v = parsed.get(k)
                if isinstance(v, list):
                    return v
    except json.JSONDecodeError:
        pass
    i = s.find("[")
    j = s.rfind("]")
    if i >= 0 and j > i:
        chunk = s[i : j + 1]
        parsed = json.loads(chunk)
        if isinstance(parsed, list):
            return parsed
    oi = s.find("{")
    oj = s.rfind("}")
    if oi >= 0 and oj > oi:
        chunk_obj = s[oi : oj + 1]
        try:
            parsed_obj = json.loads(chunk_obj)
            if isinstance(parsed_obj, dict):
                for k in ("segments", "items", "data", "result"):
                    v = parsed_obj.get(k)
                    if isinstance(v, list):
                        return v
        except json.JSONDecodeError:
            pass
    raise GeminiAudioSegmentsError("Gemini 응답에서 JSON 배열을 찾지 못했습니다.")


def _parse_time_token_to_sec(tok: str) -> float | None:
    s = (tok or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}(?:\.\d+)?))?", s)
    if m:
        a = float(m.group(1))
        b = float(m.group(2))
        c = m.group(3)
        if c is None:
            return (a * 60.0) + b
        return (a * 3600.0) + (b * 60.0) + float(c)
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v >= 0 else None


def _extract_segments_from_timecoded_text(text: str) -> list[Any]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    pat = re.compile(
        r"(?P<st>\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?|\d+(?:\.\d+)?)\s*"
        r"(?:~|-|–|—)\s*"
        r"(?P<en>\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?|\d+(?:\.\d+)?)"
    )
    out: list[Any] = []
    for i, ln in enumerate(lines, start=1):
        m = pat.search(ln)
        if not m:
            continue
        st = _parse_time_token_to_sec(m.group("st"))
        en = _parse_time_token_to_sec(m.group("en"))
        if st is None or en is None or en <= st:
            continue
        label = ln.split("(")[0].strip()
        if not label:
            label = f"Section {i}"
        prompt = ""
        if "프롬프트:" in ln:
            prompt = ln.split("프롬프트:", 1)[1].strip()
        elif "image_prompt:" in ln.lower():
            prompt = ln.split(":", 1)[1].strip()
        out.append(
            {
                "start_sec": st,
                "end_sec": en,
                "label": label,
                "image_prompt": prompt,
            }
        )
    return out


def _normalize_segments(raw: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        try:
            st = float(item.get("start_sec"))
            en = float(item.get("end_sec"))
        except (TypeError, ValueError):
            continue
        if en <= st:
            continue
        label = str(item.get("label", f"Section {idx}")).strip() or f"Section {idx}"
        image_prompt = str(
            item.get("image_prompt", item.get("visual_prompt", item.get("narration", "")))
        ).strip()
        out.append(
            {
                "start_sec": st,
                "end_sec": en,
                "label": label,
                "image_prompt": image_prompt,
            }
        )
    out.sort(key=lambda x: float(x["start_sec"]))
    if not out:
        raise GeminiAudioSegmentsError("유효한 구간이 없습니다.")
    return out


def _rescale_segments_to_duration(
    segments: list[dict[str, Any]], target_duration_sec: float
) -> list[dict[str, Any]]:
    """모델이 전체 길이 대비 지나치게 짧은 초 단위를 줄 때 선형 보정."""
    if not segments or target_duration_sec <= 0:
        return segments
    try:
        max_end = max(float(s["end_sec"]) for s in segments)
    except (TypeError, ValueError, KeyError):
        return segments
    if max_end <= 0:
        return segments
    if max_end >= target_duration_sec * 0.85:
        return segments
    if max_end >= target_duration_sec * 0.5 or target_duration_sec <= 15:
        return segments
    scale = target_duration_sec / max_end
    logger.warning(
        "Gemini 구간 타임스탬프가 실제 길이 대비 짧음(max=%.2fs, audio=%.2fs) → %.2fx 보정",
        max_end,
        target_duration_sec,
        scale,
    )
    out: list[dict[str, Any]] = []
    for seg in segments:
        item = dict(seg)
        try:
            st = float(item["start_sec"]) * scale
            en = float(item["end_sec"]) * scale
        except (TypeError, ValueError, KeyError):
            out.append(item)
            continue
        item["start_sec"] = max(0.0, st)
        item["end_sec"] = min(target_duration_sec, en)
        out.append(item)
    out.sort(key=lambda x: float(x["start_sec"]))
    if out:
        out[-1]["end_sec"] = target_duration_sec
        out[0]["start_sec"] = 0.0
    return _normalize_segments(out)


_LYRICS_PROMPT_MAX_CHARS = 12_000


def _reference_lyrics_prompt_block(reference_lyrics: str) -> str:
    ref = (reference_lyrics or "").strip()
    if not ref:
        return ""
    if len(ref) > _LYRICS_PROMPT_MAX_CHARS:
        ref = ref[:_LYRICS_PROMPT_MAX_CHARS] + "\n…(이하 생략)"
    return (
        "\n\n참고 자료 — 원곡 가사 전문(오디오와 함께 활용):\n"
        "· 구간 경계(start_sec/end_sec)는 오디오와 가사의 흐름(절·후렴·브릿지 등)에 맞추세요.\n"
        "· label은 짧은 구간 제목(예: 1절, 후렴)으로, 가사 문장을 그대로 넣지 마세요.\n"
        "· image_prompt는 해당 구간 가사의 분위기·상징·장면을 한국어로 묘사하되, "
        "가사·자막 문장을 그대로 복사하지 마세요.\n"
        "--- 원곡 가사 ---\n"
        f"{ref}\n"
        "--- 끝 ---"
    )


def _duration_prompt_block(audio_duration_sec: float) -> str:
    if audio_duration_sec <= 0:
        return ""
    dur = float(audio_duration_sec)
    mm = int(dur // 60)
    ss = dur % 60
    return (
        f"\n\n오디오 전체 길이: 약 {dur:.2f}초 ({mm}분 {ss:.1f}초).\n"
        "· start_sec/end_sec는 **초(second)** 단위 실수입니다. 분·백분율·구간 번호가 아닙니다.\n"
        f"· 첫 구간 start_sec=0, 마지막 구간 end_sec는 {dur:.1f}초 전후(±3초)로 맞추세요.\n"
        "· 구간들이 오디오 전체를 빠짐없이 덮도록 시간 순서대로 나열하세요."
    )


def _segment_prompts(reference_lyrics: str, audio_duration_sec: float) -> tuple[str, str]:
    lyrics_block = _reference_lyrics_prompt_block(reference_lyrics)
    duration_block = _duration_prompt_block(audio_duration_sec)
    prompt_primary = (
        "다음 오디오를 시간 순서대로 구간 분할하고, 각 구간마다 배경 이미지 생성용 프롬프트를 작성해 주세요.\n"
        "반드시 JSON 배열만 반환하세요. 마크다운 금지.\n"
        "각 항목 필드: start_sec(number), end_sec(number), label(string), image_prompt(string).\n"
        "중요 규칙:\n"
        "1) image_prompt는 해당 구간 분위기·장면을 묘사하는 한국어 문장(가사/자막 텍스트 금지).\n"
        "2) 텍스트·자막·워터마크가 들어간 이미지는 만들지 않는다고 가정하고 장면만 설명합니다.\n"
        "3) 가사나 들리는 대사를 그대로 적지 마세요.\n"
        "4) start_sec/end_sec는 초 단위 소수점으로 정확히, 구간은 시간 순서/비중복."
        f"{duration_block}"
        f"{lyrics_block}"
    )
    prompt_fallback = (
        "오디오를 시간 순서대로 6~12개 구간으로 분할해 주세요.\n"
        "JSON 배열만 반환하고 필드는 start_sec,end_sec,label,image_prompt만 사용하세요.\n"
        "image_prompt는 배경 이미지용 한국어 장면 설명(최대 80자)."
        f"{duration_block}"
        f"{lyrics_block}"
    )
    return prompt_primary, prompt_fallback


def gemini_segment_audio_file(
    *,
    audio_path: Path,
    mime_type: str,
    api_key: str,
    model: str,
    reference_lyrics: str = "",
    audio_duration_sec: float = 0.0,
    timeout_sec: float = 240.0,
) -> list[dict[str, Any]]:
    key = (api_key or os.environ.get("GEMINI_API_KEY", "") or "").strip()
    if not key:
        raise GeminiAudioSegmentsError("Gemini API 키가 비어 있습니다.")
    if not audio_path.is_file():
        raise GeminiAudioSegmentsError(f"오디오 파일이 없습니다: {audio_path}")
    preferred_model_id = _normalize_model_id(model)
    if not preferred_model_id:
        raise GeminiAudioSegmentsError(f"Gemini 모델 ID를 입력하세요. (예: {DEFAULT_GEMINI_MODEL})")

    audio_bytes = audio_path.read_bytes()
    b64_audio = __import__("base64").b64encode(audio_bytes).decode("ascii")
    dur = max(0.0, float(audio_duration_sec or 0.0))
    prompt_primary, prompt_fallback = _segment_prompts(reference_lyrics, dur)
    if dur > 0:
        logger.info("Gemini 구간 분석: 오디오 길이 %.2f초 프롬프트에 포함", dur)
    if (reference_lyrics or "").strip():
        logger.info(
            "Gemini 구간 분석: 원곡 가사 참고 포함 (%s자)",
            min(len((reference_lyrics or "").strip()), _LYRICS_PROMPT_MAX_CHARS),
        )

    def _request_once(model_id: str, prompt: str, max_output_tokens: int) -> list[dict[str, Any]]:
        dump_chars_raw = (os.environ.get("CONTENT_PRODUCTION_GEMINI_DUMP_CHARS", "") or "").strip()
        try:
            dump_chars = int(dump_chars_raw) if dump_chars_raw else 6000
        except ValueError:
            dump_chars = 6000
        logger.debug(
            "Gemini audio request model=%s mime=%s audio_bytes=%s max_output_tokens=%s prompt=%r",
            model_id,
            mime_type,
            len(audio_bytes),
            max_output_tokens,
            prompt[: min(len(prompt), dump_chars)],
        )
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": b64_audio,
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            _gemini_url(model_id, key),
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw_txt = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            msg = err_body[:800]
            try:
                ej = json.loads(err_body)
                em = ej.get("error") if isinstance(ej, dict) else None
                if isinstance(em, dict) and em.get("message"):
                    msg = str(em["message"])
            except json.JSONDecodeError:
                pass
            raise GeminiAudioSegmentsError(f"HTTP {e.code}: {msg or e.reason}") from e
        except urllib.error.URLError as e:
            raise GeminiAudioSegmentsError(f"연결 실패: {e.reason}") from e

        logger.debug(
            "Gemini audio raw response chars=%s prefix=%r",
            len(raw_txt),
            raw_txt[: min(len(raw_txt), dump_chars)],
        )

        try:
            payload = json.loads(raw_txt)
        except json.JSONDecodeError as e:
            logger.warning("Gemini audio segments JSON 파싱 실패: %s raw_prefix=%r", e, raw_txt[:400])
            raise GeminiAudioSegmentsError(f"응답 JSON 파싱 실패: {e}") from e
        if not isinstance(payload, dict):
            raise GeminiAudioSegmentsError("응답 루트가 객체가 아닙니다.")
        usage = payload.get("usageMetadata")
        if isinstance(usage, dict):
            logger.debug(
                "Gemini audio usage prompt=%s total=%s thoughts=%s",
                usage.get("promptTokenCount"),
                usage.get("totalTokenCount"),
                usage.get("thoughtsTokenCount"),
            )
        text = _extract_text(payload)
        logger.debug("Gemini audio extracted text prefix=%r", text[: min(len(text), dump_chars)])
        try:
            arr = _extract_json_array(text)
        except GeminiAudioSegmentsError:
            logger.debug("Gemini audio JSON 배열 추출 실패, 타임코드 텍스트 fallback 시도")
            arr = _extract_segments_from_timecoded_text(text)
            if not arr:
                logger.warning(
                    "Gemini audio fallback 실패: text_prefix=%r",
                    text[: min(len(text), dump_chars)],
                )
                raise
        logger.debug("Gemini audio parsed segments count=%s", len(arr))
        return _normalize_segments(arr)

    fallback_model_id = _normalize_model_id(DEFAULT_GEMINI_MODEL)
    attempts: list[tuple[str, str, int]] = [
        (preferred_model_id, prompt_primary, 4096),
        (preferred_model_id, prompt_fallback, 1024),
    ]
    if fallback_model_id and fallback_model_id != preferred_model_id:
        attempts.append((fallback_model_id, prompt_fallback, 1024))

    last_err: GeminiAudioSegmentsError | None = None
    for idx, (mid, prompt, out_toks) in enumerate(attempts, start=1):
        try:
            if idx == 2:
                logger.info("Gemini 구간 분석 1차 실패(MAX_TOKENS), 간결 프롬프트로 재시도합니다.")
            elif idx >= 3:
                logger.info(
                    "Gemini 구간 분석 재시도: 모델 fallback %s -> %s",
                    preferred_model_id,
                    mid,
                )
            segs = _request_once(mid, prompt, out_toks)
            if dur > 0:
                segs = _rescale_segments_to_duration(segs, dur)
            return segs
        except GeminiAudioSegmentsError as e:
            last_err = e
            msg = str(e)
            if "토큰 제한" in msg or "MAX_TOKENS" in msg:
                continue
            raise
    if last_err is not None:
        raise last_err
    raise GeminiAudioSegmentsError("Gemini 구간 분석 실패: 알 수 없는 오류")
