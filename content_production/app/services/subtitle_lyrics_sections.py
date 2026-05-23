from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 가사 헤더 [Chorus] 등 — 곡마다 다른 본문 가사(10코스 등)는 매칭하지 않음
_CHORUS_SECTION_RE = re.compile(
    r"(chorus|refrain|hook|후렴|코러스)",
    re.IGNORECASE,
)

_SECTION_HEAD_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$", re.MULTILINE)


@dataclass(frozen=True)
class LyricSection:
    name: str
    lines: tuple[str, ...]


def parse_lyrics_sections(reference_lyrics: str) -> list[LyricSection]:
    """원곡 가사를 [Verse1] 등 섹션으로 분리."""
    ref = (reference_lyrics or "").strip()
    if not ref:
        return []
    sections: list[LyricSection] = []
    current_name = "본문"
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines, current_name
        lines = tuple(x for x in (ln.strip() for ln in current_lines) if x)
        if lines:
            sections.append(LyricSection(name=current_name, lines=lines))
        current_lines = []

    for raw in ref.splitlines():
        line = raw.strip()
        m = _SECTION_HEAD_RE.match(line)
        if m:
            flush()
            current_name = m.group(1).strip() or "본문"
            continue
        if line:
            current_lines.append(line)
    flush()
    return sections


def is_chorus_section_name(name: str) -> bool:
    """섹션 제목이 후렴/코러스류인지(구조 판별용, 가사 본문과 무관)."""
    return bool(_CHORUS_SECTION_RE.search(name or ""))


def section_follows_chorus(sections: list[LyricSection], index: int) -> bool:
    """바로 앞 섹션이 후렴이면 간주 직후 절로 간주."""
    if index <= 0 or index >= len(sections):
        return False
    return is_chorus_section_name(sections[index - 1].name)


def _norm_text(t: str) -> str:
    return re.sub(r"[\s\W_]+", "", (t or "").strip().lower())


def _line_match_score(lyric_line: str, stt_text: str) -> float:
    a = _norm_text(lyric_line)
    b = _norm_text(stt_text)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    key = a[: min(10, len(a))]
    if len(key) >= 4 and key in b:
        return 0.85
    return 0.0


def _best_stt_match_for_line(
    lyric_line: str,
    stt_hints: list[dict[str, Any]],
    *,
    threshold: float,
    min_start_sec: float | None = None,
) -> tuple[float, float] | None:
    """가사 줄과 가장 잘 맞는 STT 구간 (start_sec, end_sec)."""
    best: tuple[float, float] | None = None
    best_score = 0.0
    floor = max(0.0, float(min_start_sec or 0.0)) - 0.5 if min_start_sec is not None else None
    for h in stt_hints:
        try:
            st = float(h.get("start_sec", 0.0))
            en = float(h.get("end_sec", 0.0))
        except (TypeError, ValueError):
            continue
        if st < threshold:
            continue
        if floor is not None and st < floor:
            continue
        score = _line_match_score(lyric_line, str(h.get("text", "")))
        if score > best_score:
            best_score = score
            best = (st, en)
    if best_score >= 0.75 and best is not None:
        return best
    return None


def _next_stt_hint_after(
    stt_hints: list[dict[str, Any]],
    *,
    after_sec: float,
    used_starts: set[float],
) -> tuple[float, float] | None:
    """매칭 실패 시 순서대로 다음 STT 구간 할당."""
    for h in stt_hints:
        try:
            st = float(h["start_sec"])
            en = float(h["end_sec"])
        except (TypeError, ValueError, KeyError):
            continue
        if st < after_sec - 0.5:
            continue
        if st in used_starts:
            continue
        if en <= st:
            en = st + 4.0
        used_starts.add(st)
        return st, en
    return None


def _best_stt_start_for_line(
    lyric_line: str,
    stt_hints: list[dict[str, Any]],
    *,
    threshold: float,
    min_start_sec: float | None = None,
) -> float | None:
    m = _best_stt_match_for_line(
        lyric_line, stt_hints, threshold=threshold, min_start_sec=min_start_sec
    )
    return m[0] if m is not None else None


