"""자막 섹션·STT 타이밍 테스트 (곡·초 단위 하드코딩 없음)."""

from __future__ import annotations

from typing import Any

from app.services.subtitle_lyrics_sections import (
    LyricSection,
    deduplicate_timed_lines,
    is_chorus_section_name,
    parse_lyrics_sections,
    section_follows_chorus,
    stt_anchor_for_section,
    stt_section_start_sec,
    stt_vocal_gaps,
)


def _section(name: str, *lines: str) -> LyricSection:
    body = "\n".join([f"[{name}]", *lines])
    secs = parse_lyrics_sections(body)
    assert len(secs) == 1
    return secs[0]


def _stt_segments(
  pairs: list[tuple[float, float, str]],
) -> list[dict[str, Any]]:
    return [
        {"start_sec": st, "end_sec": en, "text": text}
        for st, en, text in pairs
    ]


def _chorus_then_verse_ref(chorus_line: str, verse_lines: list[str]) -> str:
    return (
        "[Chorus]\n"
        + chorus_line
        + "\n[Verse]\n"
        + "\n".join(verse_lines)
    )


def test_parse_lyrics_sections():
    ref = "[Verse1]\nline1\n\n[Chorus]\nc1\n\n[Verse3]\nv3"
    secs = parse_lyrics_sections(ref)
    assert [s.name for s in secs] == ["Verse1", "Chorus", "Verse3"]
    assert secs[2].lines[0] == "v3"


def test_section_follows_chorus():
    secs = parse_lyrics_sections("[Verse1]\na\n[Chorus]\nb\n[Verse3]\nc")
    assert not section_follows_chorus(secs, 0)
    assert not section_follows_chorus(secs, 1)
    assert section_follows_chorus(secs, 2)
    assert is_chorus_section_name("Chorus")
    assert is_chorus_section_name("후렴")


def test_stt_anchor_for_section():
    line = "첫 번째 절 가사"
    sec = _section("Verse", line)
    chorus_end = 70.0
    match_start = chorus_end + 2.0
    stt = _stt_segments(
        [
            (chorus_end - 5.0, chorus_end, "후렴 구간"),
            (match_start, match_start + 4.0, line),
        ]
    )
    assert stt_anchor_for_section(sec, stt, after_sec=chorus_end) == match_start


def test_stt_section_start_prefers_later_after_chorus():
    """여러 줄 STT가 넓게 퍼질 때, 간주 직후 잘못된 이른 매칭보다 늦은 보컬 진입을 고른다."""
    lines = ("절 첫줄", "절 둘째줄", "절 셋째줄")
    sec = _section("Verse", *lines)
    after = 70.0
    anchors = (after + 2.0, after + 6.0, after + 11.0)
    stt = _stt_segments(
        [
            (anchors[0], anchors[0] + 4.0, lines[0]),
            (anchors[1], anchors[1] + 5.0, lines[1]),
            (anchors[2], anchors[2] + 4.0, lines[2]),
        ]
    )
    got = stt_section_start_sec(sec, stt, after_sec=after, prefer_later_match=True)
    assert got is not None
    assert got >= after + 8.0
    assert got in anchors
    assert got == min(a for a in anchors if a >= after + 8.0)


def test_stt_section_start_ignores_spurious_late_anchor():
    """가사에 없는 STT 구간이 섞여도 절 시작 시각이 바뀌지 않는다."""
    lines = ("절 첫줄", "절 둘째줄", "절 셋째줄")
    sec = _section("Verse", *lines)
    after = 70.0
    anchors = (after + 2.0, after + 6.0, after + 11.0)
    base_stt = _stt_segments(
        [
            (anchors[0], anchors[0] + 4.0, lines[0]),
            (anchors[1], anchors[1] + 5.0, lines[1]),
            (anchors[2], anchors[2] + 4.0, lines[2]),
        ]
    )
    expected = stt_section_start_sec(
        sec, base_stt, after_sec=after, prefer_later_match=True
    )
    noisy = base_stt + [
        {
            "start_sec": anchors[2] + 4.0,
            "end_sec": anchors[2] + 8.0,
            "text": "가사에 없는 오인식 구간",
        }
    ]
    got = stt_section_start_sec(sec, noisy, after_sec=after, prefer_later_match=True)
    assert got == expected


