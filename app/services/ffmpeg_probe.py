from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class FfmpegProbeResult:
    ok: bool
    message: str


def probe_ffmpeg() -> FfmpegProbeResult:
    exe = shutil.which("ffmpeg")
    if not exe:
        return FfmpegProbeResult(False, "PATH에서 ffmpeg를 찾지 못했습니다.")
    try:
        p = subprocess.run(
            [exe, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        out = (p.stdout or "") + (p.stderr or "")
        first = next((ln for ln in out.splitlines() if ln.strip()), "(출력 없음)")
        if p.returncode != 0:
            return FfmpegProbeResult(False, f"ffmpeg 실행 실패(code={p.returncode}): {first}")
        return FfmpegProbeResult(True, f"ffmpeg: {first}")
    except subprocess.TimeoutExpired:
        return FfmpegProbeResult(False, "ffmpeg -version 실행이 시간 초과되었습니다.")
    except OSError as e:
        return FfmpegProbeResult(False, f"ffmpeg 실행 오류: {e}")
