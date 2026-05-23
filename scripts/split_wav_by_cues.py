from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.services.ffmpeg_render import which_ffmpeg
from app.services.ffprobe_audio import ffprobe_duration_seconds


@dataclass
class CueRow:
    index: int
    start_sec: float
    end_sec: float | None
    narration: str
    transition: str
    image_relpath: str


def _parse_time_to_seconds(raw: str) -> float:
    s = (raw or "").strip()
    if not s:
        raise ValueError("빈 시간 값")
    if ":" not in s:
        try:
            v = float(s)
        except ValueError as e:
            raise ValueError(f"초 단위 숫자 파싱 실패: {raw!r}") from e
        if v < 0:
            raise ValueError(f"음수 시간은 허용되지 않습니다: {raw!r}")
        return v

    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(f"시간 형식 오류: {raw!r} (HH:MM:SS.mmm)")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"시간 형식 오류: {raw!r}") from e
    if any(v < 0 for v in nums):
        raise ValueError(f"음수 시간은 허용되지 않습니다: {raw!r}")

    if len(nums) == 2:
        mm, ss = nums
        return (mm * 60.0) + ss
    hh, mm, ss = nums
    return (hh * 3600.0) + (mm * 60.0) + ss


def _parse_cues(csv_path: Path) -> list[CueRow]:
    if not csv_path.is_file():
        raise ValueError(f"CSV 파일이 없습니다: {csv_path}")

    rows: list[CueRow] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV 헤더가 없습니다.")
        headers = {h.strip() for h in reader.fieldnames if h}
        if "start" not in headers:
            raise ValueError("CSV 헤더에 start 컬럼이 필요합니다.")

        for i, row in enumerate(reader, start=1):
            start_raw = (row.get("start") or "").strip()
            if not start_raw:
                continue
            end_raw = (row.get("end") or "").strip()
            narration = (row.get("narration") or "").strip() or " "
            transition = (row.get("transition") or "").strip() or "fade"
            image_relpath = (row.get("image_relpath") or "").strip()

            start_sec = _parse_time_to_seconds(start_raw)
            end_sec = _parse_time_to_seconds(end_raw) if end_raw else None
            rows.append(
                CueRow(
                    index=i,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    narration=narration,
                    transition=transition,
                    image_relpath=image_relpath,
                )
            )
    if not rows:
        raise ValueError("유효한 구간이 없습니다. start 컬럼 값을 확인하세요.")
    rows.sort(key=lambda r: r.start_sec)
    return rows


def _finalize_cues(rows: list[CueRow], total_sec: float) -> list[CueRow]:
    out: list[CueRow] = []
    for i, row in enumerate(rows):
        start = row.start_sec
        if start >= total_sec:
            raise ValueError(
                f"{row.index}행 start({start:.3f}s)가 원본 길이({total_sec:.3f}s)보다 큽니다."
            )
        if row.end_sec is None:
            if i + 1 < len(rows):
                end = rows[i + 1].start_sec
            else:
                end = total_sec
        else:
            end = row.end_sec
        if end <= start:
            raise ValueError(
                f"{row.index}행 구간 길이가 0 이하입니다. start={start:.3f}, end={end:.3f}"
            )
        if end > total_sec:
            raise ValueError(
                f"{row.index}행 end({end:.3f}s)가 원본 길이({total_sec:.3f}s)를 초과합니다."
            )
        out.append(
            CueRow(
                index=row.index,
                start_sec=start,
                end_sec=end,
                narration=row.narration,
                transition=row.transition,
                image_relpath=row.image_relpath,
            )
        )
    return out


def _split_audio(
    *,
    ffmpeg: str,
    source_audio: Path,
    cue: CueRow,
    out_wav: Path,
) -> None:
    assert cue.end_sec is not None
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{cue.start_sec:.6f}",
        "-to",
        f"{cue.end_sec:.6f}",
        "-i",
        str(source_audio),
        "-vn",
        "-acodec",
        "pcm_s16le",
        str(out_wav),
    ]
    kwargs: dict[str, int] = {}
    if sys.platform == "win32":
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(msg or f"ffmpeg 실패(code={proc.returncode})")


def _write_manifest(
    *,
    cues: list[CueRow],
    outputs: list[Path],
    manifest_csv: Path,
) -> None:
    manifest_csv.parent.mkdir(parents=True, exist_ok=True)
    with manifest_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "wav_source",
                "narration",
                "transition",
                "image_relpath",
                "start_sec",
                "end_sec",
                "duration_sec",
            ]
        )
        for cue, wav_path in zip(cues, outputs):
            assert cue.end_sec is not None
            dur = cue.end_sec - cue.start_sec
            w.writerow(
                [
                    wav_path.resolve().as_posix(),
                    cue.narration,
                    cue.transition,
                    cue.image_relpath,
                    f"{cue.start_sec:.3f}",
                    f"{cue.end_sec:.3f}",
                    f"{dur:.3f}",
                ]
            )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "구간 CSV(start/end/narration/image_relpath/transition)로 오디오를 분할해 "
            "WAV 목록용 파일과 manifest CSV를 생성합니다."
        )
    )
    p.add_argument("source_audio", type=Path, help="원본 오디오 파일 경로 (wav/mp3 등)")
    p.add_argument("cues_csv", type=Path, help="구간 CSV 파일 경로")
    p.add_argument("--out-dir", type=Path, required=True, help="분할 WAV 저장 폴더")
    p.add_argument("--prefix", default="wavseq_", help="출력 파일 prefix (기본: wavseq_)")
    p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="결과 manifest CSV 경로 (기본: <out-dir>/wav_sequence_manifest.csv)",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    source_audio: Path = args.source_audio
    cues_csv: Path = args.cues_csv
    out_dir: Path = args.out_dir
    prefix: str = str(args.prefix).strip() or "wavseq_"
    manifest: Path = args.manifest if args.manifest is not None else (out_dir / "wav_sequence_manifest.csv")

    if not source_audio.is_file():
        print(f"[오류] 원본 오디오 파일이 없습니다: {source_audio}", file=sys.stderr)
        return 2

    try:
        ffmpeg = which_ffmpeg()
        total_sec = ffprobe_duration_seconds(source_audio)
        cues = _parse_cues(cues_csv)
        cues = _finalize_cues(cues, total_sec)
    except Exception as e:
        print(f"[오류] 입력 검증 실패: {e}", file=sys.stderr)
        return 2

    outputs: list[Path] = []
    try:
        for i, cue in enumerate(cues, start=1):
            out_wav = (out_dir / f"{prefix}{i:03d}.wav").resolve()
            _split_audio(ffmpeg=ffmpeg, source_audio=source_audio, cue=cue, out_wav=out_wav)
            outputs.append(out_wav)
            assert cue.end_sec is not None
            print(
                f"[{i}/{len(cues)}] {out_wav.name} "
                f"({cue.start_sec:.3f}s ~ {cue.end_sec:.3f}s)"
            )
        _write_manifest(cues=cues, outputs=outputs, manifest_csv=manifest)
    except Exception as e:
        print(f"[오류] 분할 실패: {e}", file=sys.stderr)
        return 1

    print(f"[완료] 분할 WAV {len(outputs)}개 생성: {out_dir}")
    print(f"[완료] manifest CSV: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
