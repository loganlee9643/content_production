from __future__ import annotations

import re
from pathlib import Path

from app.models.storyboard import Scene
from app.services.ffprobe_audio import FfprobeError, ffprobe_duration_seconds


_SRT_TS = re.compile(
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})"
)


def srt_timestamp_to_seconds(ts: str) -> float:
    m = _SRT_TS.fullmatch((ts or "").strip())
    if not m:
        raise ValueError(f"잘못된 SRT 타임스탬프: {ts!r}")
    return (
        int(m.group("h")) * 3600.0
        + int(m.group("m")) * 60.0
        + int(m.group("s"))
        + int(m.group("ms")) / 1000.0
    )


def parse_srt_file(path: Path) -> list[tuple[float, float, str]]:
    """SRT 파일 → (start_sec, end_sec, text) 목록."""
    if not path.is_file():
        return []
    return parse_srt_content(path.read_text(encoding="utf-8"))


def parse_srt_content(body: str) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    blocks = re.split(r"\n\s*\n", (body or "").strip())
    for block in blocks:
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        arrow_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), -1)
        if arrow_idx < 0:
            continue
        try:
            st_s, en_s = [p.strip() for p in lines[arrow_idx].split("-->", 1)]
            st = srt_timestamp_to_seconds(st_s)
            en = srt_timestamp_to_seconds(en_s)
        except ValueError:
            continue
        if en <= st:
            continue
        text = "\n".join(lines[arrow_idx + 1 :]).strip()
        if not text:
            continue
        cues.append((st, en, text))
    cues.sort(key=lambda x: x[0])
    return cues


def build_srt_from_timed_lines(
    lines: list[dict[str, object]],
    *,
    max_line_chars: int,
) -> str:
    """STT 등 타임라인 문장 목록 → 단일 WAV용 SRT."""
    blocks: list[str] = []
    cue_index = 1
    max_line_chars = max(8, int(max_line_chars))
    for item in lines:
        try:
            st = float(item.get("start_sec", 0.0))
            en = float(item.get("end_sec", 0.0))
        except (TypeError, ValueError):
            continue
        if en <= st:
            continue
        text = " ".join(str(item.get("text", "")).split()).strip()
        if not text:
            continue
        blocks.append(str(cue_index))
        blocks.append(f"{seconds_to_srt_timestamp(st)} --> {seconds_to_srt_timestamp(en)}")
        blocks.append(text)
        blocks.append("")
        cue_index += 1
    if not blocks:
        raise ValueError("자막 구간이 없습니다.")
    return "\n".join(blocks).rstrip() + "\n"


def merge_wav_subtitle_srts(
    *,
    project_parent: Path,
    wav_sources: list[Path],
    subtitle_relpaths: list[str | None],
    max_line_chars: int,
    allow_empty: bool = False,
) -> str:
    """
    WAV 목록 순서대로 각 WAV의 SRT를 이어 붙입니다.
    타임라인 오프셋은 이전 WAV 전체 길이(초)의 합입니다.

    allow_empty=True이면 병합할 자막이 없을 때 빈 문자열을 반환합니다.
    """
    if len(wav_sources) != len(subtitle_relpaths):
        raise ValueError("WAV와 자막 경로 개수가 맞지 않습니다.")
    timeline = 0.0
    blocks: list[str] = []
    cue_index = 1
    max_line_chars = max(8, int(max_line_chars))
    has_any = False

    for wav_path, sub_rel in zip(wav_sources, subtitle_relpaths):
        rel = (sub_rel or "").strip().replace("\\", "/")
        if rel:
            cues = parse_srt_file((project_parent / rel).resolve())
            for st, en, text in cues:
                start = timeline + st
                end = timeline + en
                for line in split_narration_lines(text, max_line_chars) or [text]:
                    blocks.append(str(cue_index))
                    blocks.append(
                        f"{seconds_to_srt_timestamp(start)} --> {seconds_to_srt_timestamp(end)}"
                    )
                    blocks.append(line)
                    blocks.append("")
                    cue_index += 1
                    has_any = True
        try:
            dur = max(0.04, float(ffprobe_duration_seconds(wav_path.resolve())))
        except FfprobeError:
            dur = 0.0
        if dur > 0:
            timeline += dur

    if not has_any:
        if allow_empty:
            return ""
        raise ValueError(
            "병합할 자막이 없습니다. 각 WAV에 「자막 생성」을 실행했는지, "
            "프로젝트를 저장했는지 확인하세요."
        )
    return "\n".join(blocks).rstrip() + "\n"


