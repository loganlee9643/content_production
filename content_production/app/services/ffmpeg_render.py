from __future__ import annotations

import shutil
import subprocess
import sys
import wave
from pathlib import Path

from app.models.storyboard import Scene


class FFmpegRenderError(RuntimeError):
    pass


def _no_window_kwargs() -> dict[str, int]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def which_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise FFmpegRenderError("PATH에서 ffmpeg를 찾을 수 없습니다.")
    return exe


def run_ffmpeg(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout_sec: float = 7200.0,
) -> None:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
        encoding="utf-8",
        errors="replace",
        **_no_window_kwargs(),
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise FFmpegRenderError(err or f"ffmpeg 종료 코드 {proc.returncode}")


def parse_resolution(res: str) -> tuple[int, int]:
    parts = res.lower().replace("*", "x").split("x")
    if len(parts) != 2:
        raise FFmpegRenderError(f"해상도 형식 오류: {res!r}")
    return int(parts[0]), int(parts[1])


def scenes_with_existing_wav(
    scenes: list[Scene],
    project_parent: Path,
) -> list[tuple[Scene, Path]]:
    out: list[tuple[Scene, Path]] = []
    for s in scenes:
        if not s.audio_relpath.strip():
            continue
        wav = (project_parent / s.audio_relpath).resolve()
        if wav.is_file():
            out.append((s, wav))
    return out


def _fade_vf_suffix(transition: str, fade_in_sec: float = 0.28) -> str:
    t = (transition or "fade").strip().lower()
    if t == "fade":
        return f",fade=t=in:st=0:d={fade_in_sec}"
    return ""


def _wav_duration_seconds(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
        if frames > 0 and rate > 0:
            return frames / float(rate)
    except (wave.Error, OSError):
        return None
    return None


def render_scene_segment(
    *,
    ffmpeg: str,
    wav: Path,
    out_mp4: Path,
    width: int,
    height: int,
    fps: int,
    background_image: Path | None,
    scene_image: Path | None,
    transition: str,
    cwd: Path,
) -> None:
    """scene_image가 있으면 우선, 없으면 전역 background_image, 둘 다 없으면 단색."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    fade = _fade_vf_suffix(transition)
    vf_base = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p{fade}"
    )
    vf_lavfi = f"format=yuv420p{fade}"

    img_src: Path | None = None
    if scene_image is not None and scene_image.is_file():
        img_src = scene_image
    elif background_image is not None and background_image.is_file():
        img_src = background_image

    w_abs = str(wav.resolve())
    o_abs = str(out_mp4.resolve())
    clip_sec = _wav_duration_seconds(wav)
    clip_sec_arg = f"{clip_sec:.6f}" if clip_sec and clip_sec > 0 else None

    if img_src is not None:
        img = str(img_src.resolve())
        cmd_img = [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            img,
            "-i",
            w_abs,
            "-vf",
            vf_base,
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-tune",
            "stillimage",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]
        if clip_sec_arg is not None:
            cmd_img += ["-t", clip_sec_arg]
        cmd_img += [
            "-shortest",
            o_abs,
        ]
        try:
            run_ffmpeg(cmd_img, cwd=cwd)
            return
        except FFmpegRenderError:
            # 일부 PNG/메타데이터 케이스에서 이미지 입력 인코딩이 실패할 수 있어
            # 같은 오디오로 단색 배경 클립을 재시도하여 전체 렌더 중단을 방지한다.
            pass

    cmd_fallback = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x1a1a1a:s={width}x{height}:r={fps}",
            "-i",
            w_abs,
            "-vf",
            vf_lavfi,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
    ]
    if clip_sec_arg is not None:
        cmd_fallback += ["-t", clip_sec_arg]
    cmd_fallback += [
            "-shortest",
            o_abs,
    ]
    run_ffmpeg(cmd_fallback, cwd=cwd)


def write_concat_list(segment_paths: list[Path], list_file: Path) -> None:
    lines: list[str] = []
    for p in segment_paths:
        ap = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{ap}'")
    list_file.write_text("\n".join(lines), encoding="utf-8")


def concat_segments_copy(
    *,
    ffmpeg: str,
    segment_paths: list[Path],
    out_mp4: Path,
    cwd: Path,
) -> None:
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_mp4.parent / "concat_list.txt"
    write_concat_list(segment_paths, list_file)
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file.resolve()),
        "-fflags",
        "+genpts",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(out_mp4.resolve()),
    ]
    run_ffmpeg(cmd, cwd=cwd)


def mix_narration_with_bgm(
    *,
    ffmpeg: str,
    input_mp4: Path,
    bgm: Path,
    out_mp4: Path,
    cwd: Path,
    volume: float,
) -> None:
    """나레이션 오디오에 BGM을 낮은 볼륨으로 합칩니다. BGM은 루프."""
    vol = max(0.02, min(0.45, float(volume)))
    inp = str(input_mp4.resolve())
    bm = str(bgm.resolve())
    out = str(out_mp4.resolve())
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        inp,
        "-stream_loop",
        "-1",
        "-i",
        bm,
        "-filter_complex",
        f"[1:a]volume={vol}[bga];[0:a][bga]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map",
        "0:v",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        out,
    ]
    run_ffmpeg(cmd, cwd=cwd)


def burn_subtitles_to_mp4(
    *,
    ffmpeg: str,
    input_mp4: Path,
    srt_relative: str,
    out_mp4: Path,
    cwd: Path,
) -> None:
    """cwd 기준 상대 SRT 경로(슬래시). 자막 필터로 비디오 재인코, 오디오 copy."""
    sub = srt_relative.replace("\\", "/").strip()
    if not sub:
        raise FFmpegRenderError("SRT 상대 경로가 비어 있습니다.")
    vf = f"subtitles='{sub}':charenc=UTF-8"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_mp4.resolve()),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "22",
        "-c:a",
        "copy",
        str(out_mp4.resolve()),
    ]
    run_ffmpeg(cmd, cwd=cwd)
