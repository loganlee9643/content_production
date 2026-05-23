from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from app.services.gemini_audio_segments import (
    GeminiAudioSegmentsError,
    _extract_json_array,
    _extract_text,
    _normalize_model_id,
)
from app.services.gemini_model_catalog import DEFAULT_GEMINI_MODEL
from app.services.subtitle_lyrics_sections import (
    LyricSection,
    _best_stt_match_for_line,
    _line_match_score,
    _next_stt_hint_after,
    deduplicate_timed_lines,
    filter_stt_hints_before_sec,
    parse_lyrics_sections,
    section_follows_chorus,
    stt_section_start_sec,
)

logger = logging.getLogger(__name__)
DebugLog = Callable[[str], None] | None

_LYRICS_MAX = 12_000
_POST_CHORUS_VOCAL_GAP_SEC = 8.0


def _emit_debug(debug_log: DebugLog, message: str, *args: Any) -> None:
    txt = message % args if args else message
    logger.info(txt)
    if debug_log:
        try:
            debug_log(txt)
        except Exception:
            logger.debug("debug_log 콜백 실패", exc_info=True)


def _clamp_post_chorus_shift(shift_sec: float) -> float:
    """후렴 직후 절 보정은 과도한 오탐 이동을 막기 위해 절대값을 제한."""
    cap = float(_POST_CHORUS_VOCAL_GAP_SEC)
    if shift_sec > cap:
        return cap
    if shift_sec < -cap:
        return -cap
    return shift_sec