def seconds_to_srt_timestamp(sec: float) -> str:
    """SRT 타임스탬프 HH:MM:SS,mmm (항상 양수 구간)."""
    sec = max(0.0, float(sec))
    total_ms = int(round(sec * 1000.0))
    h = total_ms // 3_600_000
    total_ms %= 3_600_000
    m = total_ms // 60_000
    total_ms %= 60_000
    s = total_ms // 1000
    ms = total_ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_narration_lines(text: str, max_chars: int) -> list[str]:
    """나레이션을 자막 줄 단위로 나눕니다(공백·문장 경계를 우선)."""
    t = " ".join((text or "").split())
    if not t:
        return []
    max_chars = max(8, int(max_chars))
    if len(t) <= max_chars:
        return [t]
    target_count = max(1, (len(t) + max_chars - 1) // max_chars)
    if target_count > 1:
        balanced = _split_balanced_by_boundaries(t, target_count=target_count, max_chars=max_chars)
        if balanced:
            return balanced
    lines: list[str] = []
    remaining = t
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        cut = max(
            window.rfind(" "),
            window.rfind(","),
            window.rfind("."),
            window.rfind("?"),
            window.rfind("!"),
            window.rfind("，"),
            window.rfind("。"),
            window.rfind("？"),
            window.rfind("！"),
        )
        if cut < max(8, max_chars // 2):
            cut = max_chars
        chunk = remaining[:cut].strip()
        if chunk:
            lines.append(chunk)
        remaining = remaining[cut:].strip()
    if remaining:
        lines.append(remaining)
    return lines


def _split_balanced_by_boundaries(text: str, *, target_count: int, max_chars: int) -> list[str]:
    remaining = text.strip()
    chunks: list[str] = []
    punctuation = set(",.?!，。？！、…")
    for remaining_chunks in range(target_count, 1, -1):
        ideal = max(8, round(len(remaining) / remaining_chunks))
        slack = max(6, ideal // 3)
        lower = max(8, ideal - slack)
        upper = min(len(remaining) - 1, max_chars, ideal + slack)
        candidates: list[int] = []
        for i, ch in enumerate(remaining[: upper + 1]):
            if i < lower:
                continue
            if ch.isspace():
                candidates.append(i)
            elif ch in punctuation:
                candidates.append(i + 1)
        if not candidates:
            candidates = [min(max_chars, ideal)]
        cut = min(candidates, key=lambda pos: (abs(pos - ideal), pos))
        chunk = remaining[:cut].strip()
        if not chunk:
            return []
        chunks.append(chunk)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    if len(chunks) != target_count:
        return []
    if any(len(chunk) > max_chars for chunk in chunks):
        return []
    return chunks


def build_merged_srt(
    scenes: list[Scene],
    project_parent: Path,
    *,
    max_line_chars: int,
) -> str:
    """씬 순서대로 WAV 길이만큼 타임라인을 쌓아 단일 SRT 문자열 생성."""
    timeline = 0.0
    cue_index = 1
    blocks: list[str] = []

    for scene in scenes:
        if not scene.audio_relpath.strip():
            continue
        wav = (project_parent / scene.audio_relpath).resolve()
        if not wav.is_file():
            continue
        dur = 0.0
        note = (scene.notes or "").strip()
        if note.startswith("duration_sec:"):
            raw = note[len("duration_sec:") :].strip()
            try:
                dur = float(raw)
            except ValueError:
                dur = 0.0
        if dur <= 0:
            try:
                dur = ffprobe_duration_seconds(wav)
            except FfprobeError as e:
                raise ValueError(f"WAV 길이 확인 실패: {wav} ({e})") from e
        dur = max(0.04, dur)
        narration = (scene.narration_ko or "").strip()
        if not narration:
            narration = " "
        lines = split_narration_lines(narration, max_line_chars)
        if not lines:
            lines = [" "]
        step = dur / len(lines)
        for line in lines:
            start = timeline
            end = timeline + step
            blocks.append(str(cue_index))
            blocks.append(f"{seconds_to_srt_timestamp(start)} --> {seconds_to_srt_timestamp(end)}")
            blocks.append(line)
            blocks.append("")
            cue_index += 1
            timeline = end

    if not blocks:
        raise ValueError(
            "자막 구간이 없습니다. 씬에 audio_relpath가 있고 WAV 파일이 프로젝트 폴더 기준으로 존재하는지 확인하세요."
        )
    return "\n".join(blocks).rstrip() + "\n"