def test_prepend_intro_title_pushes_lyrics():
    from app.services.subtitle_timing import drop_overlapping_cues, prepend_intro_title_cue

    title = "제목 자막"
    title_dur = 5.0
    gap = 0.12
    lines = [
        {"start_sec": 0.0, "end_sec": 4.0, "text": "첫 가사"},
        {"start_sec": 5.0, "end_sec": 9.0, "text": "둘째 가사"},
    ]
    out = prepend_intro_title_cue(
        lines, title=title, duration_sec=title_dur, intro_skip_sec=0.0, lyric_gap_sec=gap
    )
    assert out[0]["text"] == title
    assert float(out[0]["end_sec"]) == title_dur
    lyric_start = title_dur + gap
    for cue in out[1:]:
        assert float(cue["start_sec"]) >= lyric_start

    # 첫 cue가 길어도 다음 cue들이 사라지지 않아야 한다.
    long_first = [
        {"start_sec": 0.0, "end_sec": 15.0, "text": "첫 가사"},
        {"start_sec": 15.0, "end_sec": 20.0, "text": "둘째 가사"},
        {"start_sec": 20.0, "end_sec": 24.0, "text": "셋째 가사"},
        {"start_sec": 24.0, "end_sec": 30.0, "text": "넷째 가사"},
    ]
    out2 = prepend_intro_title_cue(
        long_first, title=title, duration_sec=title_dur, intro_skip_sec=0.0, lyric_gap_sec=gap
    )
    out2 = drop_overlapping_cues(out2)
    lyric_texts = [str(c.get("text", "")) for c in out2[1:]]
    assert lyric_texts == ["첫 가사", "둘째 가사", "셋째 가사", "넷째 가사"]


def test_stt_vocal_gaps():
    gap_len = 5.0
    first_end = 10.0
    second_start = first_end + gap_len
    stt = _stt_segments(
        [
            (0.0, first_end, "a"),
            (second_start, second_start + 5.0, "b"),
        ]
    )
    gaps = stt_vocal_gaps(stt, min_gap_sec=gap_len)
    assert gaps == [(first_end, second_start)]


def test_build_subtitles_stt_aligned_keeps_refined_cues():
    from app.services.subtitle_vocal_align import build_subtitles_stt_aligned

    ref = "[Verse1]\n가사1\n가사2\n[Chorus]\n후렴1"
    stt = _stt_segments(
        [
            (0.0, 4.0, "x1"),
            (5.0, 9.0, "x2"),
            (10.0, 14.0, "x3"),
        ]
    )
    refined = [
        {"start_sec": st, "end_sec": en, "text": text}
        for st, en, text in [
            (0.0, 4.0, "가사1"),
            (5.0, 9.0, "가사2"),
            (10.0, 14.0, "후렴1"),
        ]
    ]
    lines = build_subtitles_stt_aligned(ref, stt, refined_lines=refined, audio_duration_sec=60.0)
    assert len(lines) == len(refined)
    for got, exp in zip(lines, refined):
        assert float(got["start_sec"]) == exp["start_sec"]
        assert got["text"] == exp["text"]


