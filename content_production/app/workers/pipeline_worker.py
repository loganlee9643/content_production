from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.models.storyboard import StoryProject
from app.services.ffmpeg_probe import probe_ffmpeg
from app.services.ffprobe_audio import probe_ffprobe_cli


class PipelineWorker(CancellableQThread):
    """백그라운드 검증·준비 작업(이후 TTS/자막/렌더 단계 확장)."""

    log_line = Signal(str)
    finished_ok = Signal(bool)

    def __init__(self, project: StoryProject, project_path: Path | None) -> None:
        super().__init__()
        self._project = project
        self._project_path = project_path

    def run(self) -> None:
        ok = True
        try:
            self.check_cancelled()
            self.log_line.emit("--- 프로젝트 검증 시작 ---")
            if self._project_path:
                self.log_line.emit(f"파일: {self._project_path}")
            self.log_line.emit(f"씬 수: {len(self._project.scenes)}")
            nar_len = sum(len(s.narration_ko) for s in self._project.scenes)
            self.log_line.emit(f"나레이션 총 글자 수(공백 포함): {nar_len}")
            empty = [s.scene_id for s in self._project.scenes if not s.narration_ko.strip()]
            if empty:
                self.log_line.emit(f"경고: 나레이션이 비어 있는 씬 ID: {empty}")
            ff = probe_ffmpeg()
            self.log_line.emit(ff.message)
            if not ff.ok:
                ok = False
            fp = probe_ffprobe_cli()
            self.log_line.emit(fp.message)
            if not fp.ok:
                ok = False
            self.log_line.emit("--- 프로젝트 검증 끝 ---")
        except WorkerCancelled:
            ok = False
            self.log_line.emit("작업이 중지되었습니다.")
        except Exception as e:
            ok = False
            self.log_line.emit(f"오류: {e}")
        self.finished_ok.emit(ok)
