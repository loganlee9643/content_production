from __future__ import annotations

import shutil
import subprocess
import sys
import wave
import re
from dataclasses import dataclass
from pathlib import Path


class FfprobeError(RuntimeError):
    pass


@dataclass
class FfprobeCliResult:
    ok: bool
    message: str


def _no_window_kwargs() -> dict[str, int]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _resolve_ffprobe() -> str | None:
    """PATH의 ffprobe, 없으면 ffmpeg과 같은 디렉터리의 ffprobe (Windows 등)."""
    w = shutil.which("ffprobe")
    if w:
        return w
    ffm = shutil.which("ffmpeg")
    if not ffm:
        return None
    parent = Path(ffm).resolve().parent
    for name in ("ffprobe.exe", "ffprobe"):
        cand = parent / name
        if cand.is_file():
            return str(cand)
    return None


def _resolve_ffmpeg() -> str | None:
    w = shutil.which("ffmpeg")
    if w:
        return w
    ffp = _resolve_ffprobe()
    if not ffp:
        return None
    parent = Path(ffp).resolve().parent
    for name in ("ffmpeg.exe", "ffmpeg"):
        cand = parent / name
        if cand.is_file():
            return str(cand)
    return None


def probe_ffprobe_cli() -> FfprobeCliResult:
    exe = _resolve_ffprobe()
    if not exe:
        return FfprobeCliResult(
            False,
            "PATH에서 ffprobe를 찾지 못했습니다. "
            "PowerShell에서 ffprobe -version 이 되는지 확인한 뒤, 앱을 완전히 종료하고 다시 실행하세요.",
        )
    try:
        p = subprocess.run(
            [exe, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            **_no_window_kwargs(),
        )
        out = (p.stdout or "") + (p.stderr or "")
        first = next((ln for ln in out.splitlines() if ln.strip()), "(출력 없음)")
        if p.returncode != 0:
            return FfprobeCliResult(False, f"ffprobe 실행 실패(code={p.returncode}): {first}")
        return FfprobeCliResult(True, f"ffprobe: {first}")
    except subprocess.TimeoutExpired:
        return FfprobeCliResult(False, "ffprobe -version 실행이 시간 초과되었습니다.")
    except OSError as e:
        return FfprobeCliResult(False, f"ffprobe 실행 오류: {e}")


def ffprobe_duration_seconds(media: Path) -> float:
    """미디어 파일 재생 길이(초). ffprobe PATH(또는 ffmpeg 동일 폴더) 필요."""
    exe = _resolve_ffprobe()
    if not exe:
        raise FfprobeError(
            "ffprobe를 찾지 못했습니다. ffmpeg와 함께 설치되어 있는지 확인하고, "
            "설치 후에는 앱을 다시 실행하세요."
        )
    if not media.is_file():
        raise FfprobeError(f"파일이 없습니다: {media}")
    def _probe_lines(show_entries: str) -> list[str]:
        cmd = [
            exe,
            "-v",
            "error",
            "-show_entries",
            show_entries,
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
                **_no_window_kwargs(),
            )
        except subprocess.TimeoutExpired as e:
            raise FfprobeError("ffprobe 시간 초과") from e
        except OSError as e:
            raise FfprobeError(f"ffprobe 실행 오류: {e}") from e
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            raise FfprobeError(err or f"ffprobe 실패(code={proc.returncode})")
        return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]

    sec: float | None = None
    for entries in ("format=duration", "stream=duration,format=duration"):
        lines = _probe_lines(entries)
        for ln in lines:
            if ln.upper() == "N/A":
                continue
            try:
                v = float(ln)
            except ValueError:
                continue
            if v > 0:
                sec = v
                break
        if sec is not None:
            break
    if sec is None and media.suffix.lower() == ".wav":
        try:
            with wave.open(str(media), "rb") as wf:
                rate = wf.getframerate()
                frames = wf.getnframes()
            if rate > 0 and frames > 0:
                sec = frames / float(rate)
        except (wave.Error, OSError):
            sec = None
    if sec is None:
        ffmpeg = _resolve_ffmpeg()
        if ffmpeg:
            cmd = [ffmpeg, "-hide_banner", "-i", str(media)]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=False,
                    **_no_window_kwargs(),
                )
                out = (proc.stderr or "") + "\n" + (proc.stdout or "")
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
                if m:
                    hh = float(m.group(1))
                    mm = float(m.group(2))
                    ss = float(m.group(3))
                    cand = (hh * 3600.0) + (mm * 60.0) + ss
                    if cand > 0:
                        sec = cand
            except (subprocess.TimeoutExpired, OSError):
                sec = None
    if sec is None:
        raise FfprobeError("길이 파싱 실패: ffprobe가 N/A를 반환했습니다.")
    if sec <= 0:
        return 0.04
    return sec
