from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.services.ffmpeg_render import (
    FFmpegRenderError,
    burn_subtitles_to_mp4,
    mix_narration_with_bgm,
    parse_resolution,
    run_ffmpeg,
    which_ffmpeg,
    write_concat_list,
)
from app.services.ffprobe_audio import FfprobeError, ffprobe_duration_seconds
from app.wav_sequence_dialog import WavSeqRow


class WavSequenceRenderWorker(CancellableQThread):
    log_line = Signal(str)
    progress = Signal(int, int)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        rows: list[WavSeqRow],
        project_parent: Path,
        resolution: str,
        fps: int,
        global_background_relpath: str,
        bgm_relpath: str,
        bgm_volume_percent: int,
        merged_srt_relpath: str,
        output_relpath: str,
    ) -> None:
        super().__init__()
        self._rows = rows
        self._parent = project_parent
        self._resolution = resolution
        self._fps = max(1, int(fps))
        self._global_background_relpath = (global_background_relpath or "").strip()
        self._bgm_relpath = (bgm_relpath or "").strip()
        self._bgm_volume_percent = int(bgm_volume_percent)
        self._srt_rel = (merged_srt_relpath or "").strip()
        self._out_rel = output_relpath.strip().replace("\\", "/") or "export/wav_sequence.mp4"

    def _resolve_image(self, rel_or_abs: str) -> Path | None:
        p = (rel_or_abs or "").strip()
        if not p:
            return None
        cand = Path(p)
        if not cand.is_absolute():
            cand = (self._parent / cand).resolve()
        else:
            cand = cand.resolve()
        return cand if cand.is_file() else None

    def _render_still_segment(
        self,
        *,
        ffmpeg: str,
        out_mp4: Path,
        duration_sec: float,
        width: int,
        height: int,
        fps: int,
        image: Path | None,
        transition: str,
    ) -> None:
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        fade = ",fade=t=in:st=0:d=0.28" if (transition or "fade").strip().lower() == "fade" else ""
        vf_base = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p{fade}"
        )
        dur = max(0.04, float(duration_sec))
        dsec = f"{dur:.6f}"
        if image is not None and image.is_file():
            cmd = [
                ffmpeg,
                "-y",
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-t",
                dsec,
                "-i",
                str(image.resolve()),
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
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(out_mp4.resolve()),
            ]
        else:
            cmd = [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x1a1a1a:s={width}x{height}:r={fps}:d={dsec}",
                "-t",
                dsec,
                "-vf",
                f"format=yuv420p{fade}",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(out_mp4.resolve()),
            ]
        run_ffmpeg(cmd, cwd=self._parent)

    def run(self) -> None:
        try:
            self.check_cancelled()
            ffmpeg = which_ffmpeg()
            width, height = parse_resolution(self._resolution)
            work = self._parent / "_render" / "work"
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            seg_dir = work / "seg"
            seg_dir.mkdir(parents=True, exist_ok=True)

            global_bg = self._resolve_image(self._global_background_relpath)
            audio_sources: list[Path] = []
            visual_segments: list[tuple[float, str, Path | None]] = []
            eps = 0.04

            for row_idx, row in enumerate(self._rows, start=1):
                self.check_cancelled()
                wav = row.wav_source.resolve()
                if not wav.is_file():
                    self.log_line.emit(f"[WAV 목록 영상] {row_idx}행 WAV 없음: {wav}")
                    continue
                try:
                    src_dur = max(0.0, float(ffprobe_duration_seconds(wav)))
                except (FfprobeError, OSError, ValueError):
                    src_dur = 0.0
                if src_dur <= 0:
                    self.log_line.emit(f"[WAV 목록 영상] {row_idx}행 WAV 길이 확인 실패: {wav}")
                    continue
                audio_sources.append(wav)
                segs = list(row.segments or [])
                if not segs:
                    segs = [
                        {
                            "start_sec": 0.0,
                            "end_sec": src_dur,
                            "transition": row.transition or "fade",
                            "image_relpath": row.image_relpath,
                        }
                    ]
                segs.sort(key=lambda x: float(x.get("start_sec", 0.0)))
                # 구간 JSON이 WAV 전체를 덮지 못하는 경우(특히 마지막 테일 누락),
                # 비디오가 오디오보다 짧아져 마지막이 툭 끊겨 보일 수 있어 빈 구간을 채운다.
                if segs:
                    try:
                        first_st = max(0.0, float(segs[0].get("start_sec", 0.0)))
                    except (TypeError, ValueError):
                        first_st = 0.0
                    if first_st > eps:
                        segs.insert(
                            0,
                            {
                                "start_sec": 0.0,
                                "end_sec": first_st,
                                "transition": row.transition or "fade",
                                "image_relpath": row.image_relpath,
                            },
                        )
                        self.log_line.emit(
                            f"[WAV 목록 영상] {row_idx}행 시작 빈 구간 보정: 0.00~{first_st:.2f}s"
                        )
                    try:
                        last_en = max(0.0, float(segs[-1].get("end_sec", 0.0)))
                    except (TypeError, ValueError):
                        last_en = 0.0
                    if src_dur - last_en > eps:
                        segs.append(
                            {
                                "start_sec": last_en,
                                "end_sec": src_dur,
                                "transition": row.transition or "fade",
                                "image_relpath": row.image_relpath,
                            }
                        )
                        self.log_line.emit(
                            f"[WAV 목록 영상] {row_idx}행 끝 빈 구간 보정: {last_en:.2f}~{src_dur:.2f}s"
                        )
                for s in segs:
                    try:
                        st = float(s.get("start_sec", 0.0))
                        en = float(s.get("end_sec", 0.0))
                    except (TypeError, ValueError):
                        continue
                    st = max(0.0, min(st, src_dur))
                    en = max(st, min(en, src_dur))
                    dur = en - st
                    if dur < eps:
                        continue
                    tr = str(s.get("transition", row.transition)).strip() or "fade"
                    seg_img = self._resolve_image(str(s.get("image_relpath", row.image_relpath)).strip())
                    visual_segments.append((dur, tr, seg_img or global_bg))

            if not audio_sources or not visual_segments:
                self.failed.emit("유효한 오디오/구간이 없어 렌더를 진행할 수 없습니다.")
                return

            bgm_abs: Path | None = None
            if self._bgm_relpath:
                bgm_abs = self._resolve_image(self._bgm_relpath)
                if bgm_abs is None:
                    cand = (self._parent / self._bgm_relpath).resolve()
                    bgm_abs = cand if cand.is_file() else None

            srt_abs = (self._parent / self._srt_rel).resolve() if self._srt_rel else None
            post_steps = 3  # 영상 concat, 오디오+영상 합성, 최종(자막/복사)
            if len(audio_sources) > 1:
                post_steps += 1
            if bgm_abs is not None and bgm_abs.is_file():
                post_steps += 1

            total = len(visual_segments) + post_steps
            step = 0
            segment_paths: list[Path] = []
            for i, (dur, tr, img) in enumerate(visual_segments, start=1):
                self.check_cancelled()
                step += 1
                self.progress.emit(step, total)
                self.log_line.emit(f"씬 {i}: 영상 클립 인코딩…")
                out_seg = seg_dir / f"scene_{i:03d}.mp4"
                self._render_still_segment(
                    ffmpeg=ffmpeg,
                    out_mp4=out_seg,
                    duration_sec=dur,
                    width=width,
                    height=height,
                    fps=self._fps,
                    image=img,
                    transition=tr,
                )
                segment_paths.append(out_seg)

            video_list = work / "video_concat_list.txt"
            write_concat_list(segment_paths, video_list)
            merged_video = work / "merged_video.mp4"
            step += 1
            self.progress.emit(step, total)
            self.log_line.emit("클립 연결(concat) 중…")
            run_ffmpeg(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(video_list.resolve()),
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
                    "-an",
                    str(merged_video.resolve()),
                ],
                cwd=self._parent,
            )

            if len(audio_sources) == 1:
                combined_audio = audio_sources[0]
            else:
                audio_list = work / "audio_concat_list.txt"
                write_concat_list(audio_sources, audio_list)
                combined_audio = work / "merged_audio.wav"
                step += 1
                self.progress.emit(step, total)
                self.log_line.emit("원본 WAV 연결(concat) 중…")
                run_ffmpeg(
                    [
                        ffmpeg,
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(audio_list.resolve()),
                        "-vn",
                        "-acodec",
                        "pcm_s16le",
                        str(combined_audio.resolve()),
                    ],
                    cwd=self._parent,
                )

            merged_nosub = work / "merged_nosub.mp4"
            step += 1
            self.progress.emit(step, total)
            self.log_line.emit("비디오+원본 WAV 합성 중…")
            run_ffmpeg(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(merged_video.resolve()),
                    "-i",
                    str(combined_audio.resolve()),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-shortest",
                    str(merged_nosub.resolve()),
                ],
                cwd=self._parent,
            )

            after_mix = merged_nosub
            if bgm_abs is not None and bgm_abs.is_file():
                step += 1
                self.progress.emit(step, total)
                merged_bgm = work / "merged_bgm.mp4"
                vol = self._bgm_volume_percent / 100.0
                self.log_line.emit(
                    f"BGM 믹스: {self._bgm_relpath} (대략 {self._bgm_volume_percent}% 볼륨)"
                )
                mix_narration_with_bgm(
                    ffmpeg=ffmpeg,
                    input_mp4=merged_nosub,
                    bgm=bgm_abs,
                    out_mp4=merged_bgm,
                    cwd=self._parent,
                    volume=vol,
                )
                after_mix = merged_bgm
            elif self._bgm_relpath:
                self.log_line.emit(f"경고: BGM 파일 없음 — 건너뜀 ({self._bgm_relpath})")

            final_out = (self._parent / Path(self._out_rel)).resolve()
            final_out.parent.mkdir(parents=True, exist_ok=True)
            step += 1
            self.progress.emit(step, total)
            if srt_abs is not None and srt_abs.is_file():
                self.log_line.emit(f"자막 입히기: {self._srt_rel}")
                burn_subtitles_to_mp4(
                    ffmpeg=ffmpeg,
                    input_mp4=after_mix,
                    srt_relative=self._srt_rel,
                    out_mp4=final_out,
                    cwd=self._parent,
                )
            else:
                self.log_line.emit("병합 SRT 없음 — 자막 없이 최종 파일로 복사합니다.")
                shutil.copyfile(after_mix, final_out)

            self.log_line.emit(f"완료: {self._out_rel}")
            self.succeeded.emit(self._out_rel)
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
        except FFmpegRenderError as e:
            self.failed.emit(str(e))
        except OSError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(str(e))
