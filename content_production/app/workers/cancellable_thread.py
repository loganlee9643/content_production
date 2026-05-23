from __future__ import annotations

from PySide6.QtCore import QThread


class WorkerCancelled(Exception):
    """사용자가 작업 중지를 요청한 경우."""


class CancellableQThread(QThread):
    """QThread + 협력적 취소(request_cancel / check_cancelled)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cancel_requested = False

    def request_cancel(self) -> None:
        self._cancel_requested = True
        self.requestInterruption()

    def is_cancelled(self) -> bool:
        return self._cancel_requested or self.isInterruptionRequested()

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise WorkerCancelled()
