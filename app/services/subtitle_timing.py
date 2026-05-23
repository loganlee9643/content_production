from __future__ import annotations

from typing import Any


def apply_subtitle_timing_adjustments(
    lines: list[dict[str, Any]],
    *,
    intro_skip_sec: float = 0.0,
    offset_sec: float = 0.0,
    audio_duration_sec: float = 0.0,
    min_cue_sec: float = 0.04,
) -> list[dict[str, Any]]:
    """인트로 무자막·전체 지연 등을 SRT 구간에 반영."""
    if not lines:
        return []
    intro = max(0.0, float(intro_skip_sec or 0.0))
    offset = float(offset_sec or 0.0)
    dur = max(0.0, float(audio_duration_sec or 0.0))
    out: list[dict[str, Any]] = []
    for item in lines:
        try:
            st = float(item.get("start_sec", 0.0)) + offset
            en = float(item.get("end_sec", 0.0)) + offset
        except (TypeError, ValueError):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        if en <= intro:
            continue
        if st < intro:
            st = intro
        if dur > 0:
            st = max(0.0, min(st, dur))
            en = max(0.0, min(en, dur))
        if en - st < min_cue_sec:
            continue
        row = dict(item)
        row["start_sec"] = st
        row["end_sec"] = en
        row["text"] = text
        out.append(row)
    out.sort(key=lambda x: float(x["start_sec"]))
    return out


def prepend_intro_title_cue(
    lines: list[dict[str, Any]],
    *,
    title: str,
    duration_sec: float = 0.0,
    intro_skip_sec: float = 0.0,
    audio_duration_sec: float = 0.0,
    lyric_gap_sec: float = 0.12,
) -> list[dict[str, Any]]:
    """인트로 구간에 곡 제목 cue를 넣고, 가사는 제목·무자막 구간 이후에만 시작."""
    text = (title or "").strip()
    if not text:
        return lines
    skip = max(0.0, float(intro_skip_sec or 0.0))
    dur = float(duration_sec or 0.0)
    if dur <= 0:
        dur = skip if skip > 0 else 5.0
    dur = max(0.5, dur)
    title_st = 0.0
    title_en = title_st + dur
    if audio_duration_sec > 0:
        title_en = min(title_en, float(audio_duration_sec))
    if title_en <= title_st:
        return lines

    lyric_start = max(skip, title_en + max(0.0, float(lyric_gap_sec)))
    if audio_duration_sec > 0:
        lyric_start = min(lyric_start, float(audio_duration_sec))

    adjusted: list[dict[str, Any]] = []
    for item in lines:
        try:
            st = float(item.get("start_sec", 0.0))
            en = float(item.get("end_sec", 0.0))
        except (TypeError, ValueError):
            continue
        text_line = str(item.get("text", "")).strip()
        if not text_line:
            continue
        if st < lyric_start:
            # 긴 첫 cue(예: 0~15s)는 시작만 당겨도 충분하며,
            # 전체를 미루면 뒤 절(10코스 등)까지 같이 밀린다.
            if en > lyric_start:
                st = lyric_start
            else:
                delta = lyric_start - st
                st += delta
                en += delta
        if audio_duration_sec > 0:
            st = min(st, float(audio_duration_sec))
            en = min(en, float(audio_duration_sec))
        if en <= st:
            continue
        row = dict(item)
        row["start_sec"] = st
        row["end_sec"] = en
        row["text"] = text_line
        adjusted.append(row)

    cue: dict[str, Any] = {"start_sec": title_st, "end_sec": title_en, "text": text}
    return [cue, *adjusted]


def drop_overlapping_cues(lines: list[dict[str, Any]], *, min_gap_sec: float = 0.02) -> list[dict[str, Any]]:
    if not lines:
        return []
    out: list[dict[str, Any]] = []
    for item in sorted(lines, key=lambda x: float(x["start_sec"])):
        st = float(item["start_sec"])
        en = float(item["end_sec"])
        if out and st < float(out[-1]["end_sec"]) - min_gap_sec:
            st = float(out[-1]["end_sec"])
        if en <= st:
            continue
        row = dict(item)
        row["start_sec"] = st
        row["end_sec"] = en
        out.append(row)
    return out