def _normalize_subtitle_lines(raw: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            st = float(item.get("start_sec"))
            en = float(item.get("end_sec"))
        except (TypeError, ValueError):
            continue
        text = " ".join(str(item.get("text", "")).split()).strip()
        if en <= st or not text:
            continue
        out.append({"start_sec": st, "end_sec": en, "text": text})
    out.sort(key=lambda x: float(x["start_sec"]))
    if not out:
        raise GeminiAudioSegmentsError("보컬 자막 구간을 파싱하지 못했습니다.")
    return out


def _clamp_lines_to_window(
    lines: list[dict[str, Any]],
    *,
    min_start_sec: float,
    max_end_sec: float,
) -> list[dict[str, Any]]:
    lo = max(0.0, float(min_start_sec or 0.0))
    hi = max(0.0, float(max_end_sec or 0.0))
    out: list[dict[str, Any]] = []
    for ln in lines:
        st = float(ln["start_sec"])
        en = float(ln["end_sec"])
        if hi > 0 and st >= hi:
            continue
        if en <= lo:
            continue
        if st < lo:
            st = lo
        if hi > 0 and en > hi:
            en = hi
        if en <= st:
            continue
        row = dict(ln)
        row["start_sec"] = st
        row["end_sec"] = en
        out.append(row)
    return out


def _gemini_vocal_timing_request(
    *,
    audio_path: Path,
    mime_type: str,
    prompt: str,
    api_key: str,
    model: str,
    timeout_sec: float,
) -> list[dict[str, Any]]:
    key = (api_key or os.environ.get("GEMINI_API_KEY", "") or "").strip()
    if not key:
        raise GeminiAudioSegmentsError("Gemini API 키가 비어 있습니다.")
    if not audio_path.is_file():
        raise GeminiAudioSegmentsError(f"오디오 파일이 없습니다: {audio_path}")

    model_id = _normalize_model_id(model) or DEFAULT_GEMINI_MODEL
    audio_bytes = audio_path.read_bytes()
    b64_audio = __import__("base64").b64encode(audio_bytes).decode("ascii")

    from app.services.gemini_audio_segments import _gemini_url
    import urllib.error
    import urllib.request

    body: dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": mime_type, "data": b64_audio}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
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
    logger.info("Gemini 보컬 자막 타이밍 요청 (모델=%s)", model_id)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw_txt = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise GeminiAudioSegmentsError(f"HTTP {e.code}: {err_body[:400]}") from e
    except urllib.error.URLError as e:
        raise GeminiAudioSegmentsError(f"연결 실패: {e.reason}") from e

    payload = json.loads(raw_txt)
    if not isinstance(payload, dict):
        raise GeminiAudioSegmentsError("응답 형식 오류")
    text = _extract_text(payload)
    arr = _extract_json_array(text)
    return _normalize_subtitle_lines(arr)


def _build_section_prompt(
    *,
    section: LyricSection,
    audio_duration_sec: float,
    min_start_sec: float,
    stt_hints: list[dict[str, Any]] | None,
) -> str:
    ref = "\n".join(section.lines).strip()
    dur = max(0.0, float(audio_duration_sec or 0.0))
    dur_block = ""
    if dur > 0:
        dur_block = (
            f"\n오디오 전체 길이: 약 {dur:.2f}초. "
            f"이번 섹션 자막은 {min_start_sec:.1f}초 이후에만 배치하세요."
        )
    hints_block = ""
    filtered = filter_stt_hints_before_sec(stt_hints, min_start_sec=min_start_sec)
    if filtered:
        compact = [
            {
                "idx": i + 1,
                "start_sec": float(x.get("start_sec", 0)),
                "end_sec": float(x.get("end_sec", 0)),
                "text": str(x.get("text", "")).strip()[:80],
            }
            for i, x in enumerate(filtered[:30])
        ]
        hints_block = (
            "\n\nstt_hints는 참고용입니다. 간주 구간의 오인식은 무시하고, "
            "실제 보컬 시작 시각에 맞추세요.\n"
            f"{json.dumps(compact, ensure_ascii=False)}"
        )

    return (
        "음악 자막 타이밍 전문가입니다. 첨부 오디오를 듣고 **이번 섹션 가사만** "
        "보컬이 나올 때 자막으로 배치하세요.\n"
        "반드시 JSON 배열만 반환하세요. 필드: start_sec(number), end_sec(number), text(string).\n"
        "규칙:\n"
        f"1) text는 아래 [{section.name}] 가사만 순서대로 나눠 넣으세요.\n"
        f"2) **모든 start_sec는 {min_start_sec:.1f}초 이상**이어야 합니다. "
        "그 이전(간주·전주)에는 자막을 두지 마세요.\n"
        "3) 인트로·간주·아웃트로 등 보컬이 없는 구간에는 자막을 만들지 마세요.\n"
        "4) start_sec/end_sec는 초 단위 실수, 구간은 시간 순서·비중복.\n"
        "5) 한 cue의 text는 너무 길지 않게(대략 1~2문장).\n"
        "6) 다른 섹션 가사는 포함하지 마세요.\n"
        f"{dur_block}"
        f"\n\n--- [{section.name}] 가사 ---\n{ref}\n--- 끝 ---"
        f"{hints_block}"
    )


def _estimate_min_start_for_section(
    section: LyricSection,
    *,
    index: int,
    sections: list[LyricSection],
    cursor_sec: float,
    stt_hints: list[dict[str, Any]] | None,
) -> float:
    """섹션별 자막 최소 시작 시각.

    영상용 구간 분석(wav_segments)은 사용하지 않습니다.
    후렴+간주가 한 블록으로 잡혀 자막이 늦게 밀리는 문제를 피하기 위함입니다.
    """
    after_chorus = section_follows_chorus(sections, index)
    search_after = cursor_sec if after_chorus else 0.0
    stt_start = stt_section_start_sec(
        section,
        stt_hints,
        after_sec=search_after,
        prefer_later_match=after_chorus,
    )

    if stt_start is not None and stt_start >= search_after - 5.0:
        return max(0.0, cursor_sec, stt_start)

    if after_chorus:
        return max(cursor_sec, cursor_sec + _POST_CHORUS_VOCAL_GAP_SEC)

    return max(0.0, cursor_sec)


def _retime_single_section(
    *,
    audio_path: Path,
    mime_type: str,
    section: LyricSection,
    min_start_sec: float,
    audio_duration_sec: float,
    stt_hints: list[dict[str, Any]] | None,
    api_key: str,
    model: str,
    timeout_sec: float,
) -> list[dict[str, Any]]:
    prompt = _build_section_prompt(
        section=section,
        audio_duration_sec=audio_duration_sec,
        min_start_sec=min_start_sec,
        stt_hints=stt_hints,
    )
    lines = _gemini_vocal_timing_request(
        audio_path=audio_path,
        mime_type=mime_type,
        prompt=prompt,
        api_key=api_key,
        model=model,
        timeout_sec=timeout_sec,
    )
    dur = max(0.0, float(audio_duration_sec or 0.0))
    return _clamp_lines_to_window(
        lines,
        min_start_sec=min_start_sec,
        max_end_sec=dur if dur > 0 else 0.0,
    )


def _apply_post_chorus_timing_fix(
    lines: list[dict[str, Any]],
    sections: list[LyricSection],
    stt_hints: list[dict[str, Any]],
    audio_duration_sec: float,
    *,
    debug_log: DebugLog = None,
) -> list[dict[str, Any]]:
    """가사 교정된 cue 목록에 후렴 직후 절 타이밍 보정만 적용."""
    out = [dict(ln) for ln in lines]
    dur = max(0.0, float(audio_duration_sec or 0.0))
    cursor = 0.0

    for i, sec in enumerate(sections):
        if not section_follows_chorus(sections, i):
            continue

        first_idx: int | None = None
        for j, cue in enumerate(out):
            for line in sec.lines:
                if _line_match_score(line, str(cue.get("text", ""))) >= 0.75:
                    first_idx = j
                    break
            if first_idx is not None:
                break

        if first_idx is None:
            continue

        search_after = cursor
        if first_idx > 0:
            search_after = max(search_after, float(out[first_idx - 1]["end_sec"]))
        elif i > 0:
            prev = sections[i - 1]
            for j, cue in enumerate(out):
                for line in prev.lines:
                    if _line_match_score(line, str(cue.get("text", ""))) >= 0.75:
                        search_after = max(search_after, float(cue["end_sec"]))

        floor = stt_section_start_sec(
            sec, stt_hints, after_sec=search_after, prefer_later_match=True
        )
        if floor is None:
            _emit_debug(
                debug_log,
                "후렴보정[%s] floor 없음 (search_after=%.2fs, first_st=%.2fs)",
                sec.name,
                search_after,
                float(out[first_idx]["start_sec"]),
            )
            continue

        first_st = float(out[first_idx]["start_sec"])
        raw_shift = floor - first_st
        shift = _clamp_post_chorus_shift(raw_shift)
        # 이미 후렴/간주 경계(search_after) 이후에서 절이 시작한 경우에는
        # 중간줄 anchor로 인한 과보정(예: +6s) 방지를 위해 전진 이동을 더 보수적으로 적용.
        if shift > 0 and first_st >= search_after - 0.5 and shift <= 6.5:
            _emit_debug(
                debug_log,
                "후렴보정[%s] 전진 과보정 방지: first_st=%.2fs search_after=%.2fs shift=%+.2fs -> 0.00s",
                sec.name,
                first_st,
                search_after,
                shift,
            )
            shift = 0.0
        _emit_debug(
            debug_log,
            "후렴보정[%s] search_after=%.2fs floor=%.2fs first_st=%.2fs shift=%+.2fs(raw=%+.2fs)",
            sec.name,
            search_after,
            floor,
            first_st,
            shift,
            raw_shift,
        )
        if shift != raw_shift:
            _emit_debug(
                debug_log,
                "후렴보정[%s] 과보정 제한 적용: raw=%+.2fs -> capped=%+.2fs",
                sec.name,
                raw_shift,
                shift,
            )
        # 후렴 직후 절은 STT floor 기준으로 "늦게/빠르게" 모두 보정한다.
        # 기존엔 늦게 미는 경우만 처리해서, 실제보다 4~5초 늦는 케이스를 당겨오지 못했다.
        if abs(shift) < 2.0:
            _emit_debug(debug_log, "후렴보정[%s] |shift|<2.0s → 보정 스킵", sec.name)
            continue
        end_idx = min(len(out), first_idx + len(sec.lines))
        first_text = str(out[first_idx].get("text", "")).strip()
        _emit_debug(
            debug_log,
            "후렴보정[%s] 적용: cue[%s:%s) 첫줄=%r",
            sec.name,
            first_idx,
            end_idx,
            first_text,
        )
        for j in range(first_idx, end_idx):
            out[j]["start_sec"] = float(out[j]["start_sec"]) + shift
            out[j]["end_sec"] = float(out[j]["end_sec"]) + shift

        if dur > 0:
            for j in range(first_idx, end_idx):
                out[j]["start_sec"] = min(float(out[j]["start_sec"]), dur)
                out[j]["end_sec"] = min(float(out[j]["end_sec"]), dur)

        cursor = float(out[end_idx - 1]["end_sec"]) if end_idx > first_idx else cursor

    out.sort(key=lambda x: float(x["start_sec"]))
    return deduplicate_timed_lines(out)


def _apply_opening_section_timing_fix(
    lines: list[dict[str, Any]],
    sections: list[LyricSection],
    stt_hints: list[dict[str, Any]],
    audio_duration_sec: float,
    *,
    debug_log: DebugLog = None,
) -> list[dict[str, Any]]:
    """첫 절이 0초 부근으로 과도하게 당겨진 경우 STT 첫 앵커로 정렬."""
    if not lines or not sections:
        return lines
    out = [dict(ln) for ln in lines]
    sec = sections[0]
    if not sec.lines:
        return out
    floor = stt_section_start_sec(sec, stt_hints, after_sec=0.0, prefer_later_match=True)
    first_hint = _next_stt_hint_after(stt_hints, after_sec=5.0, used_starts=set())
    first_hint_st = float(first_hint[0]) if first_hint is not None else None
    # 가사 매칭 floor가 과도하게 늦으면, 오프닝 첫 유효 STT 힌트로 보정한다.
    if first_hint_st is not None:
        if floor is None:
            floor = first_hint_st
        elif floor - first_hint_st > 4.0:
            _emit_debug(
                debug_log,
                "오프닝보정[%s] floor 과대 보정: %.2fs -> 첫 STT 힌트 %.2fs",
                sec.name,
                floor,
                first_hint_st,
            )
            floor = first_hint_st
    # 첫 줄이 0초로 오인식되는 경우, 약간 뒤(5초)부터 다시 찾는다.
    if floor is not None and floor < 3.0:
        retry = stt_section_start_sec(sec, stt_hints, after_sec=5.0, prefer_later_match=True)
        if retry is not None:
            _emit_debug(
                debug_log,
                "오프닝보정[%s] floor 재탐색: %.2fs -> %.2fs",
                sec.name,
                floor,
                retry,
            )
            floor = retry
    if floor is None:
        _emit_debug(debug_log, "오프닝보정[%s] floor 없음", sec.name)
        return out

    first_idx: int | None = None
    for j, cue in enumerate(out):
        for line in sec.lines:
            if _line_match_score(line, str(cue.get("text", ""))) >= 0.75:
                first_idx = j
                break
        if first_idx is not None:
            break
    if first_idx is None:
        return out

    first_st = float(out[first_idx]["start_sec"])
    raw_shift = float(floor) - first_st
    # 오프닝 보정은 "너무 이른 시작"만 늦추는 방향으로 제한.
    if raw_shift <= 2.0 or first_st > 2.0 or float(floor) < 8.0:
        _emit_debug(
            debug_log,
            "오프닝보정[%s] 스킵: floor=%.2fs first_st=%.2fs raw_shift=%+.2fs",
            sec.name,
            floor,
            first_st,
            raw_shift,
        )
        return out
    end_idx = min(len(out), first_idx + len(sec.lines))
    # 다음 섹션 시작 시각을 침범하면 가사 순서가 섞이므로 오프닝 보정은 제한한다.
    max_non_overlap_shift = 8.0
    if end_idx < len(out):
        next_st = float(out[end_idx]["start_sec"])
        sec_last_en = float(out[end_idx - 1]["end_sec"])
        max_non_overlap_shift = min(max_non_overlap_shift, max(0.0, next_st - sec_last_en - 0.12))
    shift = min(raw_shift, max_non_overlap_shift)
    if shift < 2.0:
        _emit_debug(
            debug_log,
            "오프닝보정[%s] 비중첩 한계로 스킵: raw=%+.2fs max_non_overlap=%+.2fs",
            sec.name,
            raw_shift,
            max_non_overlap_shift,
        )
        return out
    _emit_debug(
        debug_log,
        "오프닝보정[%s] floor=%.2fs first_st=%.2fs shift=%+.2fs(raw=%+.2fs)",
        sec.name,
        floor,
        first_st,
        shift,
        raw_shift,
    )
    dur = max(0.0, float(audio_duration_sec or 0.0))
    for j in range(first_idx, end_idx):
        out[j]["start_sec"] = float(out[j]["start_sec"]) + shift
        out[j]["end_sec"] = float(out[j]["end_sec"]) + shift
        if dur > 0:
            out[j]["start_sec"] = min(float(out[j]["start_sec"]), dur)
            out[j]["end_sec"] = min(float(out[j]["end_sec"]), dur)
    out.sort(key=lambda x: float(x["start_sec"]))
    return out


def _build_from_stt_line_matching(
    sections: list[LyricSection],
    stt_hints: list[dict[str, Any]],
    *,
    refined_lines: list[dict[str, Any]] | None,
    audio_duration_sec: float,
    enable_timing_corrections: bool,
    debug_log: DebugLog = None,
) -> list[dict[str, Any]]:
    """줄 단위 STT 매칭(교정 cue가 없을 때). 매칭 실패 시 순서·보간으로 누락 방지."""
    refined_texts = [
        str(x.get("text", "")).strip() for x in (refined_lines or []) if str(x.get("text", "")).strip()
    ]
    ref_i = 0
    dur = max(0.0, float(audio_duration_sec or 0.0))
    out: list[dict[str, Any]] = []
    cursor = 0.0
    used_stt: set[float] = set()

    for i, sec in enumerate(sections):
        after_chorus = section_follows_chorus(sections, i)
        search_after = cursor if after_chorus else 0.0
        section_floor = stt_section_start_sec(
            sec, stt_hints, after_sec=search_after, prefer_later_match=after_chorus
        )
        line_threshold = max(0.0, search_after - 8.0)
        section_cues: list[dict[str, Any]] = []
        est_dur = 4.5

        for line in sec.lines:
            text = line
            if ref_i < len(refined_texts):
                text = refined_texts[ref_i]
                ref_i += 1
            if not text.strip():
                continue

            match = _best_stt_match_for_line(text, stt_hints, threshold=line_threshold)
            if match is None:
                match = _best_stt_match_for_line(line, stt_hints, threshold=line_threshold)
            if match is None:
                match = _next_stt_hint_after(
                    stt_hints, after_sec=line_threshold, used_starts=used_stt
                )
            if match is None:
                st = max(line_threshold, cursor) + 0.05
                en = st + est_dur
                section_cues.append({"start_sec": st, "end_sec": en, "text": text})
                line_threshold = en
                cursor = en
                continue

            st, en = match
            if st in used_stt:
                st = max(line_threshold, cursor) + 0.05
                en = st + est_dur
            else:
                used_stt.add(st)
            if en <= st:
                en = st + est_dur
            section_cues.append({"start_sec": st, "end_sec": en, "text": text})
            line_threshold = max(line_threshold, en)
            cursor = en

        if (
            enable_timing_corrections
            and after_chorus
            and section_floor is not None
            and section_cues
        ):
            raw_shift = section_floor - float(section_cues[0]["start_sec"])
            shift = _clamp_post_chorus_shift(raw_shift)
            if abs(shift) < 2.0:
                shift = 0.0
        else:
            shift = 0.0

        if after_chorus and section_floor is not None and section_cues:
            _emit_debug(
                debug_log,
                "줄매칭보정[%s] floor=%.2fs first_st=%.2fs shift=%+.2fs",
                sec.name,
                section_floor,
                float(section_cues[0]["start_sec"]),
                shift,
            )

        if shift != 0.0:
            for cue in section_cues:
                cue["start_sec"] = float(cue["start_sec"]) + shift
                cue["end_sec"] = float(cue["end_sec"]) + shift

        for cue in section_cues:
            st = float(cue["start_sec"])
            en = float(cue["end_sec"])
            if dur > 0:
                st = min(st, dur)
                en = min(en, dur)
            if en <= st:
                continue
            out.append({"start_sec": st, "end_sec": en, "text": cue["text"]})
            cursor = en

    return out


def build_subtitles_stt_aligned(
    reference_lyrics: str,
    stt_hints: list[dict[str, Any]],
    *,
    refined_lines: list[dict[str, Any]] | None = None,
    audio_duration_sec: float = 0.0,
    enable_timing_corrections: bool = False,
    debug_log: DebugLog = None,
) -> list[dict[str, Any]]:
    """원곡 가사 + STT로 자막 타임라인 생성.

    1) 가사 교정(refined_lines)이 있으면 그 시각·텍스트를 유지하고 후렴 직후만 보정
    2) 없으면 줄별 STT 매칭(누락 시 순서·보간)
    """
    sections = parse_lyrics_sections(reference_lyrics)
    if not sections or not stt_hints:
        raise GeminiAudioSegmentsError("가사 섹션 또는 STT 결과가 비어 있습니다.")

    dur = max(0.0, float(audio_duration_sec or 0.0))
    timed_refined: list[dict[str, Any]] = []
    for ln in refined_lines or []:
        txt = str(ln.get("text", "")).strip()
        if not txt:
            continue
        try:
            st = float(ln["start_sec"])
            en = float(ln["end_sec"])
        except (TypeError, ValueError, KeyError):
            continue
        if en <= st:
            en = st + 4.0
        timed_refined.append({"start_sec": st, "end_sec": en, "text": txt})

    total_lyric_lines = sum(len(s.lines) for s in sections)
    if timed_refined and len(timed_refined) >= max(3, int(total_lyric_lines * 0.5)):
        _emit_debug(
            debug_log,
            "STT자막 모드=refined (보정=%s, refined=%s, lyric_lines=%s)",
            "ON" if enable_timing_corrections else "OFF",
            len(timed_refined),
            total_lyric_lines,
        )
        if enable_timing_corrections:
            out = _apply_opening_section_timing_fix(
                timed_refined, sections, stt_hints, dur, debug_log=debug_log
            )
            out = _apply_post_chorus_timing_fix(
                out, sections, stt_hints, dur, debug_log=debug_log
            )
        else:
            out = timed_refined
        _emit_debug(
            debug_log,
            "STT 기반 자막(교정 cue 유지%s): %s개 cue (가사 %s줄)",
            " + 보정" if enable_timing_corrections else "",
            len(out),
            total_lyric_lines,
        )
    else:
        out = _build_from_stt_line_matching(
            sections,
            stt_hints,
            refined_lines=refined_lines,
            audio_duration_sec=dur,
            enable_timing_corrections=enable_timing_corrections,
            debug_log=debug_log,
        )
        _emit_debug(debug_log, "STT 기반 자막(줄별 매칭): %s개 cue", len(out))

    if not out:
        raise GeminiAudioSegmentsError("STT 기반 자막 매칭 결과가 비어 있습니다.")
    out.sort(key=lambda x: float(x["start_sec"]))
    if enable_timing_corrections:
        before_dedup = len(out)
        out = deduplicate_timed_lines(out)
        if len(out) != before_dedup:
            _emit_debug(
                debug_log,
                "STT자막 dedup: %s -> %s (제거 %s)",
                before_dedup,
                len(out),
                before_dedup - len(out),
            )
    else:
        _emit_debug(debug_log, "STT자막 dedup: OFF")
    preview_n = min(8, len(out))
    for i in range(preview_n):
        cue = out[i]
        _emit_debug(
            debug_log,
            "STT자막 cue[%02d] %.2f->%.2f %r",
            i,
            float(cue.get("start_sec", 0.0)),
            float(cue.get("end_sec", 0.0)),
            str(cue.get("text", "")).strip(),
        )
    return out


def retime_subtitles_by_lyrics_sections(
    *,
    audio_path: Path,
    mime_type: str,
    reference_lyrics: str,
    stt_hints: list[dict[str, Any]] | None,
    audio_duration_sec: float,
    api_key: str,
    model: str,
    wav_segments: list[dict[str, Any]] | None = None,
    timeout_sec: float = 240.0,
) -> list[dict[str, Any]]:
    """가사 섹션별로 보컬 타이밍을 맞춤(간주 후 재동기화)."""
    sections = parse_lyrics_sections(reference_lyrics)
    if len(sections) <= 1:
        return retime_subtitles_with_vocal_audio(
            audio_path=audio_path,
            mime_type=mime_type,
            reference_lyrics=reference_lyrics,
            stt_hints=stt_hints,
            audio_duration_sec=audio_duration_sec,
            api_key=api_key,
            model=model,
            wav_segments=wav_segments,
            timeout_sec=timeout_sec,
        )

    all_lines: list[dict[str, Any]] = []
    cursor = 0.0
    dur = max(0.0, float(audio_duration_sec or 0.0))

    for i, sec in enumerate(sections):
        min_start = _estimate_min_start_for_section(
            sec,
            index=i,
            sections=sections,
            cursor_sec=cursor,
            stt_hints=stt_hints,
        )
        logger.info(
            "보컬 자막 섹션 [%s] min_start=%.1fs (후렴직후=%s)",
            sec.name,
            min_start,
            section_follows_chorus(sections, i),
        )
        part = _retime_single_section(
            audio_path=audio_path,
            mime_type=mime_type,
            section=sec,
            min_start_sec=min_start,
            audio_duration_sec=dur,
            stt_hints=stt_hints,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec,
        )
        if not part:
            continue
        all_lines.extend(part)
        cursor = float(part[-1]["end_sec"]) + 0.25

    if not all_lines:
        raise GeminiAudioSegmentsError("섹션별 보컬 자막 결과가 비어 있습니다.")
    all_lines = deduplicate_timed_lines(all_lines)
    if dur > 0:
        all_lines[-1]["end_sec"] = min(float(all_lines[-1]["end_sec"]), dur)
    logger.info("섹션별 보컬 자막 타이밍 완료: %s개 cue", len(all_lines))
    return all_lines


def retime_subtitles_with_vocal_audio(
    *,
    audio_path: Path,
    mime_type: str,
    reference_lyrics: str,
    stt_hints: list[dict[str, Any]] | None,
    audio_duration_sec: float,
    api_key: str,
    model: str,
    wav_segments: list[dict[str, Any]] | None = None,
    timeout_sec: float = 240.0,
    use_stt_timing: bool = True,
    refined_lines: list[dict[str, Any]] | None = None,
    enable_timing_corrections: bool = False,
    debug_log: DebugLog = None,
) -> list[dict[str, Any]]:
    """오디오+원곡 가사로 보컬 구간에만 자막 타임라인 생성(인트로·간주 무자막)."""
    ref = (reference_lyrics or "").strip()
    if not ref:
        raise GeminiAudioSegmentsError("원곡 가사가 비어 있습니다.")

    if use_stt_timing and stt_hints:
        try:
            return build_subtitles_stt_aligned(
                ref,
                stt_hints,
                refined_lines=refined_lines,
                audio_duration_sec=audio_duration_sec,
                enable_timing_corrections=enable_timing_corrections,
                debug_log=debug_log,
            )
        except GeminiAudioSegmentsError:
            logger.warning("STT 기반 자막 타이밍 실패 — Gemini 방식으로 재시도")

    sections = parse_lyrics_sections(ref)
    if len(sections) >= 2:
        return retime_subtitles_by_lyrics_sections(
            audio_path=audio_path,
            mime_type=mime_type,
            reference_lyrics=ref,
            stt_hints=stt_hints,
            audio_duration_sec=audio_duration_sec,
            api_key=api_key,
            model=model,
            wav_segments=wav_segments,
            timeout_sec=timeout_sec,
        )

    if len(ref) > _LYRICS_MAX:
        ref = ref[:_LYRICS_MAX] + "\n…(이하 생략)"

    dur = max(0.0, float(audio_duration_sec or 0.0))
    dur_block = ""
    if dur > 0:
        dur_block = (
            f"\n오디오 전체 길이: 약 {dur:.2f}초. "
            f"마지막 자막 end_sec는 {dur:.1f}초 전후여야 합니다."
        )

    hints_block = ""
    if stt_hints:
        compact = [
            {
                "idx": i + 1,
                "start_sec": float(x.get("start_sec", 0)),
                "end_sec": float(x.get("end_sec", 0)),
                "text": str(x.get("text", "")).strip()[:80],
            }
            for i, x in enumerate(stt_hints[:40])
        ]
        hints_block = (
            "\n\nstt_hints는 Whisper 결과(타이밍이 틀릴 수 있음, 특히 인트로·간주)입니다. "
            "참고만 하고 최종 시간은 오디오의 실제 보컬에 맞추세요.\n"
            f"{json.dumps(compact, ensure_ascii=False)}"
        )

    section_lines = ""
    if sections:
        section_lines = "\n".join(
            f"- [{s.name}] {len(s.lines)}줄" for s in sections
        )

    prompt = (
        "음악 자막 타이밍 전문가입니다. 첨부 오디오를 듣고 원곡 가사를 보컬이 나올 때만 자막으로 배치하세요.\n"
        "반드시 JSON 배열만 반환하세요. 필드: start_sec(number), end_sec(number), text(string).\n"
        "규칙:\n"
        "1) text는 reference_lyrics의 가사를 순서대로 나눠 넣으세요(표기는 원곡 가사 기준).\n"
        "2) 인트로·간주·아웃트로 등 **보컬/노래가 없는 구간에는 자막을 만들지 마세요**.\n"
        "3) **[Chorus] 다음 [Verse3] 등은 긴 간주 뒤에 시작**합니다. "
        "간주 중·전주에 다음 절 가사를 미리 넣지 마세요.\n"
        "4) 동일 가사를 두 번 반복하지 마세요.\n"
        "5) start_sec/end_sec는 초(second) 단위 실수입니다.\n"
        "6) 구간은 시간 순서·비중복, 보컬이 있는 구간만.\n"
        "7) 한 cue의 text는 너무 길지 않게(대략 1~2문장).\n"
        f"{dur_block}"
    )
    if section_lines:
        prompt += f"\n\n가사 섹션 구조:\n{section_lines}\n"
    prompt += f"\n\n--- reference_lyrics ---\n{ref}\n--- 끝 ---{hints_block}"

    lines = _gemini_vocal_timing_request(
        audio_path=audio_path,
        mime_type=mime_type,
        prompt=prompt,
        api_key=api_key,
        model=model,
        timeout_sec=timeout_sec,
    )
    lines = deduplicate_timed_lines(lines)
    if dur > 0:
        lines[-1]["end_sec"] = min(float(lines[-1]["end_sec"]), dur)
    logger.info("Gemini 보컬 자막 타이밍 완료: %s개 cue", len(lines))
    return lines
