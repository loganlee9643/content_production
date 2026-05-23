from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.models.storyboard import StoryProject
from app.services.ffmpeg_render import (
    FFmpegRenderError,
    burn_subtitles_to_mp4,
    concat_segments_copy,
    mix_narration_with_bgm,
    parse_resolution,
    render_scene_segment,
    scenes_with_existing_wav,
    which_ffmpeg,
)

EXPORT_FINAL_REL = "export/final.mp4"


class FinalRenderWorker(CancellableQThread):
    log_line = Signal(str)
    progress = Signal(int, int)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        project: StoryProject,
        project_parent: Path,
        *,
        output_relpath: str | None = None,
    ) -> None:
        super().__init__()
        self._project = project
        self._parent = project_parent
        rel = (output_relpath or EXPORT_FINAL_REL).strip().replace("\\", "/")
        self._output_relpath = rel or EXPORT_FINAL_REL

    def run(self) -> None:
        try:
            self.check_cancelled()
            ffmpeg = which_ffmpeg()
            w, h = parse_resolution(self._project.resolution)
            fps = max(1, int(self._project.fps))
            pairs = scenes_with_existing_wav(self._project.scenes, self._parent)
            if not pairs:
                self.failed.emit("렌더할 WAV가 있는 씬이 없습니다. TTS를 먼저 실행하세요.")
                return

            work = self._parent / "_render" / "work"
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            seg_dir = work / "seg"
            seg_dir.mkdir()

            bg: Path | None = None
            if self._project.background_image_relpath.strip():
                cand = (self._parent / self._project.background_image_relpath).resolve()
                if cand.is_file():
                    bg = cand
                else:
                    self.log_line.emit(f"경고: 배경 이미지 없음, 단색 사용 — {cand}")

            bgm_abs: Path | None = None
            bgm_rel = (self._project.bgm_relpath or "").strip()
            if bgm_rel:
                cand = (self._parent / bgm_rel).resolve()
                bgm_abs = cand if cand.is_file() else None

            srt_rel = (self._project.merged_srt_relpath or "").strip()
            srt_abs = (self._parent / srt_rel).resolve() if srt_rel else None

            post_steps = 2  # 클립 concat, 최종(자막/복사)
            if bgm_abs is not None:
                post_steps += 1

            segment_paths: list[Path] = []
            total = len(pairs) + post_steps
            step = 0
            for i, (scene, wav) in enumerate(pairs, start=1):
                self.check_cancelled()
                step += 1
                self.progress.emit(step, total)
                self.log_line.emit(f"씬 {scene.scene_id}: 영상 클립 인코딩…")
                out_seg = seg_dir / f"scene_{scene.scene_id:03d}.mp4"
                scene_img: Path | None = None
                if scene.image_relpath.strip():
                    ip = (self._parent / scene.image_relpath).resolve()
                    if ip.is_file():
                        scene_img = ip
                    else:
                        self.log_line.emit(f"경고: 씬 {scene.scene_id} 이미지 없음 — 전역 배경 사용 — {ip}")
                render_scene_segment(
                    ffmpeg=ffmpeg,
                    wav=wav,
                    out_mp4=out_seg,
                    width=w,
                    height=h,
                    fps=fps,
                    background_image=bg,
                    scene_image=scene_img,
                    transition=scene.transition or "fade",
                    cwd=self._parent,
                )
                segment_paths.append(out_seg)

            merged = work / "merged_nosub.mp4"
            step += 1
            self.progress.emit(step, total)
            self.log_line.emit("클립 연결(concat) 중…")
            concat_segments_copy(
                ffmpeg=ffmpeg,
                segment_paths=segment_paths,
                out_mp4=merged,
                cwd=self._parent,
            )

            after_mix: Path = merged
            if bgm_abs is not None:
                step += 1
                self.progress.emit(step, total)
                merged_bgm = work / "merged_bgm.mp4"
                vol = self._project.bgm_volume_percent / 100.0
                self.log_line.emit(
                    f"BGM 믹스: {bgm_rel} (대략 {self._project.bgm_volume_percent}% 볼륨)"
                )
                mix_narration_with_bgm(
                    ffmpeg=ffmpeg,
                    input_mp4=merged,
                    bgm=bgm_abs,
                    out_mp4=merged_bgm,
                    cwd=self._parent,
                    volume=vol,
                )
                after_mix = merged_bgm
            elif bgm_rel:
                self.log_line.emit(f"경고: BGM 파일 없음 — 건너뜀 ({bgm_rel})")

            export_dir = self._parent / "export"
            export_dir.mkdir(parents=True, exist_ok=True)
            final_rel = self._output_relpath
            final_out = (self._parent / Path(final_rel)).resolve()
            final_out.parent.mkdir(parents=True, exist_ok=True)

            step += 1
            self.progress.emit(step, total)
            if srt_abs is not None and srt_abs.is_file():
                self.log_line.emit(f"자막 입히기: {srt_rel}")
                burn_subtitles_to_mp4(
                    ffmpeg=ffmpeg,
                    input_mp4=after_mix,
                    srt_relative=srt_rel,
                    out_mp4=final_out,
                    cwd=self._parent,
                )
            else:
                self.log_line.emit("병합 SRT 없음 — 자막 없이 최종 파일로 복사합니다.")
                shutil.copyfile(after_mix, final_out)

            self.log_line.emit(f"완료: {final_rel}")
            self.succeeded.emit(final_rel)
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
        except FFmpegRenderError as e:
            self.failed.emit(str(e))
        except OSError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(str(e))
