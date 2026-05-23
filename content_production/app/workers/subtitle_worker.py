from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.models.storyboard import Scene
from app.services.srt_build import build_merged_srt


class SubtitleWorker(CancellableQThread):
    log_line = Signal(str)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        scenes: list[Scene],
        project_parent: Path,
        max_line_chars: int,
        output_relpath: str = "subs/merged.srt",
    ) -> None:
        super().__init__()
        self._scenes = scenes
        self._project_parent = project_parent
        self._max_line_chars = max_line_chars
        self._output_relpath = output_relpath.strip() or "subs/merged.srt"

    def run(self) -> None:
        try:
            self.check_cancelled()
            self.log_line.emit("병합 SRT 생성 중…")
            body = build_merged_srt(
                self._scenes,
                self._project_parent,
                max_line_chars=self._max_line_chars,
            )
            out_abs = self._project_parent / self._output_relpath
            out_abs.parent.mkdir(parents=True, exist_ok=True)
            out_abs.write_text(body, encoding="utf-8")
            self.log_line.emit(f"저장됨: {self._output_relpath}")
            self.succeeded.emit(self._output_relpath)
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
        except Exception as e:
            self.failed.emit(str(e))
