from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@dataclass(frozen=True)
class TimelinePlaybackClip:
    relpath: str
    duration_sec: float
    trim_in_sec: float = 0.0
    trim_out_sec: float = 0.0
    start_sec: float = 0.0

    @property
    def effective_duration_sec(self) -> float:
        return max(0.04, self.duration_sec - self.trim_in_sec - self.trim_out_sec)

    @property
    def end_sec(self) -> float:
        return max(0.0, self.start_sec) + self.effective_duration_sec

    @property
    def is_image(self) -> bool:
        return Path(self.relpath).suffix.lower() in IMAGE_SUFFIXES


class TimelinePlaybackEngine(QThread):
    _PREVIEW_MAX_WIDTH = 960
    _PREVIEW_MAX_HEIGHT = 540

    frameReady = Signal(QImage)
    positionChanged = Signal(float)
    playbackFinished = Signal()
    playbackStateChanged = Signal(bool)
    errorOccurred = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = Lock()
        self._project_parent: Path | None = None
        self._clips: list[TimelinePlaybackClip] = []
        self._position_sec = 0.0
        self._playing = False
        self._stop_requested = False
        self._seek_requested = True

    def configure(self, project_parent: Path | None, clips: list[TimelinePlaybackClip]) -> None:
        with self._lock:
            self._project_parent = Path(project_parent).resolve() if project_parent else None
            self._clips = sorted(clips, key=lambda clip: (clip.start_sec, clip.relpath))
            self._position_sec = min(self._position_sec, self._total_duration_locked())
            self._seek_requested = True

    def play(self, seconds: float | None = None) -> None:
        with self._lock:
            if seconds is not None:
                self._position_sec = max(0.0, min(float(seconds), self._total_duration_locked()))
                self._seek_requested = True
            self._playing = True
            self._stop_requested = False
        if not self.isRunning():
            self.start()
        self.playbackStateChanged.emit(True)

    def pause(self) -> None:
        with self._lock:
            self._playing = False
        self.playbackStateChanged.emit(False)

    def seek(self, seconds: float) -> None:
        with self._lock:
            self._position_sec = max(0.0, min(float(seconds), self._total_duration_locked()))
            self._seek_requested = True

    def stop_engine(self) -> None:
        with self._lock:
            self._playing = False
            self._stop_requested = True
        self.wait(1200)

    def _total_duration_locked(self) -> float:
        return max((clip.end_sec for clip in self._clips), default=0.0)

    def _snapshot(self) -> tuple[Path | None, list[TimelinePlaybackClip], float, bool, bool, bool]:
        with self._lock:
            seek_requested = self._seek_requested
            self._seek_requested = False
            return (
                self._project_parent,
                list(self._clips),
                self._position_sec,
                self._playing,
                self._stop_requested,
                seek_requested,
            )

    def _set_position(self, seconds: float) -> None:
        with self._lock:
            self._position_sec = seconds
        self.positionChanged.emit(seconds)

    @staticmethod
    def _clip_at(clips: list[TimelinePlaybackClip], seconds: float) -> tuple[int, float, float]:
        for index, clip in enumerate(clips):
            start = max(0.0, clip.start_sec)
            end = start + clip.effective_duration_sec
            if start <= seconds < end:
                return index, max(0.0, min(clip.effective_duration_sec, seconds - start)), start
        return -1, 0.0, 0.0

    @classmethod
    def _frame_image(cls, frame) -> QImage:
        scale = min(
            1.0,
            cls._PREVIEW_MAX_WIDTH / max(1, frame.width),
            cls._PREVIEW_MAX_HEIGHT / max(1, frame.height),
        )
        preview_width = max(2, int(frame.width * scale))
        preview_height = max(2, int(frame.height * scale))
        preview_width -= preview_width % 2
        preview_height -= preview_height % 2
        rgb = frame.reformat(
            width=preview_width,
            height=preview_height,
            format="rgb24",
        )
        plane = rgb.planes[0]
        return QImage(
            bytes(plane),
            rgb.width,
            rgb.height,
            plane.line_size,
            QImage.Format.Format_RGB888,
        ).copy()

    def run(self) -> None:
        try:
            import av  # type: ignore[import-not-found]
        except Exception:
            av = None

        container = None
        current_index = -1
        current_path: Path | None = None
        frame_iter = None
        current_fps = 24.0
        last_tick = time.monotonic()

        while True:
            project_parent, clips, position_sec, playing, stop_requested, seek_requested = self._snapshot()
            if stop_requested:
                break
            if not playing:
                time.sleep(0.03)
                continue
            if project_parent is None or not clips:
                self.pause()
                time.sleep(0.03)
                continue

            total = max((clip.end_sec for clip in clips), default=0.0)
            if position_sec >= total:
                self._set_position(total)
                self.playbackFinished.emit()
                self.pause()
                continue

            clip_index, local_sec, clip_start_sec = self._clip_at(clips, position_sec)
            if clip_index < 0:
                now = time.monotonic()
                elapsed = max(0.0, now - last_tick)
                last_tick = now
                next_starts = [clip.start_sec for clip in clips if clip.start_sec > position_sec]
                next_pos = min(next_starts, default=total)
                self._set_position(min(next_pos, position_sec + elapsed))
                time.sleep(0.01)
                continue
            clip = clips[clip_index]
            path = (project_parent / clip.relpath).resolve()
            if not path.is_file():
                self.errorOccurred.emit(f"클립 파일이 없습니다: {clip.relpath}")
                self.pause()
                continue

            if clip.is_image:
                if container is not None:
                    container.close()
                    container = None
                if seek_requested or clip_index != current_index or path != current_path:
                    image = QImage(str(path))
                    if image.isNull():
                        self.errorOccurred.emit(f"이미지를 불러올 수 없습니다: {clip.relpath}")
                        self.pause()
                        continue
                    self.frameReady.emit(image)
                    current_index = clip_index
                    current_path = path
                    frame_iter = None
                    last_tick = time.monotonic()
                now = time.monotonic()
                elapsed = max(0.0, now - last_tick)
                last_tick = now
                next_position = min(clip.end_sec, position_sec + elapsed)
                self._set_position(next_position)
                if next_position >= clip.end_sec - 0.001:
                    current_index = -1
                    current_path = None
                time.sleep(0.01)
                continue

            if av is None:
                self.errorOccurred.emit("PyAV를 불러올 수 없습니다. requirements 설치가 필요합니다.")
                self.pause()
                continue

            if seek_requested or clip_index != current_index or path != current_path or frame_iter is None:
                if container is not None:
                    container.close()
                container = av.open(str(path))
                stream = next((s for s in container.streams if s.type == "video"), None)
                if stream is None:
                    self.errorOccurred.emit(f"비디오 스트림이 없습니다: {clip.relpath}")
                    self.pause()
                    continue
                current_fps = float(stream.average_rate) if stream.average_rate else 24.0
                seek_target_sec = clip.trim_in_sec + local_sec
                container.seek(int(seek_target_sec / float(stream.time_base)), stream=stream)
                frame_iter = container.decode(stream)
                current_index = clip_index
                current_path = path
                last_tick = time.monotonic()

            try:
                frame = next(frame_iter)
            except StopIteration:
                next_pos = clip_start_sec + clip.effective_duration_sec
                self._set_position(min(total, next_pos))
                current_index = -1
                frame_iter = None
                continue

            frame_sec = float(frame.pts * frame.time_base) if frame.pts is not None else clip.trim_in_sec + local_sec
            if frame_sec < clip.trim_in_sec + local_sec - 0.01:
                continue
            if frame_sec >= clip.duration_sec - clip.trim_out_sec:
                next_pos = clip_start_sec + clip.effective_duration_sec
                self._set_position(min(total, next_pos))
                current_index = -1
                frame_iter = None
                continue

            self.frameReady.emit(self._frame_image(frame))

            timeline_sec = clip_start_sec + max(0.0, frame_sec - clip.trim_in_sec)
            self._set_position(min(total, timeline_sec))

            delay = max(0.001, min(0.08, 1.0 / max(1.0, current_fps)))
            elapsed = time.monotonic() - last_tick
            if elapsed < delay:
                time.sleep(delay - elapsed)
            last_tick = time.monotonic()

        if container is not None:
            container.close()
