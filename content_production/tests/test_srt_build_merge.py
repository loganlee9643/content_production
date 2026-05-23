"""WAV 목록 SRT 병합 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.srt_build import merge_wav_subtitle_srts


def test_merge_wav_subtitle_srts_allow_empty(tmp_path: Path) -> None:
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"\x00" * 64)
    with pytest.raises(ValueError, match="병합할 자막이 없습니다"):
        merge_wav_subtitle_srts(
            project_parent=tmp_path,
            wav_sources=[wav],
            subtitle_relpaths=[None],
            max_line_chars=40,
            allow_empty=False,
        )
    body = merge_wav_subtitle_srts(
        project_parent=tmp_path,
        wav_sources=[wav],
        subtitle_relpaths=[None],
        max_line_chars=40,
        allow_empty=True,
    )
    assert body == ""