def test_build_subtitles_stt_aligned_post_chorus_shift():
    from app.services.subtitle_vocal_align import (
        _POST_CHORUS_VOCAL_GAP_SEC,
        build_subtitles_stt_aligned,
    )

    chorus_line = "후렴 가사"
    verse_lines = ("절 A", "절 B", "절 C")
    ref = _chorus_then_verse_ref(chorus_line, list(verse_lines))
    chorus_end = 71.0
    early_verse = chorus_end + 1.0
    stt = _stt_segments(
        [
            (chorus_end - 6.0, chorus_end, chorus_line),
            (early_verse, early_verse + 4.0, verse_lines[0]),
            (early_verse + 4.0, early_verse + 9.0, verse_lines[1]),
            (early_verse + 9.0, early_verse + 13.0, verse_lines[2]),
        ]
    )
    refined = [dict(s) for s in stt]
    secs = parse_lyrics_sections(ref)
    verse_sec = secs[-1]
    after = chorus_end
    floor = stt_section_start_sec(
        verse_sec, stt, after_sec=after, prefer_later_match=True
    )
    assert floor is not None

    lines = build_subtitles_stt_aligned(
        ref, stt, refined_lines=refined, audio_duration_sec=200.0, enable_timing_corrections=True
    )
    first_verse = next(ln for ln in lines if ln["text"] == verse_lines[0])
    got = float(first_verse["start_sec"])

    if abs(floor - early_verse) >= 2.0:
        capped_shift = min(
            abs(floor - early_verse), float(_POST_CHORUS_VOCAL_GAP_SEC)
        )
        expected = early_verse + (capped_shift if floor >= early_verse else -capped_shift)
        assert got == expected
    else:
        assert got == early_verse


def test_build_subtitles_stt_aligned_post_chorus_default_no_correction():
    from app.services.subtitle_vocal_align import build_subtitles_stt_aligned

    chorus_line = "후렴 가사"
    verse_lines = ("절 A", "절 B", "절 C")
    ref = _chorus_then_verse_ref(chorus_line, list(verse_lines))
    stt = _stt_segments(
        [
            (65.0, 71.0, chorus_line),
            (72.0, 76.0, verse_lines[0]),
            (76.0, 81.0, verse_lines[1]),
            (81.0, 85.0, verse_lines[2]),
        ]
    )
    refined = _stt_segments(
        [
            (65.0, 71.0, chorus_line),
            (72.0, 76.0, verse_lines[0]),
            (76.0, 81.0, verse_lines[1]),
            (81.0, 85.0, verse_lines[2]),
        ]
    )
    lines = build_subtitles_stt_aligned(ref, stt, refined_lines=refined, audio_duration_sec=200.0)
    first_verse = next(ln for ln in lines if ln["text"] == verse_lines[0])
    assert float(first_verse["start_sec"]) == 72.0


def test_build_subtitles_stt_aligned_post_chorus_shift_backward():
    from app.services.subtitle_vocal_align import (
        _POST_CHORUS_VOCAL_GAP_SEC,
        build_subtitles_stt_aligned,
    )

    chorus_line = "후렴 가사"
    verse_lines = ("절 A", "절 B", "절 C")
    ref = _chorus_then_verse_ref(chorus_line, list(verse_lines))
    chorus_end = 71.0
    stt_starts = (chorus_end + 1.0, chorus_end + 5.0, chorus_end + 10.0)
    stt = _stt_segments(
        [
            (chorus_end - 6.0, chorus_end, chorus_line),
            (stt_starts[0], stt_starts[0] + 4.0, verse_lines[0]),
            (stt_starts[1], stt_starts[1] + 5.0, verse_lines[1]),
            (stt_starts[2], stt_starts[2] + 4.0, verse_lines[2]),
        ]
    )
    late_delta = 4.0
    refined = [
        dict(seg) if seg["text"] == chorus_line else {
            "start_sec": float(seg["start_sec"]) + late_delta,
            "end_sec": float(seg["end_sec"]) + late_delta,
            "text": seg["text"],
        }
        for seg in stt
    ]

    secs = parse_lyrics_sections(ref)
    verse_sec = secs[-1]
    floor = stt_section_start_sec(
        verse_sec, stt, after_sec=chorus_end, prefer_later_match=True
    )
    assert floor is not None

    lines = build_subtitles_stt_aligned(
        ref, stt, refined_lines=refined, audio_duration_sec=200.0, enable_timing_corrections=True
    )
    first_verse = next(ln for ln in lines if ln["text"] == verse_lines[0])
    got = float(first_verse["start_sec"])
    raw_shift = floor - (stt_starts[0] + late_delta)
    capped_shift = min(abs(raw_shift), float(_POST_CHORUS_VOCAL_GAP_SEC))
    expected = stt_starts[0] + late_delta
    if raw_shift > 0 and capped_shift <= 6.5:
        expected = stt_starts[0] + late_delta
    else:
        expected = (stt_starts[0] + late_delta) + (
            capped_shift if raw_shift >= 0 else -capped_shift
        )
    assert got == expected