def stt_section_start_sec(
    section: LyricSection,
    stt_hints: list[dict[str, Any]] | None,
    *,
    after_sec: float = 0.0,
    prefer_later_match: bool = False,
) -> float | None:
    """섹션 가사 각 줄에 대응하는 STT 시작 시각 추정.

    후렴 직후 절은 간주 중 첫 줄만 조기 매칭되는 경우가 많아,
    여러 줄 매칭 시각이 넓게 퍼지면 가장 늦은 매칭(실제 보컬 진입)을 씁니다.
    """
    if not stt_hints or not section.lines:
        return None
    threshold = max(0.0, float(after_sec) - 8.0)
    anchors: list[float] = []
    for line in section.lines:
        st = _best_stt_start_for_line(line, stt_hints, threshold=threshold)
        if st is not None:
            anchors.append(st)
    if not anchors:
        return None
    if not prefer_later_match or len(anchors) == 1:
        return anchors[0]
    spread = max(anchors) - min(anchors)
    # 간주·멜로디에 첫 줄만 일찍 잡히고 실제 2~3줄째부터 맞는 경우:
    # max(전체)는 다음 줄 STT(85초 등)까지 끌어올려 4초 이상 늦어질 수 있음 →
    # 후렴·간주 이후(min_after)에 잡힌 매칭 중 가장 이른 시각을 절 시작으로 씀.
    if spread >= 6.0:
        min_after = max(threshold, float(after_sec) + 8.0)
        late = sorted(a for a in anchors if a >= min_after)
        if late:
            return late[0]
        return max(anchors)
    return anchors[0]


def stt_anchor_for_section(
    section: LyricSection,
    stt_hints: list[dict[str, Any]] | None,
    *,
    after_sec: float = 0.0,
) -> float | None:
    """첫 가사 줄만 기준으로 한 STT 시작 시각(하위 호환)."""
    if not section.lines:
        return None
    threshold = max(0.0, float(after_sec) - 8.0)
    return _best_stt_start_for_line(section.lines[0], stt_hints or [], threshold=threshold)


def stt_vocal_gaps(
    stt_hints: list[dict[str, Any]] | None,
    *,
    min_gap_sec: float = 3.5,
) -> list[tuple[float, float]]:
    """STT 구간 사이 긴 무성(간주) 후보 구간 (시작, 끝) 초."""
    if not stt_hints:
        return []
    rows: list[tuple[float, float]] = []
    for h in stt_hints:
        try:
            rows.append((float(h["start_sec"]), float(h["end_sec"])))
        except (TypeError, ValueError, KeyError):
            continue
    rows.sort(key=lambda x: x[0])
    gaps: list[tuple[float, float]] = []
    for i in range(len(rows) - 1):
        gap_start = rows[i][1]
        gap_end = rows[i + 1][0]
        if gap_end - gap_start >= min_gap_sec:
            gaps.append((gap_start, gap_end))
    return gaps


def deduplicate_timed_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """동일 가사 블록이 두 번 들어간 경우 앞쪽(간주 오인·조기 배치) 블록 제거.

    단일 문장(1줄) 중복은 의도된 반복일 수 있어 제거하지 않는다.
    """
    if len(lines) < 2:
        return lines

    def norm(t: str) -> str:
        return re.sub(r"\s+", "", (t or "").strip().lower())

    texts = [norm(str(x.get("text", ""))) for x in lines]
    n = len(lines)
    best_remove: set[int] = set()

    # 1줄 중복까지 제거하면 정상 반복 가사가 사라질 수 있어 2줄 이상만 대상으로 한다.
    for block_len in range(2, min(8, n // 2 + 1)):
        for i in range(n - block_len):
            block = texts[i : i + block_len]
            if not all(block):
                continue
            for j in range(i + block_len, n - block_len + 1):
                if texts[j : j + block_len] == block:
                    if float(lines[i]["start_sec"]) < float(lines[j]["start_sec"]):
                        best_remove.update(range(i, i + block_len))
                    else:
                        best_remove.update(range(j, j + block_len))
                    break

    if not best_remove:
        return lines
    return [ln for idx, ln in enumerate(lines) if idx not in best_remove]


def filter_stt_hints_before_sec(
    stt_hints: list[dict[str, Any]] | None,
    *,
    min_start_sec: float,
) -> list[dict[str, Any]] | None:
    """특정 시각 이전 STT 힌트 제거."""
    if not stt_hints or min_start_sec <= 0:
        return stt_hints
    out = [
        h
        for h in stt_hints
        if float(h.get("start_sec", 0.0)) >= min_start_sec - 0.5
    ]
    return out if out else None