def test_build_subtitles_stt_aligned_post_chorus_shift_is_capped():
    from app.services.subtitle_vocal_align import (
        _POST_CHORUS_VOCAL_GAP_SEC,
        build_subtitles_stt_aligned,
    )

    chorus_line = "후렴 가사"
    verse_lines = ("절 A", "절 B", "절 C")
    ref = _chorus_then_verse_ref(chorus_line, list(verse_lines))
    chorus_end = 71.0
    stt = _stt_segments(
        [
            (chorus_end - 6.0, chorus_end, chorus_line),
            # floor를 큰 값으로 만들도록 후렴 뒤 절 STT를 늦게 배치
            (85.0, 89.0, verse_lines[0]),
            (90.0, 94.0, verse_lines[1]),
            (95.0, 99.0, verse_lines[2]),
        ]
    )
    refined = _stt_segments(
        [
            (chorus_end - 6.0, chorus_end, chorus_line),
            # refined cue는 이른 시각에 있어 큰 이동(raw shift)이 필요해지는 상황
            (72.0, 76.0, verse_lines[0]),
            (76.0, 81.0, verse_lines[1]),
            (81.0, 85.0, verse_lines[2]),
        ]
    )

    lines = build_subtitles_stt_aligned(
        ref, stt, refined_lines=refined, audio_duration_sec=200.0, enable_timing_corrections=True
    )
    first_verse = next(ln for ln in lines if ln["text"] == verse_lines[0])
    got = float(first_verse["start_sec"])
    assert got == 72.0 + float(_POST_CHORUS_VOCAL_GAP_SEC)


def test_build_subtitles_stt_aligned_post_chorus_skips_moderate_forward_shift():
    from app.services.subtitle_vocal_align import build_subtitles_stt_aligned

    chorus_line = "후렴 가사"
    verse_lines = ("절 A",)
    ref = _chorus_then_verse_ref(chorus_line, list(verse_lines))
    chorus_end = 108.0
    stt = _stt_segments(
        [
            (100.0, chorus_end, chorus_line),
            (108.0, 114.0, "절 A 오인식"),
            (114.0, 120.0, verse_lines[0]),
        ]
    )
    refined = _stt_segments(
        [
            (100.0, chorus_end, chorus_line),
            (108.0, 114.0, verse_lines[0]),
        ]
    )
    lines = build_subtitles_stt_aligned(
        ref, stt, refined_lines=refined, audio_duration_sec=240.0, enable_timing_corrections=True
    )
    first_verse = next(ln for ln in lines if ln["text"] == verse_lines[0])
    assert float(first_verse["start_sec"]) == 108.0


def test_build_subtitles_stt_aligned_opening_shift_from_zero():
    from app.services.subtitle_vocal_align import build_subtitles_stt_aligned

    ref = "[Verse1]\n첫줄\n둘째줄\n셋째줄"
    stt = _stt_segments(
        [
            (11.0, 15.0, "첫줄"),
            (15.0, 20.0, "둘째줄"),
            (20.0, 24.0, "셋째줄"),
        ]
    )
    refined = _stt_segments(
        [
            (0.0, 15.0, "첫줄"),
            (15.0, 20.0, "둘째줄"),
            (20.0, 24.0, "셋째줄"),
        ]
    )
    lines = build_subtitles_stt_aligned(
        ref, stt, refined_lines=refined, audio_duration_sec=200.0, enable_timing_corrections=True
    )
    first = next(ln for ln in lines if ln["text"] == "첫줄")
    assert float(first["start_sec"]) == 8.0


def test_build_subtitles_stt_aligned_opening_ignores_zero_false_anchor():
    from app.services.subtitle_vocal_align import build_subtitles_stt_aligned

    ref = "[Verse1]\n첫줄\n둘째줄\n셋째줄"
    stt = _stt_segments(
        [
            (0.0, 2.0, "첫줄"),  # 오탐
            (11.0, 15.0, "둘째줄"),
            (15.0, 20.0, "셋째줄"),
        ]
    )
    refined = _stt_segments(
        [
            (0.0, 15.0, "첫줄"),
            (15.0, 20.0, "둘째줄"),
            (20.0, 24.0, "셋째줄"),
        ]
    )
    lines = build_subtitles_stt_aligned(
        ref, stt, refined_lines=refined, audio_duration_sec=200.0, enable_timing_corrections=True
    )
    first = next(ln for ln in lines if ln["text"] == "첫줄")
    assert float(first["start_sec"]) >= 8.0


def test_build_subtitles_stt_aligned_opening_uses_first_hint_when_floor_too_late():
    from app.services.subtitle_vocal_align import build_subtitles_stt_aligned

    ref = "[Verse1]\n첫줄\n둘째줄\n셋째줄"
    stt = _stt_segments(
        [
            (10.0, 14.0, "인트로 끝 실제 첫 보컬"),
            (20.0, 24.0, "둘째줄"),
            (24.0, 28.0, "셋째줄"),
        ]
    )
    refined = _stt_segments(
        [
            (0.0, 15.0, "첫줄"),
            (15.0, 20.0, "둘째줄"),
            (20.0, 24.0, "셋째줄"),
        ]
    )
    lines = build_subtitles_stt_aligned(
        ref, stt, refined_lines=refined, audio_duration_sec=200.0, enable_timing_corrections=True
    )
    first = next(ln for ln in lines if ln["text"] == "첫줄")
    assert float(first["start_sec"]) == 8.0


def test_build_subtitles_stt_aligned_opening_shift_avoids_next_section_overlap():
    from app.services.subtitle_vocal_align import build_subtitles_stt_aligned

    ref = "[Verse1]\n첫줄\n둘째줄\n셋째줄\n넷째줄\n[Verse2]\n다음절"
    stt = _stt_segments(
        [
            (15.0, 20.0, "첫줄"),
            (20.0, 24.0, "둘째줄"),
            (24.0, 30.0, "셋째줄"),
            (30.0, 35.0, "다음절"),
        ]
    )
    refined = _stt_segments(
        [
            (0.0, 15.0, "첫줄"),
            (15.0, 20.0, "둘째줄"),
            (20.0, 24.0, "셋째줄"),
            (24.0, 30.0, "넷째줄"),
            (30.0, 35.0, "다음절"),
        ]
    )
    lines = build_subtitles_stt_aligned(
        ref, stt, refined_lines=refined, audio_duration_sec=200.0, enable_timing_corrections=True
    )
    first = next(ln for ln in lines if ln["text"] == "첫줄")
    next_sec = next(ln for ln in lines if ln["text"] == "다음절")
    assert float(first["start_sec"]) == 0.0
    assert float(next_sec["start_sec"]) == 30.0


def test_deduplicate_timed_lines():
    text = "반복 가능한 단일 가사"
    early_start = 71.0
    late_start = 125.0
    lines = [
        {"start_sec": early_start, "end_sec": early_start + 4.0, "text": text},
        {"start_sec": late_start, "end_sec": late_start + 4.0, "text": text},
    ]
    out = deduplicate_timed_lines(lines)
    assert len(out) == 2
    assert out[0]["start_sec"] == early_start
    assert out[1]["start_sec"] == late_start


def test_deduplicate_timed_lines_repeated_block():
    lines = [
        {"start_sec": 71.0, "end_sec": 75.0, "text": "10코스 화순"},
        {"start_sec": 75.0, "end_sec": 79.0, "text": "산방산 웅장함"},
        {"start_sec": 125.0, "end_sec": 129.0, "text": "10코스 화순"},
        {"start_sec": 129.0, "end_sec": 133.0, "text": "산방산 웅장함"},
    ]
    out = deduplicate_timed_lines(lines)
    assert len(out) == 2
    assert out[0]["start_sec"] == 125.0
    assert out[1]["start_sec"] == 129.0
