from __future__ import annotations

import json
import audioop
import hashlib
import importlib.util
import wave
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QMimeData,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QColor, QDrag, QDragEnterEvent, QDragMoveEvent, QDropEvent, QIcon, QMouseEvent, QPainter, QPen, QPixmap, QPolygon, QWheelEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.models.storyboard import StoryProject
from app.services.ffmpeg_render import concat_segments_normalized, run_ffmpeg, which_ffmpeg
from app.services.ffprobe_audio import FfprobeError, ffprobe_duration_seconds
from app.services.timeline_audio import TimelineAudioRenderer
from app.services.timeline_playback import TimelinePlaybackClip, TimelinePlaybackEngine
from app.services.srt_build import parse_srt_file, seconds_to_srt_timestamp


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DEFAULT_IMAGE_DURATION_SEC = 5.0


@dataclass
class EditorClip:
    relpath: str
    duration_sec: float = 0.0
    trim_in_sec: float = 0.0
    trim_out_sec: float = 0.0
    start_sec: float = 0.0

    @property
    def effective_duration_sec(self) -> float:
        return max(0.04, self.duration_sec - self.trim_in_sec - self.trim_out_sec)

    @property
    def is_image(self) -> bool:
        return Path(self.relpath).suffix.lower() in IMAGE_SUFFIXES

    def to_dict(self) -> dict[str, object]:
        return {
            "relpath": self.relpath,
            "duration_sec": self.duration_sec,
            "trim_in_sec": self.trim_in_sec,
            "trim_out_sec": self.trim_out_sec,
            "start_sec": self.start_sec,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> EditorClip:
        return EditorClip(
            relpath=str(data.get("relpath", "") or ""),
            duration_sec=float(data.get("duration_sec", 0.0) or 0.0),
            trim_in_sec=max(0.0, float(data.get("trim_in_sec", 0.0) or 0.0)),
            trim_out_sec=max(0.0, float(data.get("trim_out_sec", 0.0) or 0.0)),
            start_sec=max(0.0, float(data.get("start_sec", 0.0) or 0.0)),
        )


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _fmt_duration(sec: float) -> str:
    sec = max(0.0, float(sec or 0.0))
    m = int(sec // 60)
    s = sec - (m * 60)
    return f"{m:02d}:{s:05.2f}"


def _fmt_badge_duration(sec: float) -> str:
    sec = max(0.0, float(sec or 0.0))
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


MEDIA_MIME_TYPE = "application/x-content-production-media"
MEDIA_CARD_SIZE = QSize(124, 96)
MEDIA_THUMB_SIZE = QSize(112, 63)


def _media_checkbox_rect(item_rect: QRect) -> QRect:
    return QRect(item_rect.left() + 8, item_rect.top() + 8, 18, 18)


def _media_add_rect(item_rect: QRect) -> QRect:
    return QRect(
        item_rect.left() + 6 + MEDIA_THUMB_SIZE.width() - 22,
        item_rect.top() + 6 + MEDIA_THUMB_SIZE.height() - 20,
        18,
        18,
    )


def _is_checked_state(value: object) -> bool:
    if value == Qt.CheckState.Checked:
        return True
    try:
        return int(value) == int(Qt.CheckState.Checked.value)
    except (TypeError, ValueError):
        return False


def _encode_media_rels(rels: list[str]) -> bytes:
    return "\n".join(rel for rel in rels if rel).encode("utf-8")


def _decode_media_rels(data: bytes) -> list[str]:
    return [line.strip() for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]


class MediaCardDelegate(QStyledItemDelegate):
    def sizeHint(self, _option: QStyleOptionViewItem, _index) -> QSize:
        return MEDIA_CARD_SIZE

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        checked = _is_checked_state(index.data(Qt.ItemDataRole.CheckStateRole))
        any_checked = bool(index.data(Qt.ItemDataRole.UserRole + 4) or False)
        active = selected or checked
        bg = QColor("#f6f1ff") if active else QColor("#f3f4f8") if hover else QColor("#ffffff")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 6, 6)

        icon = index.data(Qt.ItemDataRole.DecorationRole)
        thumb_rect = QRect(rect.left() + 6, rect.top() + 6, MEDIA_THUMB_SIZE.width(), MEDIA_THUMB_SIZE.height())
        if isinstance(icon, QIcon) and not icon.isNull():
            painter.drawPixmap(thumb_rect, icon.pixmap(MEDIA_THUMB_SIZE))
        else:
            painter.fillRect(thumb_rect, QColor("#eef1f7"))
            painter.setPen(QColor("#6f7482"))
            painter.drawText(thumb_rect, Qt.AlignmentFlag.AlignCenter, "MP4")

        show_checkbox = active or any_checked
        if show_checkbox:
            check_rect = _media_checkbox_rect(rect)
            painter.setBrush(QColor("#7c2df2") if checked else QColor("#ffffff"))
            painter.setPen(QPen(QColor("#7c2df2") if checked else QColor("#858b9a"), 1))
            painter.drawRoundedRect(check_rect, 4, 4)
            if checked:
                painter.setPen(QPen(QColor("#ffffff"), 2))
                painter.drawLine(check_rect.left() + 5, check_rect.center().y(), check_rect.left() + 8, check_rect.bottom() - 6)
                painter.drawLine(check_rect.left() + 8, check_rect.bottom() - 6, check_rect.right() - 4, check_rect.top() + 5)

        dur = str(index.data(Qt.ItemDataRole.UserRole + 2) or "")
        relpath = str(index.data(Qt.ItemDataRole.UserRole) or "")
        if dur and Path(relpath).suffix.lower() not in IMAGE_SUFFIXES:
            dur_rect = QRect(thumb_rect.left() + 4, thumb_rect.bottom() - 22, 42, 18)
            painter.fillRect(dur_rect, QColor(255, 255, 255, 220))
            painter.setPen(QColor("#1f2430"))
            painter.drawText(dur_rect.adjusted(3, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, dur)

        title = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        title_rect = QRect(rect.left() + 6, thumb_rect.bottom() + 6, rect.width() - 12, 22)
        painter.setPen(QColor("#3e4352"))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)

        if hover:
            plus_rect = _media_add_rect(rect)
            painter.setBrush(QColor("#7c2df2"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(plus_rect, 4, 4)
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(plus_rect.center().x(), plus_rect.top() + 4, plus_rect.center().x(), plus_rect.bottom() - 4)
            painter.drawLine(plus_rect.left() + 4, plus_rect.center().y(), plus_rect.right() - 4, plus_rect.center().y())
        painter.restore()


class PlayerControlButton(QPushButton):
    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self._kind = kind
        self._playing = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("playerControlButton")
        self.setFixedSize(36 if kind == "play" else 28, 36 if kind == "play" else 28)

    def set_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        if self._kind == "play":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#252736"))
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
            painter.setBrush(QColor("#ffffff"))
            if self._playing:
                w = rect.width()
                h = rect.height()
                painter.drawRoundedRect(w // 2 - 6, h // 2 - 8, 4, 16, 2, 2)
                painter.drawRoundedRect(w // 2 + 2, h // 2 - 8, 4, 16, 2, 2)
            else:
                cx = rect.width() // 2
                cy = rect.height() // 2
                painter.drawPolygon(
                    QPolygon(
                        [
                            QPoint(cx - 4, cy - 10),
                            QPoint(cx - 4, cy + 10),
                            QPoint(cx + 11, cy),
                        ]
                    )
                )
            return

        if self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#f1f2f6"))
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        painter.setPen(QPen(QColor("#272a3a"), 1.6))
        cx = rect.width() // 2
        cy = rect.height() // 2
        if self._kind == "start":
            painter.drawLine(cx - 10, cy - 10, cx - 10, cy + 10)
            painter.drawPolygon(QPolygon([QPoint(cx + 8, cy - 10), QPoint(cx + 8, cy + 10), QPoint(cx - 4, cy)]))
        elif self._kind in {"back5", "forward5"}:
            painter.setPen(QColor("#272a3a"))
            text = "↶5" if self._kind == "back5" else "5↷"
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


class MediaListWidget(QListWidget):
    addRequested = Signal(str)
    checkedCountChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drag_start_pos = QPoint()
        self._pressed_item: QListWidgetItem | None = None
        self._drag_items: list[QListWidgetItem] = []
        self._selection_before_press: list[QListWidgetItem] = []
        self._selection_anchor_row = -1
        self._handled_selection_press = False
        self._deferred_plain_click = False
        self._drag_started = False
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def mimeData(self, items: list[QListWidgetItem]) -> QMimeData:
        mime = super().mimeData(items)
        rels = self._drag_relpaths(items)
        if rels:
            mime.setData(MEDIA_MIME_TYPE, _encode_media_rels(rels))
        return mime

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        item = self._pressed_item or self.currentItem()
        if item is None:
            return
        source_items = self._drag_items or self.selectedItems()
        rels = self._drag_relpaths(source_items)
        if not rels:
            return
        mime = QMimeData()
        mime.setData(MEDIA_MIME_TYPE, _encode_media_rels(rels))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(item.icon().pixmap(self.iconSize()))
        drag.exec(supported_actions if supported_actions else Qt.DropAction.CopyAction)
        if self._selection_before_press:
            self._restore_selected_items(self._selection_before_press)

    def _restore_selected_items(self, items: list[QListWidgetItem]) -> None:
        selected_ids = {id(item) for item in items}
        self.blockSignals(True)
        try:
            for idx in range(self.count()):
                item = self.item(idx)
                if item is not None:
                    item.setSelected(id(item) in selected_ids)
        finally:
            self.blockSignals(False)
        self._emit_checked_count()

    def _select_range_to(self, clicked_item: QListWidgetItem, *, extend: bool) -> None:
        clicked_row = self.row(clicked_item)
        anchor_row = self._selection_anchor_row
        if not (0 <= anchor_row < self.count()):
            selected_rows = sorted(self.row(item) for item in self.selectedItems())
            anchor_row = selected_rows[0] if selected_rows else clicked_row
        first, last = sorted((anchor_row, clicked_row))
        selection_model = self.selectionModel()
        self.blockSignals(True)
        try:
            if not extend:
                selection_model.clearSelection()
            for row in range(first, last + 1):
                index = self.model().index(row, 0)
                if index.isValid():
                    selection_model.select(index, QItemSelectionModel.SelectionFlag.Select)
            clicked_index = self.model().index(clicked_row, 0)
            if clicked_index.isValid():
                selection_model.setCurrentIndex(
                    clicked_index,
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        finally:
            self.blockSignals(False)
        self._selection_anchor_row = anchor_row
        self._selection_before_press = list(self.selectedItems())
        self._drag_items = list(self._selection_before_press)
        self._handled_selection_press = True
        self._emit_checked_count()

    def _drag_relpaths(self, items: list[QListWidgetItem]) -> list[str]:
        checked_items = [
            self.item(idx)
            for idx in range(self.count())
            if self.item(idx) is not None and _is_checked_state(self.item(idx).checkState())
        ]
        source_items = checked_items if checked_items else items
        if not source_items and self.currentItem() is not None:
            source_items = [self.currentItem()]
        rels: list[str] = []
        seen: set[str] = set()
        for item in source_items:
            rel = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if rel and rel not in seen:
                seen.add(rel)
                rels.append(rel)
        return rels

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._drag_start_pos = event.pos()
        self._pressed_item = self.itemAt(event.pos())
        self._drag_items = []
        self._selection_before_press = list(self.selectedItems())
        self._handled_selection_press = False
        self._deferred_plain_click = False
        self._drag_started = False
        item = self._pressed_item
        if (
            item is None
            and event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            self.clearSelection()
            self._selection_anchor_row = -1
            self.blockSignals(True)
            try:
                for idx in range(self.count()):
                    media_item = self.item(idx)
                    if media_item is not None and _is_checked_state(media_item.checkState()):
                        media_item.setCheckState(Qt.CheckState.Unchecked)
            finally:
                self.blockSignals(False)
            self._emit_checked_count()
            event.accept()
            return
        if item is not None:
            rect = self.visualItemRect(item).adjusted(2, 2, -2, -2)
            rel = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if _media_add_rect(rect).contains(event.pos()) and rel:
                self.addRequested.emit(rel)
                event.accept()
                return
            if _media_checkbox_rect(rect).contains(event.pos()):
                state = Qt.CheckState.Checked if _is_checked_state(item.checkState()) else Qt.CheckState.Unchecked
                self.clearSelection()
                self._selection_anchor_row = -1
                item.setCheckState(Qt.CheckState.Unchecked if state == Qt.CheckState.Checked else Qt.CheckState.Checked)
                self._emit_checked_count()
                self.viewport().update()
                event.accept()
                return
            modifiers = event.modifiers() | QApplication.keyboardModifiers()
            shift_pressed = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            control_pressed = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
            if event.button() == Qt.MouseButton.LeftButton and shift_pressed:
                self._select_range_to(item, extend=control_pressed)
                event.accept()
                return
            plain_left_click = (
                event.button() == Qt.MouseButton.LeftButton
                and modifiers == Qt.KeyboardModifier.NoModifier
            )
            if plain_left_click and item.isSelected() and len(self._selection_before_press) > 1:
                self._deferred_plain_click = True
                self._drag_items = list(self._selection_before_press)
                self.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
                event.accept()
                return
        super().mousePressEvent(event)
        if item is not None and event.button() == Qt.MouseButton.LeftButton:
            self._selection_anchor_row = self.row(item)
        self._selection_before_press = list(self.selectedItems())
        self._drag_items = list(self._selection_before_press)
        self._emit_checked_count()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        distance = (event.pos() - self._drag_start_pos).manhattanLength()
        if distance < 8:
            return
        if self._pressed_item is not None:
            self._drag_started = True
            self.startDrag(Qt.DropAction.CopyAction)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._handled_selection_press:
            event.accept()
        elif self._deferred_plain_click and not self._drag_started and self._pressed_item is not None:
            self.clearSelection()
            self._pressed_item.setSelected(True)
            self.setCurrentItem(self._pressed_item)
            self._selection_anchor_row = self.row(self._pressed_item)
            self._emit_checked_count()
            event.accept()
        else:
            super().mouseReleaseEvent(event)
        self._pressed_item = None
        self._drag_items = []
        self._selection_before_press = []
        self._handled_selection_press = False
        self._deferred_plain_click = False
        self._drag_started = False

    def _emit_checked_count(self) -> None:
        count = 0
        for idx in range(self.count()):
            item = self.item(idx)
            if item is not None and _is_checked_state(item.checkState()):
                count += 1
        any_selected = count > 0 or bool(self.selectedItems())
        for idx in range(self.count()):
            item = self.item(idx)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole + 4, any_selected)
        self.checkedCountChanged.emit(count)
        self.viewport().update()


class TimelineStrip(QWidget):
    clipSelected = Signal(int)
    audioSelected = Signal()
    clipMoved = Signal(int, float)
    imageResized = Signal(int, float, float)
    audioMoved = Signal(float)
    gapDeleted = Signal(float, float)
    playheadChanged = Signal(float)
    mediaDropped = Signal(list, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clips: list[EditorClip] = []
        self._selected = -1
        self._playhead_sec = 0.0
        self._playhead_selected = False
        self._press_index = -1
        self._press_x = 0.0
        self._press_start_sec = 0.0
        self._press_duration_sec = 0.0
        self._drag_preview_sec: float | None = None
        self._resize_preview_duration: float | None = None
        self._drag_mode = ""
        self._drop_index = -1
        self._drop_seconds = 0.0
        self._hover_gap: tuple[float, float] | None = None
        self._project_parent: Path | None = None
        self._audio_relpath = ""
        self._audio_duration_sec = 0.0
        self._audio_start_sec = 0.0
        self._audio_peaks: list[float] = []
        self._audio_scale_peak = 1.0
        self._audio_selected = False
        self._thumbnail_cache: dict[tuple[str, int, int], QPixmap] = {}
        self._zoom = 1.0
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_project_parent(self, project_parent: Path | None) -> None:
        self._project_parent = Path(project_parent).resolve() if project_parent else None
        self._thumbnail_cache.clear()
        self.update()

    def set_clips(self, clips: list[EditorClip], selected: int) -> None:
        self._clips = list(clips)
        self._selected = selected
        self._update_content_width()
        self.update()

    def set_audio_track(self, relpath: str, duration_sec: float, start_sec: float = 0.0) -> None:
        self._audio_relpath = relpath
        self._audio_duration_sec = max(0.0, float(duration_sec or 0.0))
        self._audio_start_sec = max(0.0, float(start_sec or 0.0))
        self._audio_peaks = self._load_waveform_peaks(relpath)
        nonzero = [peak for peak in self._audio_peaks if peak > 0.0005]
        self._audio_scale_peak = (
            max(0.01, sorted(nonzero)[int(len(nonzero) * 0.92)])
            if nonzero
            else 1.0
        )
        self._update_content_width()
        self.update()

    def set_audio_selected(self, selected: bool) -> None:
        self._audio_selected = bool(selected)
        self.update()

    def set_playhead_seconds(self, seconds: float) -> None:
        old_x = int(self._playhead_x())
        self._playhead_sec = max(0.0, float(seconds or 0.0))
        new_x = int(self._playhead_x())
        if old_x == new_x:
            return
        self.update(QRect(old_x - 7, 22, 15, 166))
        self.update(QRect(new_x - 7, 22, 15, 166))

    def _total_duration(self) -> float:
        return max((c.start_sec + c.effective_duration_sec for c in self._clips), default=0.0)

    def _timeline_duration(self) -> float:
        audio_end = self._audio_start_sec + self._audio_duration_sec if self._audio_relpath else 0.0
        return max(self._total_duration(), audio_end)

    def _px_per_second(self) -> float:
        return max(1.0, 10.0 * self._zoom)

    def _content_width(self) -> int:
        return max(420, int(16 + self._timeline_duration() * self._px_per_second()))

    def _canvas_width(self) -> int:
        return max(self.width(), self._content_width())

    def _update_content_width(self) -> None:
        self.setMinimumWidth(self._content_width())

    def _clip_bounds(self) -> list[tuple[int, float, float]]:
        bounds: list[tuple[int, float, float]] = []
        for idx, clip in enumerate(self._clips):
            resizing = idx == self._press_index and self._drag_mode in {"image_left", "image_right"}
            moving = idx == self._press_index and self._drag_mode == "clip"
            start_sec = self._drag_preview_sec if (moving or resizing) and self._drag_preview_sec is not None else clip.start_sec
            duration_sec = (
                self._resize_preview_duration
                if resizing and self._resize_preview_duration is not None
                else clip.effective_duration_sec
            )
            x = 8.0 + max(0.0, start_sec) * self._px_per_second()
            w = max(8.0, duration_sec * self._px_per_second())
            bounds.append((idx, x, x + w))
        return bounds

    @staticmethod
    def _image_handle_hit(x: float, left: float, right: float) -> str:
        handle_width = 10.0
        if abs(x - left) <= handle_width:
            return "left"
        if abs(x - right) <= handle_width:
            return "right"
        return ""

    def _selected_image_handle_at(self, x: float, y: float) -> tuple[int, str]:
        if not self._is_video_track_y(y) or not (0 <= self._selected < len(self._clips)):
            return -1, ""
        clip = self._clips[self._selected]
        if not clip.is_image:
            return -1, ""
        for index, left, right in self._clip_bounds():
            if index == self._selected:
                return index, self._image_handle_hit(x, left, right)
        return -1, ""

    def _gap_bounds(self) -> list[tuple[float, float, float, float]]:
        intervals = sorted(
            [(max(0.0, clip.start_sec), max(0.0, clip.start_sec) + clip.effective_duration_sec) for clip in self._clips],
            key=lambda item: item[0],
        )
        gaps: list[tuple[float, float, float, float]] = []
        cursor = 0.0
        for start, end in intervals:
            if start - cursor > 0.04:
                gaps.append((cursor, start, 8.0 + cursor * self._px_per_second(), 8.0 + start * self._px_per_second()))
            cursor = max(cursor, end)
        return gaps

    def _index_at_x(self, click_x: float) -> int:
        for idx, left, right in self._clip_bounds():
            if left <= click_x <= right:
                return idx
        return -1

    def _seconds_at_x(self, click_x: float) -> float:
        total = self._timeline_duration()
        if total <= 0:
            return 0.0
        return max(0.0, min(max(total, (self._canvas_width() - 16) / self._px_per_second()), (click_x - 8.0) / self._px_per_second()))

    def seconds_at_content_x(self, x: float) -> float:
        return self._seconds_at_x(x)

    def content_x_for_seconds(self, seconds: float) -> float:
        total = self._timeline_duration()
        return 8.0 + self._px_per_second() * max(0.0, min(total, seconds))

    def zoom_by(self, factor: float) -> None:
        self._zoom = max(0.25, min(8.0, self._zoom * factor))
        self._update_content_width()
        self.updateGeometry()
        self.update()

    def _playhead_x(self) -> float:
        total = self._timeline_duration()
        if total <= 0:
            return 8.0
        return 8.0 + self._px_per_second() * max(0.0, min(total, self._playhead_sec))

    def _hit_playhead(self, x: float, y: float) -> bool:
        return 24 <= y <= 188 and abs(x - self._playhead_x()) <= 8

    def _is_video_track_y(self, y: float) -> bool:
        return 62 <= y <= 118

    def _is_audio_track_y(self, y: float) -> bool:
        return 122 <= y <= 176

    def _insert_index_at_x(self, x: float) -> int:
        bounds = self._clip_bounds()
        if not bounds:
            return 0
        for idx, left, right in bounds:
            if x < (left + right) / 2:
                return idx
        return len(bounds)

    def _drop_x(self, insert_index: int) -> float:
        bounds = self._clip_bounds()
        if not bounds:
            return 8.0
        if insert_index <= 0:
            return bounds[0][1]
        if insert_index >= len(bounds):
            return bounds[-1][2]
        return bounds[insert_index][1]

    def _audio_bounds(self) -> tuple[float, float]:
        start_x = 8.0 + self._audio_start_sec * self._px_per_second()
        end_x = start_x + max(8, int(self._audio_duration_sec * self._px_per_second()))
        return start_x, end_x

    def _gap_at(self, x: float, y: float) -> tuple[float, float] | None:
        if not self._is_video_track_y(y):
            return None
        for start, end, left, right in self._gap_bounds():
            if right - left >= 10 and left <= x <= right:
                return start, end
        return None

    def _gap_delete_rect(self, start: float, end: float) -> QRect:
        left = int(8.0 + start * self._px_per_second())
        right = int(8.0 + end * self._px_per_second())
        cx = max(left + 14, min(right - 14, (left + right) // 2))
        return QRect(cx - 10, 80, 20, 20)

    def _tick_interval(self) -> int:
        px = self._px_per_second()
        for interval in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
            if interval * px >= 70:
                return interval
        return 600

    def _fmt_tick(self, sec: float) -> str:
        sec = max(0, int(sec))
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}" if m else f"{s}s"

    def _timeline_thumbnail_path(self, relpath: str) -> Path | None:
        if self._project_parent is None:
            return None
        path = self._project_parent / relpath
        if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
            return path
        stem = path.stem
        image_dirs = [
            self._project_parent / "images" / "video_production",
            self._project_parent / "images",
            self._project_parent / "video_production",
            path.parent,
        ]
        stems = [stem]
        if stem.endswith("_backup"):
            stems.append(stem.removesuffix("_backup"))
        for image_dir in image_dirs:
            for candidate_stem in stems:
                for suffix in (".png", ".jpg", ".jpeg", ".webp"):
                    candidate = image_dir / f"{candidate_stem}{suffix}"
                    if candidate.is_file():
                        return candidate
        return None

    def _draw_time_ruler(self, painter: QPainter, total: float, dirty: QRect) -> None:
        painter.setPen(QColor("#7d8190"))
        canvas_w = self._canvas_width()
        painter.drawLine(8, 34, max(8, canvas_w - 8), 34)
        interval = self._tick_interval()
        max_time = max(total, max(0.0, (canvas_w - 16) / self._px_per_second()))
        first_time = max(0, int((dirty.left() - 12) / self._px_per_second()))
        t = (first_time // interval) * interval
        last_time = min(max_time, max(0.0, (dirty.right() + 70) / self._px_per_second()))
        while t <= max_time + 0.001:
            if t > last_time:
                break
            x = int(8 + t * self._px_per_second())
            painter.setPen(QPen(QColor("#d6d9e4"), 1))
            painter.drawLine(x, 30, x, 38)
            painter.setPen(QColor("#6a6f7f"))
            painter.drawText(x + 4, 24, self._fmt_tick(t))
            t += interval

    def _draw_clip_thumbnail(self, painter: QPainter, clip: EditorClip, x: int, y: int, w: int, h: int) -> None:
        thumb = self._timeline_thumbnail_path(clip.relpath)
        tile_w = max(24, min(64, h * 16 // 9))
        cache_key = (str(thumb), tile_w, h) if thumb is not None else ("", tile_w, h)
        pix = self._thumbnail_cache.get(cache_key, QPixmap())
        if thumb is not None and cache_key not in self._thumbnail_cache:
            source = QPixmap(str(thumb))
            pix = (
                source.scaled(
                    tile_w,
                    h,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                if not source.isNull()
                else QPixmap()
            )
            self._thumbnail_cache[cache_key] = pix
        if pix.isNull():
            painter.fillRect(x, y, w, h, QColor("#dfe4ee"))
            return
        painter.save()
        painter.setClipRect(x, y, w, h)
        tx = x
        while tx < x + w:
            painter.drawPixmap(tx, y, pix)
            tx += tile_w
        painter.restore()

    def _load_waveform_peaks(self, relpath: str) -> list[float]:
        if self._project_parent is None or not relpath:
            return []
        path = self._project_parent / relpath
        if not path.is_file() or path.suffix.lower() != ".wav":
            return []
        try:
            with wave.open(str(path), "rb") as wf:
                channels = wf.getnchannels()
                width = wf.getsampwidth()
                frames = wf.getnframes()
                if frames <= 0 or width <= 0:
                    return []
                bucket_count = 6000
                frames_per_bucket = max(1, frames // bucket_count)
                peaks: list[float] = []
                max_possible = float((1 << (8 * width - 1)) - 1) if width > 1 else 127.0
                for _ in range(bucket_count):
                    raw = wf.readframes(frames_per_bucket)
                    if not raw:
                        break
                    if channels > 1:
                        raw = audioop.tomono(raw, width, 0.5, 0.5)
                    peak = audioop.rms(raw, width) / max_possible
                    peaks.append(max(0.0, min(1.0, peak)))
                return peaks
        except (wave.Error, OSError, EOFError):
            return []

    def _draw_audio_waveform(self, painter: QPainter, y: int, h: int, dirty: QRect) -> None:
        if not self._audio_relpath or self._audio_duration_sec <= 0:
            return
        x = int(8.0 + self._audio_start_sec * self._px_per_second())
        w = max(8, int(self._audio_duration_sec * self._px_per_second()))
        painter.setPen(QPen(QColor("#8aa2ff"), 1))
        painter.setBrush(QColor("#d7ddff"))
        painter.drawRoundedRect(x, y, w, h, 5, 5)
        painter.setPen(QPen(QColor("#6d83f2"), 1))
        center = y + h // 2
        label_w = min(260, max(92, w // 3))
        wave_x = x + 6
        wave_w = max(1, w - 12)
        if not self._audio_peaks:
            painter.drawLine(wave_x, center, x + w - 8, center)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(215, 221, 255, 235))
            painter.drawRoundedRect(x + 6, y + 5, label_w, h - 10, 4, 4)
            painter.setPen(QColor("#4967d8"))
            painter.drawText(x + 12, y + h // 2 + 5, Path(self._audio_relpath).name)
            return
        usable_w = wave_w
        first_px = max(0, dirty.left() - wave_x - 1)
        last_px = min(usable_w, dirty.right() - wave_x + 2)
        for px in range(first_px, last_px):
            start = int(px * len(self._audio_peaks) / usable_w)
            end = max(start + 1, int((px + 1) * len(self._audio_peaks) / usable_w))
            end = min(len(self._audio_peaks), end)
            amp = max(self._audio_peaks[start:end] or [0.0])
            amp = min(1.0, (amp / self._audio_scale_peak) ** 0.65)
            bar_h = max(1, int((h - 6) * amp))
            painter.drawLine(wave_x + px, center - bar_h // 2, wave_x + px, center + bar_h // 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(215, 221, 255, 235))
        painter.drawRoundedRect(x + 6, y + 5, label_w, h - 10, 4, 4)
        painter.setPen(QColor("#4967d8"))
        painter.drawText(x + 12, y + h // 2 + 5, Path(self._audio_relpath).name)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        dirty = _event.rect()
        painter.fillRect(dirty, QColor("#ffffff"))
        if not self._clips and not self._audio_relpath:
            painter.setPen(QColor("#777a86"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "타임라인에 클립을 추가하세요")
            return
        total = self._timeline_duration()
        if total <= 0:
            return
        self._draw_time_ruler(painter, total, dirty)
        y = 66
        h = 46
        audio_y = 126
        audio_h = 44
        canvas_w = self._canvas_width()
        painter.fillRect(8, y - 4, max(1, canvas_w - 16), h + 10, QColor("#f4f5fb"))
        painter.fillRect(8, audio_y - 4, max(1, canvas_w - 16), audio_h + 10, QColor("#f4f5fb"))
        painter.setPen(QColor("#7d8190"))
        painter.drawText(8, 55, "비디오")
        bounds = self._clip_bounds()
        for gap_start, gap_end, gap_left, gap_right in self._gap_bounds():
            if gap_right - gap_left < 10:
                continue
            if gap_right < dirty.left() or gap_left > dirty.right():
                continue
            hovered = self._hover_gap == (gap_start, gap_end)
            painter.fillRect(int(gap_left), y, int(gap_right - gap_left), h, QColor("#f4f5fb" if not hovered else "#eceef6"))
            if hovered:
                painter.save()
                painter.setClipRect(int(gap_left), y, int(gap_right - gap_left), h)
                painter.setPen(QPen(QColor("#c6cad8"), 1))
                stripe_x = int(gap_left) - 20
                while stripe_x < int(gap_right) + 20:
                    painter.drawLine(stripe_x, y + h, stripe_x + 24, y)
                    stripe_x += 12
                painter.restore()
                painter.setBrush(QColor("#ffffff"))
                painter.setPen(QPen(QColor("#d3d6e2"), 1))
                rect = self._gap_delete_rect(gap_start, gap_end)
                painter.drawEllipse(rect)
                painter.setPen(QPen(QColor("#6d7280"), 2))
                painter.drawLine(rect.center().x() - 4, rect.center().y() - 4, rect.center().x() + 4, rect.center().y() + 4)
                painter.drawLine(rect.center().x() + 4, rect.center().y() - 4, rect.center().x() - 4, rect.center().y() + 4)
        for idx, clip in enumerate(self._clips):
            _i, left, right = bounds[idx]
            if right < dirty.left() or left > dirty.right():
                continue
            x = int(left)
            w = max(8, int(right - left))
            self._draw_clip_thumbnail(painter, clip, x, y, w, h)
            painter.fillRect(x, y, w, h, QColor(255, 255, 255, 45))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#7c2df2") if idx == self._selected else QColor("#ffffff"), 2 if idx == self._selected else 1))
            painter.drawRect(x, y, w, h)
        if 0 <= self._selected < len(self._clips) and self._clips[self._selected].is_image:
            _selected_idx, selected_left, selected_right = bounds[self._selected]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#7c2df2"))
            painter.drawRoundedRect(int(selected_left) - 4, y + 6, 8, h - 12, 3, 3)
            painter.drawRoundedRect(int(selected_right) - 4, y + 6, 8, h - 12, 3, 3)
        if self._drop_index >= 0:
            drop_x = 8.0 + self._drop_seconds * self._px_per_second()
            painter.setPen(QPen(QColor("#7c2df2"), 2))
            painter.drawLine(int(drop_x), y - 8, int(drop_x), y + h + 8)
        painter.setPen(QColor("#7d8190"))
        painter.drawText(8, 122, "오디오")
        self._draw_audio_waveform(painter, audio_y, audio_h, dirty)
        if self._audio_selected and self._audio_relpath:
            audio_left, audio_right = self._audio_bounds()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#7c2df2"), 2))
            painter.drawRoundedRect(int(audio_left), audio_y, int(audio_right - audio_left), audio_h, 5, 5)
        playhead_x = self._playhead_x()
        painter.setPen(QPen(QColor("#7c2df2") if self._playhead_selected else QColor("#222432"), 3 if self._playhead_selected else 2))
        painter.drawLine(int(playhead_x), 28, int(playhead_x), 184)
        painter.setBrush(QColor("#7c2df2") if self._playhead_selected else QColor("#222432"))
        painter.drawEllipse(int(playhead_x) - 5, 25, 10, 10)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._clips and not self._audio_relpath:
            return
        click_x = float(event.position().x())
        click_y = float(event.position().y())
        self._press_x = click_x
        self._press_index = -1
        self._drag_mode = ""
        handle_index, selected_handle = self._selected_image_handle_at(click_x, click_y)
        if handle_index >= 0 and selected_handle:
            clip = self._clips[handle_index]
            self._playhead_selected = False
            self._press_index = handle_index
            self._press_start_sec = clip.start_sec
            self._press_duration_sec = clip.effective_duration_sec
            self._drag_preview_sec = self._press_start_sec
            self._resize_preview_duration = self._press_duration_sec
            self._drag_mode = f"image_{selected_handle}"
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.update()
            return
        gap = self._gap_at(click_x, click_y)
        if gap is not None and self._gap_delete_rect(*gap).contains(event.pos()):
            self.gapDeleted.emit(gap[0], gap[1])
            self.update()
            return
        if self._hit_playhead(click_x, click_y):
            self._playhead_selected = True
            self._drag_mode = "playhead"
            self.update()
            return
        if self._is_video_track_y(click_y):
            idx = self._index_at_x(click_x)
            if idx >= 0:
                self._playhead_selected = False
                self._press_index = idx
                self._press_start_sec = self._clips[idx].start_sec
                self._press_duration_sec = self._clips[idx].effective_duration_sec
                self._drag_preview_sec = self._press_start_sec
                self._resize_preview_duration = self._press_duration_sec
                _bound_idx, left, right = self._clip_bounds()[idx]
                handle = self._image_handle_hit(click_x, left, right) if self._clips[idx].is_image else ""
                self._drag_mode = f"image_{handle}" if handle else "clip"
                self.clipSelected.emit(idx)
                self.update()
                return
        if self._is_audio_track_y(click_y) and self._audio_relpath:
            audio_left, audio_right = self._audio_bounds()
            if not (audio_left <= click_x <= audio_right):
                self._playhead_selected = True
                self._drag_mode = "playhead"
                self.playheadChanged.emit(self._seconds_at_x(click_x))
                self.update()
                return
            self._audio_selected = True
            self._playhead_selected = False
            self._press_start_sec = self._audio_start_sec
            self._drag_preview_sec = self._audio_start_sec
            self._drag_mode = "audio"
            self.audioSelected.emit()
            self.update()
            return
        self._playhead_selected = True
        self._drag_mode = "playhead"
        self.playheadChanged.emit(self._seconds_at_x(click_x))
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        move_x = float(event.position().x())
        move_y = float(event.position().y())
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            _handle_index, handle = self._selected_image_handle_at(move_x, move_y)
            self.setCursor(Qt.CursorShape.SizeHorCursor if handle else Qt.CursorShape.ArrowCursor)
        if self._drag_mode == "playhead":
            self.playheadChanged.emit(self._seconds_at_x(move_x))
            return
        if self._drag_mode == "clip" and self._press_index >= 0:
            delta_sec = (move_x - self._press_x) / self._px_per_second()
            self._drag_preview_sec = max(0.0, self._press_start_sec + delta_sec)
            self._drop_index = self._press_index
            self.update()
            return
        if self._drag_mode == "image_left" and self._press_index >= 0:
            original_end = self._press_start_sec + self._press_duration_sec
            candidate_start = max(0.0, self._press_start_sec + (move_x - self._press_x) / self._px_per_second())
            previous_ends = [
                clip.start_sec + clip.effective_duration_sec
                for index, clip in enumerate(self._clips)
                if index != self._press_index
                and clip.start_sec + clip.effective_duration_sec <= self._press_start_sec + 0.001
            ]
            if previous_ends:
                candidate_start = max(candidate_start, max(previous_ends))
            candidate_start = min(candidate_start, original_end - 0.10)
            self._drag_preview_sec = candidate_start
            self._resize_preview_duration = max(0.10, original_end - candidate_start)
            self.update()
            return
        if self._drag_mode == "image_right" and self._press_index >= 0:
            delta_sec = (move_x - self._press_x) / self._px_per_second()
            self._drag_preview_sec = self._press_start_sec
            self._resize_preview_duration = max(0.10, self._press_duration_sec + delta_sec)
            self.update()
            return
        if self._drag_mode == "audio" and self._audio_relpath:
            delta_sec = (move_x - self._press_x) / self._px_per_second()
            self._audio_start_sec = max(0.0, self._press_start_sec + delta_sec)
            self.update()
            return
        hover_gap = self._gap_at(move_x, move_y)
        if hover_gap != self._hover_gap:
            self._hover_gap = hover_gap
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode == "playhead":
            self.playheadChanged.emit(self._seconds_at_x(float(event.position().x())))
            self._drag_mode = ""
            return
        if self._drag_mode == "audio":
            delta_sec = (float(event.position().x()) - self._press_x) / self._px_per_second()
            self.audioMoved.emit(max(0.0, self._press_start_sec + delta_sec))
            self._drag_mode = ""
            self._drag_preview_sec = None
            self._resize_preview_duration = None
            return
        if self._drag_mode in {"image_left", "image_right"} and self._press_index >= 0:
            start_sec = (
                self._drag_preview_sec
                if self._drag_preview_sec is not None
                else self._press_start_sec
            )
            duration_sec = (
                self._resize_preview_duration
                if self._resize_preview_duration is not None
                else self._press_duration_sec
            )
            self.imageResized.emit(self._press_index, start_sec, duration_sec)
            self._press_index = -1
            self._drag_preview_sec = None
            self._resize_preview_duration = None
            self._drag_mode = ""
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
            return
        if self._drag_mode != "clip" or self._press_index < 0:
            self._drag_mode = ""
            return
        release_x = float(event.position().x())
        target_start = max(0.0, self._press_start_sec + (release_x - self._press_x) / self._px_per_second())
        moved_far = abs(release_x - self._press_x) > 12
        if moved_far:
            self.clipMoved.emit(self._press_index, target_start)
        self._press_index = -1
        self._drop_index = -1
        self._drag_preview_sec = None
        self._resize_preview_duration = None
        self._drag_mode = ""
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(MEDIA_MIME_TYPE):
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if not event.mimeData().hasFormat(MEDIA_MIME_TYPE):
            return
        self._drop_index = 0
        self._drop_seconds = self._seconds_at_x(float(event.position().x()))
        self.update()
        event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        if not event.mimeData().hasFormat(MEDIA_MIME_TYPE):
            return
        rels = _decode_media_rels(bytes(event.mimeData().data(MEDIA_MIME_TYPE)))
        self.mediaDropped.emit(rels, self._seconds_at_x(float(event.position().x())))
        self._drop_index = -1
        self.update()
        event.acceptProposedAction()

    def dragLeaveEvent(self, _event) -> None:
        self._drop_index = -1
        self.update()

    def leaveEvent(self, _event) -> None:
        self._hover_gap = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.08 if event.angleDelta().y() > 0 else 1 / 1.08
            self.zoom_by(factor)
            event.accept()
            return
        super().wheelEvent(event)


class TimelineScrollArea(QScrollArea):
    def __init__(self, timeline: TimelineStrip, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._timeline = timeline

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            bar = self.horizontalScrollBar()
            viewport_x = float(event.position().x())
            content_x = float(bar.value()) + viewport_x
            anchor_sec = self._timeline.seconds_at_content_x(content_x)
            factor = 1.08 if event.angleDelta().y() > 0 else 1 / 1.08
            self._timeline.zoom_by(factor)
            new_content_x = self._timeline.content_x_for_seconds(anchor_sec)
            bar.setValue(max(0, int(round(new_content_x - viewport_x))))
            event.accept()
            return
        super().wheelEvent(event)


class VideoEditorPanel(QWidget):
    _SUBTITLE_PLAYBACK_LEAD_SEC = 0.15

    stateChanged = Signal()

    def __init__(self, project_parent_getter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project_parent_getter = project_parent_getter
        self._project: StoryProject | None = None
        self._clips: list[EditorClip] = []
        self._preview_row = -1
        self._preview_seek_sec = 0.0
        self._preview_position_started = False
        self._preview_transitioning = False
        self._preview_load_token = 0
        self._timeline_preview_mode = False
        self._timeline_preview_signature = ""
        self._timeline_preview_path: Path | None = None
        self._timeline_engine_playing = False
        self._audio_tail_playing = False
        self._ignore_engine_position_updates = False
        self._user_pause_requested = False
        self._audio_relpath = ""
        self._audio_duration_sec = 0.0
        self._audio_start_sec = 0.0
        self._audio_selected = False
        self._audio_track_explicit = False
        self._subtitle_relpath = ""
        self._subtitle_cues: list[tuple[float, float, str]] = []
        self._subtitle_visible = False
        self._audio_clock_sec = 0.0
        self._playback_completed = False
        self._playhead_sec = 0.0
        self._duration_cache: dict[str, float] = {}
        self._inspector_expanded = False

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._timeline_audio = TimelineAudioRenderer(self)
        self._timeline_engine = TimelinePlaybackEngine(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        self._btn_refresh = QPushButton("미디어 새로고침")
        self._btn_add_selected = QPushButton("선택 추가")
        self._btn_add_all = QPushButton("모든 클립 추가")
        self._btn_remove = QPushButton("삭제")
        self._btn_up = QPushButton("위로")
        self._btn_down = QPushButton("아래로")
        self._btn_preview = QPushButton("선택 미리보기")
        self._btn_export = QPushButton("타임라인 내보내기")
        self._btn_export.setObjectName("primaryExportButton")
        for btn in (
            self._btn_refresh,
            self._btn_add_selected,
            self._btn_add_all,
            self._btn_remove,
            self._btn_up,
            self._btn_down,
            self._btn_preview,
        ):
            top.addWidget(btn)
        top.addStretch(1)
        top.addWidget(self._btn_export)
        root.addLayout(top)

        splitter = QSplitter()
        left = QWidget()
        left.setObjectName("mediaPanel")
        left.setFixedWidth(270)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(12, 12, 12, 12)
        left_lay.setSpacing(8)
        left_lay.addWidget(QLabel("내 미디어"))
        self._label_media_selection = QLabel("0 선택됨")
        left_lay.addWidget(self._label_media_selection)
        self._media_list = MediaListWidget()
        self._media_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._media_list.setDragEnabled(True)
        self._media_list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self._media_list.setDefaultDropAction(Qt.DropAction.CopyAction)
        self._media_list.setViewMode(QListView.ViewMode.IconMode)
        self._media_list.setResizeMode(QListView.ResizeMode.Adjust)
        self._media_list.setMovement(QListView.Movement.Static)
        self._media_list.setIconSize(MEDIA_THUMB_SIZE)
        self._media_list.setGridSize(MEDIA_CARD_SIZE)
        self._media_list.setSpacing(8)
        self._media_list.setUniformItemSizes(True)
        self._media_list.setItemDelegate(MediaCardDelegate(self._media_list))
        self._media_list.setSelectionRectVisible(False)
        left_lay.addWidget(self._media_list, stretch=1)

        center = QWidget()
        center.setObjectName("timelinePanel")
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(12, 10, 12, 12)
        center_lay.setSpacing(8)
        self._timeline = QTableWidget(0, 5)
        self._timeline.setHorizontalHeaderLabels(["#", "파일", "길이", "Trim In", "Trim Out"])
        self._timeline.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._timeline.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._timeline.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._timeline.setVisible(False)
        self._timeline_strip = TimelineStrip()
        self._timeline_scroll = TimelineScrollArea(self._timeline_strip)
        self._timeline_scroll.setWidgetResizable(True)
        self._timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._timeline_scroll.setWidget(self._timeline_strip)
        self._timeline_scroll.setMinimumHeight(260)
        center_lay.addWidget(self._timeline_scroll)
        center_lay.addWidget(self._timeline)

        right = QWidget()
        right.setObjectName("previewPanel")
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(12, 12, 12, 8)
        right_lay.setSpacing(8)
        self._preview_stack = QStackedWidget()
        self._video = QVideoWidget()
        self._video.setMinimumHeight(240)
        self._player.setVideoOutput(self._video)
        self._frame_label = QLabel()
        self._frame_label.setMinimumHeight(240)
        self._frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._frame_label.setStyleSheet("background: #000000;")
        self._preview_stack.addWidget(self._video)
        self._preview_stack.addWidget(self._frame_label)
        self._subtitle_overlay = QLabel(self._preview_stack)
        self._subtitle_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle_overlay.setWordWrap(True)
        self._subtitle_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._subtitle_overlay.setStyleSheet(
            "background: rgba(0, 0, 0, 175); color: white; padding: 7px 14px;"
            "border-radius: 4px; font-size: 18px; font-weight: 600;"
        )
        self._subtitle_overlay.hide()
        self._preview_stack.installEventFilter(self)
        right_lay.addWidget(self._preview_stack, stretch=1)
        preview_controls = QHBoxLayout()
        preview_controls.setContentsMargins(0, 4, 0, 2)
        preview_controls.setSpacing(8)
        self._btn_to_start = PlayerControlButton("start")
        self._btn_to_start.setToolTip("처음으로")
        self._btn_back5 = PlayerControlButton("back5")
        self._btn_back5.setToolTip("5초 뒤로")
        self._btn_forward5 = PlayerControlButton("forward5")
        self._btn_forward5.setToolTip("5초 앞으로")
        self._btn_play = PlayerControlButton("play")
        self._btn_play.setToolTip("재생")
        self._label_time = QLabel("0:00 / 0:00")
        self._label_time.setObjectName("playerTimeLabel")
        preview_controls.addStretch(1)
        preview_controls.addWidget(self._btn_to_start)
        preview_controls.addWidget(self._btn_back5)
        preview_controls.addWidget(self._btn_forward5)
        preview_controls.addWidget(self._btn_play)
        preview_controls.addWidget(self._label_time)
        preview_controls.addStretch(1)
        right_lay.addLayout(preview_controls)
        self._label_status = QLabel("준비됨")
        self._label_status.setWordWrap(True)
        right_lay.addWidget(self._label_status)

        inspector = QWidget()
        inspector.setObjectName("inspectorPanel")
        inspector.setFixedWidth(82)
        inspector_lay = QHBoxLayout(inspector)
        inspector_lay.setContentsMargins(0, 0, 0, 0)
        inspector_lay.setSpacing(0)
        inspector_rail = QWidget()
        inspector_rail.setObjectName("inspectorRail")
        rail_lay = QVBoxLayout(inspector_rail)
        rail_lay.setContentsMargins(8, 10, 8, 10)
        rail_lay.setSpacing(10)
        self._btn_subtitles = QPushButton("자막")
        self._btn_subtitles.setObjectName("railButton")
        self._btn_subtitles.setFixedSize(58, 58)
        rail_lay.addWidget(self._btn_subtitles)
        audio_button = QPushButton("오디오")
        audio_button.setObjectName("railButton")
        audio_button.setFixedSize(58, 58)
        rail_lay.addWidget(audio_button)
        self._btn_toggle_inspector = QPushButton("페이드")
        self._btn_toggle_inspector.setObjectName("railButton")
        self._btn_toggle_inspector.setProperty("active", True)
        self._btn_toggle_inspector.setFixedSize(58, 58)
        rail_lay.addWidget(self._btn_toggle_inspector)
        for label in ("필터", "효과", "색상", "속도"):
            btn = QPushButton(label)
            btn.setObjectName("railButton")
            btn.setFixedSize(58, 58)
            rail_lay.addWidget(btn)
        rail_lay.addStretch(1)
        self._inspector_body = QWidget()
        self._inspector_body.setObjectName("inspectorBody")
        body_lay = QVBoxLayout(self._inspector_body)
        body_lay.setContentsMargins(24, 22, 24, 22)
        body_lay.setSpacing(22)
        body_header = QHBoxLayout()
        self._inspector_title = QLabel("페이드")
        self._inspector_title.setObjectName("inspectorTitle")
        body_header.addWidget(self._inspector_title)
        self._inspector_badge = QLabel("클립 1")
        self._inspector_badge.setObjectName("inspectorBadge")
        body_header.addWidget(self._inspector_badge)
        body_header.addStretch(1)
        self._btn_close_inspector = QPushButton(">")
        self._btn_close_inspector.setObjectName("inspectorCloseButton")
        self._btn_close_inspector.setFixedSize(28, 28)
        body_header.addWidget(self._btn_close_inspector)
        body_lay.addLayout(body_header)
        self._inspector_pages = QStackedWidget()
        clip_page = QWidget()
        clip_page_lay = QVBoxLayout(clip_page)
        clip_page_lay.setContentsMargins(0, 0, 0, 0)
        clip_page_lay.setSpacing(18)
        trim_form = QFormLayout()
        trim_form.setVerticalSpacing(18)
        trim_form.setHorizontalSpacing(14)
        self._spin_trim_in = QDoubleSpinBox()
        self._spin_trim_in.setRange(0.0, 9999.0)
        self._spin_trim_in.setDecimals(2)
        self._spin_trim_in.setSingleStep(0.25)
        self._spin_trim_in.setSuffix(" s")
        self._spin_trim_out = QDoubleSpinBox()
        self._spin_trim_out.setRange(0.0, 9999.0)
        self._spin_trim_out.setDecimals(2)
        self._spin_trim_out.setSingleStep(0.25)
        self._spin_trim_out.setSuffix(" s")
        self._btn_apply_trim = QPushButton("적용")
        self._trim_in_label = QLabel("페이드 인")
        self._trim_in_label.setObjectName("inspectorSectionLabel")
        self._trim_out_label = QLabel("페이드 아웃")
        self._trim_out_label.setObjectName("inspectorSectionLabel")
        self._image_duration_label = QLabel("재생 시간")
        self._image_duration_label.setObjectName("inspectorSectionLabel")
        self._spin_image_duration = QDoubleSpinBox()
        self._spin_image_duration.setRange(0.10, 3600.0)
        self._spin_image_duration.setDecimals(2)
        self._spin_image_duration.setSingleStep(0.5)
        self._spin_image_duration.setSuffix(" s")
        trim_form.addRow(self._trim_in_label, self._spin_trim_in)
        trim_form.addRow(self._trim_out_label, self._spin_trim_out)
        trim_form.addRow(self._image_duration_label, self._spin_image_duration)
        self._image_duration_label.setVisible(False)
        self._spin_image_duration.setVisible(False)
        clip_page_lay.addLayout(trim_form)
        clip_page_lay.addWidget(self._btn_apply_trim)
        clip_page_lay.addStretch(1)
        self._inspector_pages.addWidget(clip_page)

        subtitle_page = QWidget()
        subtitle_lay = QVBoxLayout(subtitle_page)
        subtitle_lay.setContentsMargins(0, 0, 0, 0)
        subtitle_lay.setSpacing(10)
        self._check_show_subtitles = QCheckBox("영상에 자막 표시")
        subtitle_lay.addWidget(self._check_show_subtitles)
        self._label_subtitle_path = QLabel("자막 파일 없음")
        self._label_subtitle_path.setWordWrap(True)
        self._label_subtitle_path.setStyleSheet("color: #6a6f7f;")
        subtitle_lay.addWidget(self._label_subtitle_path)
        self._subtitle_table = QTableWidget(0, 2)
        self._subtitle_table.setHorizontalHeaderLabels(["시간", "대사"])
        self._subtitle_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._subtitle_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._subtitle_table.setWordWrap(True)
        self._subtitle_table.verticalHeader().setVisible(False)
        self._subtitle_table.horizontalHeader().setStretchLastSection(True)
        self._subtitle_table.setColumnWidth(0, 112)
        subtitle_lay.addWidget(self._subtitle_table, stretch=1)
        subtitle_buttons = QHBoxLayout()
        self._btn_reload_subtitles = QPushButton("다시 불러오기")
        self._btn_save_subtitles = QPushButton("자막 저장")
        self._btn_save_subtitles.setObjectName("primaryExportButton")
        subtitle_buttons.addWidget(self._btn_reload_subtitles)
        subtitle_buttons.addWidget(self._btn_save_subtitles)
        subtitle_lay.addLayout(subtitle_buttons)
        self._inspector_pages.addWidget(subtitle_page)
        body_lay.addWidget(self._inspector_pages, stretch=1)
        self._inspector_body.setVisible(False)
        inspector_lay.addWidget(self._inspector_body)
        inspector_lay.addWidget(inspector_rail)
        self._inspector_panel = inspector

        splitter.addWidget(left)
        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(right)
        vertical.addWidget(center)
        vertical.setStretchFactor(0, 2)
        vertical.setStretchFactor(1, 2)
        vertical.setSizes([520, 320])
        main_splitter = QSplitter()
        main_splitter.addWidget(vertical)
        main_splitter.addWidget(inspector)
        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 0)
        main_splitter.setSizes([1360, 82])
        splitter.addWidget(main_splitter)
        splitter.setSizes([270, 1560])
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)
        self.setStyleSheet(
            """
            VideoEditorPanel {
                background: #f7f7fb;
            }
            QWidget#mediaPanel,
            QWidget#previewPanel,
            QWidget#timelinePanel,
            QWidget#inspectorPanel {
                background: #f7f7fb;
                border: 0;
            }
            QScrollArea {
                background: #ffffff;
                border: 0;
            }
            QScrollArea > QWidget > QWidget {
                background: #ffffff;
            }
            QWidget#inspectorRail {
                background: #f0f1f6;
            }
            QWidget#inspectorBody {
                background: #fbfbff;
                border-left: 1px solid #e3e5ef;
            }
            QLabel#inspectorTitle {
                color: #242538;
                font-size: 20px;
                font-weight: 700;
            }
            QLabel#inspectorBadge {
                background: #ebeaf2;
                color: #4d5267;
                border-radius: 6px;
                padding: 4px 10px;
                font-weight: 600;
            }
            QLabel#inspectorSectionLabel {
                color: #2d3042;
                font-size: 16px;
                font-weight: 700;
            }
            QListWidget {
                background: transparent;
                border: 0;
                outline: 0;
            }
            QListWidget::item {
                padding: 4px;
                border-radius: 6px;
            }
            QListWidget::item:selected {
                background: #ece2ff;
                color: #1f2430;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #dfe2eb;
                border-radius: 4px;
                padding: 5px 12px;
            }
            QPushButton:hover {
                background: #f3f4f8;
            }
            QLabel#playerTimeLabel {
                color: #252736;
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0;
            }
            QPushButton#primaryExportButton {
                background: #7c2df2;
                color: #ffffff;
                border-color: #7c2df2;
                font-weight: 600;
            }
            QPushButton#railButton {
                background: transparent;
                border: 0;
                border-radius: 12px;
                color: #686d82;
                padding: 4px;
                font-size: 12px;
            }
            QPushButton#railButton:hover {
                background: #ffffff;
            }
            QPushButton#railButton[active="true"] {
                background: #ffffff;
                color: #2a2c3d;
                font-weight: 700;
            }
            QPushButton#inspectorCloseButton {
                background: transparent;
                border: 1px solid #d6d9e4;
                border-radius: 4px;
                padding: 0;
                font-weight: 600;
            }
            QDoubleSpinBox {
                background: #ffffff;
                border: 1px solid #d3d6e2;
                border-radius: 5px;
                padding: 6px 10px;
                min-height: 24px;
            }
            QTableWidget {
                background: #ffffff;
                border: 1px solid #dfe2eb;
                border-radius: 5px;
                gridline-color: #eceef4;
                selection-background-color: #eee5ff;
                selection-color: #202230;
            }
            QTableWidget::item {
                padding: 7px;
            }
            """
        )

        self._btn_refresh.clicked.connect(self.refresh_media)
        self._btn_add_selected.clicked.connect(self._add_selected_media)
        self._btn_add_all.clicked.connect(self._add_all_media)
        self._btn_remove.clicked.connect(self._remove_selected_clip)
        self._btn_up.clicked.connect(lambda: self._move_selected(-1))
        self._btn_down.clicked.connect(lambda: self._move_selected(1))
        self._btn_preview.clicked.connect(self._preview_selected)
        self._btn_play.clicked.connect(self._toggle_playback)
        self._btn_to_start.clicked.connect(self._seek_to_start)
        self._btn_back5.clicked.connect(lambda: self._seek_relative_seconds(-5.0))
        self._btn_forward5.clicked.connect(lambda: self._seek_relative_seconds(5.0))
        self._btn_export.clicked.connect(self._export_timeline)
        self._btn_apply_trim.clicked.connect(self._apply_trim_to_selected)
        self._btn_toggle_inspector.clicked.connect(self._toggle_inspector)
        self._btn_subtitles.clicked.connect(self._show_subtitle_inspector)
        self._btn_close_inspector.clicked.connect(lambda: self._set_inspector_expanded(False))
        self._check_show_subtitles.toggled.connect(self._set_subtitle_visibility)
        self._btn_reload_subtitles.clicked.connect(self._load_subtitles)
        self._btn_save_subtitles.clicked.connect(self._save_subtitles)
        self._subtitle_table.cellDoubleClicked.connect(self._seek_to_subtitle_row)
        self._subtitle_table.itemChanged.connect(self._on_subtitle_item_changed)
        self._media_list.itemDoubleClicked.connect(lambda _item: self._add_selected_media())
        self._media_list.addRequested.connect(lambda rel: self._insert_media_relpaths([rel], self._timeline_total_seconds()))
        self._media_list.checkedCountChanged.connect(self._update_media_selection_label)
        self._media_list.itemSelectionChanged.connect(self._update_media_selection_label)
        self._timeline.itemDoubleClicked.connect(lambda _item: self._preview_selected())
        self._timeline.itemSelectionChanged.connect(self._load_selected_trim_controls)
        self._timeline_strip.clipSelected.connect(self._select_timeline_row)
        self._timeline_strip.audioSelected.connect(self._select_audio_track)
        self._timeline_strip.clipMoved.connect(self._move_clip_to)
        self._timeline_strip.imageResized.connect(self._resize_image_clip)
        self._timeline_strip.audioMoved.connect(self._move_audio_to)
        self._timeline_strip.gapDeleted.connect(self._delete_gap)
        self._timeline_strip.playheadChanged.connect(self._seek_timeline_seconds)
        self._timeline_strip.mediaDropped.connect(self._insert_media_relpaths)
        self._player.positionChanged.connect(self._on_player_position_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._timeline_engine.frameReady.connect(self._on_timeline_frame_ready)
        self._timeline_engine.positionChanged.connect(self._on_timeline_engine_position_changed)
        self._timeline_engine.playbackFinished.connect(self._on_timeline_engine_finished)
        self._timeline_engine.playbackStateChanged.connect(self._on_timeline_engine_state_changed)
        self._timeline_engine.errorOccurred.connect(self._on_timeline_engine_error)
        self._timeline_audio.positionChanged.connect(self._on_timeline_audio_position_changed)
        self._timeline_audio.playbackFinished.connect(self._on_timeline_audio_finished)
        self._timeline_audio.playbackStateChanged.connect(self._on_timeline_audio_state_changed)
        self._timeline_audio.errorOccurred.connect(self._on_timeline_audio_error)

    def set_project(self, project: StoryProject, project_parent: Path | None) -> None:
        self._project = project
        self._timeline_strip.set_project_parent(project_parent)
        self._load_state()
        self.refresh_media()
        self._refresh_track_labels()
        self._load_subtitles()

    def apply_to_project(self, _project: StoryProject) -> None:
        self._save_state()

    def _project_parent(self) -> Path | None:
        try:
            parent = self._project_parent_getter()
        except TypeError:
            parent = self._project_parent_getter
        return Path(parent).resolve() if parent else None

    def refresh_media(self) -> None:
        parent = self._project_parent()
        self._media_list.clear()
        if parent is None:
            return
        media: list[Path] = []
        for folder in ("video_clips", "export"):
            base = parent / folder
            if base.is_dir():
                media.extend(sorted(base.glob("*.mp4")))
        for folder in ("audio", "export"):
            base = parent / folder
            if base.is_dir():
                media.extend(sorted(base.glob("*.wav")))
        for folder in ("images/video_production", "images", "video_production"):
            base = parent / folder
            if base.is_dir():
                media.extend(
                    sorted(path for path in base.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)
                )
        seen: set[str] = set()
        for path in media:
            rel = _relative(path, parent)
            if rel in seen:
                continue
            seen.add(rel)
            suffix = path.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                dur = DEFAULT_IMAGE_DURATION_SEC
            elif suffix in {".wav", ".mp4"}:
                dur = self._duration(path)
            else:
                dur = self._known_duration(rel)
            thumb_path = self._thumbnail_path(path, parent)
            thumb = self._media_thumbnail(path, parent, thumb_path=thumb_path)
            item = QListWidgetItem(QIcon(thumb), path.name)
            item.setSizeHint(MEDIA_CARD_SIZE)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, rel)
            item.setData(Qt.ItemDataRole.UserRole + 2, _fmt_badge_duration(dur) if dur > 0 else "")
            item.setToolTip(f"{rel}\n{_fmt_duration(dur) if dur > 0 else '길이 미확인'}")
            self._media_list.addItem(item)
        self._label_status.setText(f"미디어 {self._media_list.count()}개")
        self._update_media_selection_label()
        self._refresh_track_labels()

    def _update_media_selection_label(self, _count: int | None = None) -> None:
        checked = 0
        for idx in range(self._media_list.count()):
            item = self._media_list.item(idx)
            if item is not None and _is_checked_state(item.checkState()):
                checked += 1
        selected = len(self._media_list.selectedItems())
        count = checked if checked > 0 else selected
        self._label_media_selection.setText(f"{count} 선택됨")
        any_selected = count > 0
        for idx in range(self._media_list.count()):
            item = self._media_list.item(idx)
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole + 4, any_selected)
        self._media_list.viewport().update()

    def _media_thumbnail(self, path: Path, parent: Path, *, thumb_path: Path | None = None) -> QPixmap:
        size = MEDIA_THUMB_SIZE
        thumb = path if path.suffix.lower() in IMAGE_SUFFIXES else (
            thumb_path if thumb_path is not None else self._thumbnail_path(path, parent)
        )
        source = QPixmap(str(thumb)) if thumb is not None else QPixmap()
        canvas = QPixmap(size)
        canvas.fill(QColor("#dfe4ee"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not source.isNull():
            scaled = source.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            x = int((size.width() - scaled.width()) / 2)
            y = int((size.height() - scaled.height()) / 2)
            painter.drawPixmap(x, y, scaled)
        else:
            painter.fillRect(0, 0, size.width(), size.height(), QColor("#eef1f7"))
            painter.setPen(QColor("#6f7482"))
            label = "WAV" if path.suffix.lower() == ".wav" else "MP4"
            painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, label)
            if path.suffix.lower() == ".wav":
                painter.setPen(QPen(QColor("#6d83f2"), 2))
                mid = size.height() // 2
                for i, x in enumerate(range(10, size.width() - 8, 7)):
                    amp = 6 + (i % 5) * 3
                    painter.drawLine(x, mid - amp, x, mid + amp)
        painter.end()
        return canvas

    def _thumbnail_path(self, path: Path, parent: Path) -> Path | None:
        if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
            return path
        stem = path.stem
        image_dirs = [
            parent / "images" / "video_production",
            parent / "images",
            parent / "video_production",
            path.parent,
        ]
        stems = [stem]
        if stem.endswith("_backup"):
            stems.append(stem.removesuffix("_backup"))
        for image_dir in image_dirs:
            for candidate_stem in stems:
                for suffix in (".png", ".jpg", ".jpeg", ".webp"):
                    candidate = image_dir / f"{candidate_stem}{suffix}"
                    if candidate.is_file():
                        return candidate
        return None

    def _state_path(self) -> Path | None:
        parent = self._project_parent()
        if parent is None:
            return None
        return parent / "video_editor_state.json"

    def _load_state(self) -> None:
        path = self._state_path()
        self._clips = []
        self._audio_relpath = ""
        self._audio_start_sec = 0.0
        self._audio_track_explicit = False
        self._subtitle_relpath = ""
        self._subtitle_visible = False
        if path is None or not path.is_file():
            self._sync_timeline()
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_clips = data.get("clips", [])
            if isinstance(raw_clips, list):
                has_start_times = any(isinstance(x, dict) and "start_sec" in x for x in raw_clips)
                self._clips = [
                    clip
                    for clip in (EditorClip.from_dict(x) for x in raw_clips if isinstance(x, dict))
                    if clip.relpath
                ]
                for clip in self._clips:
                    if clip.is_image and clip.duration_sec <= 0.04:
                        clip.duration_sec = DEFAULT_IMAGE_DURATION_SEC
                        clip.trim_in_sec = 0.0
                        clip.trim_out_sec = 0.0
                collapsed_legacy_positions = (
                    len(self._clips) > 1
                    and max((clip.start_sec for clip in self._clips), default=0.0) <= 0.001
                )
                if not has_start_times or collapsed_legacy_positions:
                    start = 0.0
                    for clip in self._clips:
                        clip.start_sec = start
                        start += clip.effective_duration_sec
            if "audio_relpath" in data:
                self._audio_track_explicit = True
                self._audio_relpath = str(data.get("audio_relpath", "") or "")
                self._audio_start_sec = max(0.0, float(data.get("audio_start_sec", 0.0) or 0.0))
            self._subtitle_relpath = str(data.get("subtitle_relpath", "") or "")
            self._subtitle_visible = bool(data.get("subtitle_visible", False))
        except Exception:
            self._clips = []
            self._audio_relpath = ""
            self._audio_start_sec = 0.0
            self._audio_track_explicit = False
            self._subtitle_relpath = ""
            self._subtitle_visible = False
        self._duration_cache.update(
            {clip.relpath: clip.duration_sec for clip in self._clips if clip.relpath and clip.duration_sec > 0}
        )
        self._sync_timeline()

    def _save_state(self) -> None:
        path = self._state_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "clips": [clip.to_dict() for clip in self._clips],
            "audio_relpath": self._audio_relpath,
            "audio_start_sec": self._audio_start_sec,
            "subtitle_relpath": self._subtitle_relpath,
            "subtitle_visible": self._subtitle_visible,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _first_existing_relpath(self, relpaths: list[str]) -> str:
        parent = self._project_parent()
        if parent is None:
            return ""
        for rel in relpaths:
            if (parent / rel).is_file():
                return rel
        return ""

    def _find_subtitle_relpath(self) -> str:
        parent = self._project_parent()
        if parent is None:
            return ""
        if self._subtitle_relpath and (parent / self._subtitle_relpath).is_file():
            return self._subtitle_relpath
        preferred = self._first_existing_relpath(
            [
                "subs/video_production_narration.srt",
                "subs/merged.srt",
                "subs/wavseq_merged.srt",
            ]
        )
        if preferred:
            return preferred
        subs_dir = parent / "subs"
        if subs_dir.is_dir():
            candidates = sorted(subs_dir.glob("*.srt"))
            if candidates:
                return _relative(candidates[0], parent)
        return ""

    def _load_subtitles(self) -> None:
        parent = self._project_parent()
        self._subtitle_relpath = self._find_subtitle_relpath()
        path = (parent / self._subtitle_relpath).resolve() if parent is not None and self._subtitle_relpath else None
        self._subtitle_cues = parse_srt_file(path) if path is not None else []
        self._subtitle_table.blockSignals(True)
        try:
            self._subtitle_table.setRowCount(0)
            for start, end, text in self._subtitle_cues:
                row = self._subtitle_table.rowCount()
                self._subtitle_table.insertRow(row)
                time_item = QTableWidgetItem(
                    f"{_fmt_badge_duration(start)}\n{_fmt_badge_duration(end)}"
                )
                time_item.setData(Qt.ItemDataRole.UserRole, start)
                time_item.setData(Qt.ItemDataRole.UserRole + 1, end)
                time_item.setFlags(time_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                text_item = QTableWidgetItem(text)
                self._subtitle_table.setItem(row, 0, time_item)
                self._subtitle_table.setItem(row, 1, text_item)
                self._subtitle_table.resizeRowToContents(row)
        finally:
            self._subtitle_table.blockSignals(False)
        self._label_subtitle_path.setText(
            self._subtitle_relpath if self._subtitle_relpath else "자막 파일 없음"
        )
        if self._inspector_pages.currentIndex() == 1:
            self._inspector_badge.setText(str(len(self._subtitle_cues)))
        self._check_show_subtitles.blockSignals(True)
        self._check_show_subtitles.setChecked(self._subtitle_visible)
        self._check_show_subtitles.blockSignals(False)
        self._update_subtitle_overlay()

    def _subtitle_cues_from_table(self) -> list[tuple[float, float, str]]:
        cues: list[tuple[float, float, str]] = []
        for row in range(self._subtitle_table.rowCount()):
            time_item = self._subtitle_table.item(row, 0)
            text_item = self._subtitle_table.item(row, 1)
            if time_item is None or text_item is None:
                continue
            start = float(time_item.data(Qt.ItemDataRole.UserRole) or 0.0)
            end = float(time_item.data(Qt.ItemDataRole.UserRole + 1) or 0.0)
            text = text_item.text().strip()
            if end > start and text:
                cues.append((start, end, text))
        return cues

    def _on_subtitle_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 1:
            return
        self._subtitle_table.resizeRowToContents(item.row())
        cues = self._subtitle_cues_from_table()
        self._subtitle_cues = cues
        if self._inspector_pages.currentIndex() == 1:
            self._inspector_badge.setText(str(len(cues)))
        self._update_subtitle_overlay()

    def _save_subtitles(self) -> None:
        parent = self._project_parent()
        if parent is None:
            return
        if not self._subtitle_relpath:
            self._subtitle_relpath = "subs/video_editor_subtitles.srt"
        cues = self._subtitle_cues_from_table()
        blocks: list[str] = []
        for index, (start, end, text) in enumerate(cues, start=1):
            blocks.extend(
                [
                    str(index),
                    f"{seconds_to_srt_timestamp(start)} --> {seconds_to_srt_timestamp(end)}",
                    text,
                    "",
                ]
            )
        path = (parent / self._subtitle_relpath).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")
        self._subtitle_cues = cues
        self._label_subtitle_path.setText(self._subtitle_relpath)
        self._inspector_badge.setText(str(len(cues)))
        self._label_status.setText(f"자막 저장: {self._subtitle_relpath}")
        self._update_subtitle_overlay()
        self.stateChanged.emit()

    def _seek_to_subtitle_row(self, row: int, column: int) -> None:
        if column == 1:
            return
        item = self._subtitle_table.item(row, 0)
        if item is not None:
            self._seek_timeline_seconds(float(item.data(Qt.ItemDataRole.UserRole) or 0.0))

    def _set_subtitle_visibility(self, visible: bool) -> None:
        self._subtitle_visible = bool(visible)
        self._update_subtitle_overlay()
        self.stateChanged.emit()

    def _subtitle_text_at(self, seconds: float) -> str:
        for start, end, text in self._subtitle_cues:
            if start <= seconds < end:
                return text
        return ""

    def _update_subtitle_overlay_geometry(self) -> None:
        width = max(180, int(self._preview_stack.width() * 0.76))
        self._subtitle_overlay.setFixedWidth(width)
        self._subtitle_overlay.adjustSize()
        height = min(120, max(42, self._subtitle_overlay.sizeHint().height()))
        x = max(0, (self._preview_stack.width() - width) // 2)
        y = max(0, self._preview_stack.height() - height - 28)
        self._subtitle_overlay.setGeometry(x, y, width, height)
        self._subtitle_overlay.raise_()

    def _update_subtitle_overlay(self) -> None:
        subtitle_sec = self._playhead_sec
        if self._timeline_audio.is_playing():
            subtitle_sec = self._audio_clock_sec
        elif (
            self._timeline_engine_playing
            or self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        ):
            subtitle_sec += self._SUBTITLE_PLAYBACK_LEAD_SEC
        text = self._subtitle_text_at(subtitle_sec) if self._subtitle_visible else ""
        self._subtitle_overlay.setText(text)
        self._subtitle_overlay.setVisible(bool(text))
        if text:
            self._update_subtitle_overlay_geometry()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._preview_stack and event.type() == QEvent.Type.Resize:
            self._update_subtitle_overlay_geometry()
        return super().eventFilter(watched, event)

    def _refresh_track_labels(self) -> None:
        parent = self._project_parent()
        audio = self._audio_relpath
        if self._audio_track_explicit and (not audio or parent is None or not (parent / audio).is_file()):
            self._set_audio_track("", 0.0)
            return
        if parent is None or not audio or not (parent / audio).is_file():
            audio = self._first_existing_relpath(
                [
                    "audio/video_production_narration.wav",
                    "audio/video_production_narration.mp3",
                ]
            )
        duration = 0.0
        if parent is not None and audio:
            duration = self._duration(parent / audio)
        self._set_audio_track(audio if audio.lower().endswith(".wav") else "", duration)

    def _set_audio_track(
        self, relpath: str, duration_sec: float, *, explicit: bool = False, start_sec: float | None = None
    ) -> None:
        if explicit:
            self._audio_track_explicit = True
        self._audio_relpath = relpath
        self._audio_duration_sec = max(0.0, float(duration_sec or 0.0))
        if start_sec is not None:
            self._audio_start_sec = max(0.0, float(start_sec or 0.0))
        if not relpath:
            self._audio_start_sec = 0.0
        self._timeline_strip.set_audio_track(relpath, self._audio_duration_sec, self._audio_start_sec)
        parent = self._project_parent()
        if parent is None or not relpath:
            self._timeline_audio.set_source(None)
            return
        path = (parent / relpath).resolve()
        if path.is_file():
            self._timeline_audio.set_source(path)
        else:
            self._timeline_audio.set_source(None)

    def _clear_audio_track(self) -> None:
        self._pause_timeline_audio()
        self._audio_selected = False
        self._set_audio_track("", 0.0, explicit=True)
        self._timeline_strip.set_audio_selected(False)
        self._sync_timeline(select_row=self._selected_row())
        self.stateChanged.emit()

    def _has_timeline_audio(self) -> bool:
        parent = self._project_parent()
        return bool(
            parent is not None
            and self._audio_relpath
            and self._audio_duration_sec > 0
            and (parent / self._audio_relpath).is_file()
        )

    def _play_timeline_audio_at(self, seconds: float) -> None:
        if not self._has_timeline_audio():
            return
        audio_local = seconds - self._audio_start_sec
        if 0 <= audio_local < self._audio_duration_sec:
            self._audio_clock_sec = seconds
            self._timeline_audio.play(audio_local)
            self._set_play_button_playing(True)

    def _pause_timeline_audio(self) -> None:
        self._timeline_audio.pause()
        if not self._timeline_engine_playing:
            self._set_play_button_playing(False)

    def _seek_timeline_audio(self, seconds: float, *, play: bool) -> None:
        if not self._has_timeline_audio():
            return
        audio_local = seconds - self._audio_start_sec
        if play and 0 <= audio_local < self._audio_duration_sec:
            self._audio_clock_sec = seconds
            self._timeline_audio.play(audio_local)
        elif play:
            self._timeline_audio.pause()
        else:
            self._timeline_audio.seek(max(0.0, audio_local))

    def _duration(self, path: Path) -> float:
        parent = self._project_parent()
        rel = _relative(path, parent) if parent is not None else str(path.resolve())
        if rel in self._duration_cache:
            return self._duration_cache[rel]
        try:
            duration = max(0.0, float(ffprobe_duration_seconds(path.resolve())))
        except (FfprobeError, OSError):
            duration = 0.0
        self._duration_cache[rel] = duration
        return duration

    def _known_duration(self, rel: str) -> float:
        if rel in self._duration_cache:
            return self._duration_cache[rel]
        for clip in self._clips:
            if clip.relpath == rel and clip.duration_sec > 0:
                self._duration_cache[rel] = clip.duration_sec
                return clip.duration_sec
        return 0.0

    def _add_selected_media(self) -> None:
        parent = self._project_parent()
        if parent is None:
            return
        checked_items = [
            self._media_list.item(idx)
            for idx in range(self._media_list.count())
            if self._media_list.item(idx) is not None
            and _is_checked_state(self._media_list.item(idx).checkState())
        ]
        items = checked_items if checked_items else self._media_list.selectedItems()
        start_sec = self._timeline_total_seconds()
        for item in items:
            rel = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if not rel:
                continue
            path = parent / rel
            if path.suffix.lower() == ".wav":
                self._set_audio_track(rel, self._duration(path), explicit=True, start_sec=start_sec)
                self._audio_selected = True
                self._timeline_strip.set_audio_selected(True)
            else:
                duration = (
                    DEFAULT_IMAGE_DURATION_SEC
                    if path.suffix.lower() in IMAGE_SUFFIXES
                    else self._duration(path)
                )
                self._clips.append(EditorClip(relpath=rel, duration_sec=duration, start_sec=start_sec))
                start_sec += max(0.0, duration)
        self._sync_timeline()
        self.stateChanged.emit()

    def _add_all_media(self) -> None:
        parent = self._project_parent()
        if parent is None:
            return
        start_sec = self._timeline_total_seconds()
        for idx in range(self._media_list.count()):
            item = self._media_list.item(idx)
            rel = str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
            if rel:
                path = parent / rel
                if path.suffix.lower() == ".wav":
                    self._set_audio_track(rel, self._duration(path), explicit=True, start_sec=start_sec)
                    self._audio_selected = True
                    self._timeline_strip.set_audio_selected(True)
                else:
                    duration = (
                        DEFAULT_IMAGE_DURATION_SEC
                        if path.suffix.lower() in IMAGE_SUFFIXES
                        else self._duration(path)
                    )
                    self._clips.append(EditorClip(relpath=rel, duration_sec=duration, start_sec=start_sec))
                    start_sec += max(0.0, duration)
        self._sync_timeline()
        self.stateChanged.emit()

    def _selected_row(self) -> int:
        rows = self._timeline.selectionModel().selectedRows()
        if not rows:
            return -1
        return int(rows[0].row())

    def _toggle_inspector(self) -> None:
        if self._inspector_pages.currentIndex() != 0:
            self._show_clip_inspector()
            return
        self._set_inspector_expanded(not self._inspector_expanded)

    def _set_inspector_button_active(self, button: QPushButton) -> None:
        for candidate in (self._btn_toggle_inspector, self._btn_subtitles):
            candidate.setProperty("active", candidate is button)
            candidate.style().unpolish(candidate)
            candidate.style().polish(candidate)

    def _show_clip_inspector(self) -> None:
        self._inspector_pages.setCurrentIndex(0)
        self._inspector_title.setText("클립 설정")
        self._inspector_badge.setText("클립")
        self._set_inspector_button_active(self._btn_toggle_inspector)
        self._set_inspector_expanded(True)

    def _show_subtitle_inspector(self) -> None:
        self._load_subtitles()
        self._inspector_pages.setCurrentIndex(1)
        self._inspector_title.setText("자막")
        self._inspector_badge.setText(str(len(self._subtitle_cues)))
        self._set_inspector_button_active(self._btn_subtitles)
        self._set_inspector_expanded(True)

    def _set_inspector_expanded(self, expanded: bool) -> None:
        self._inspector_expanded = bool(expanded)
        self._inspector_panel.setVisible(True)
        self._inspector_body.setVisible(self._inspector_expanded)
        self._inspector_panel.setFixedWidth(430 if self._inspector_expanded else 82)

    def _remove_selected_clip(self) -> None:
        if self._audio_selected:
            self._clear_audio_track()
            return
        row = self._selected_row()
        if 0 <= row < len(self._clips):
            self._clips.pop(row)
            self._sync_timeline(select_row=min(row, len(self._clips) - 1))
            self.stateChanged.emit()

    def _select_audio_track(self) -> None:
        self._audio_selected = True
        self._timeline.clearSelection()
        self._timeline_strip.set_audio_selected(True)
        self._load_selected_trim_controls()

    def _move_selected(self, delta: int) -> None:
        row = self._selected_row()
        new_row = row + delta
        if not (0 <= row < len(self._clips) and 0 <= new_row < len(self._clips)):
            return
        self._clips[row], self._clips[new_row] = self._clips[new_row], self._clips[row]
        self._sync_timeline(select_row=new_row)
        self.stateChanged.emit()

    def _video_overlap_target(
        self, start_sec: float, duration_sec: float, *, exclude_index: int = -1
    ) -> tuple[int, EditorClip] | None:
        start = max(0.0, float(start_sec or 0.0))
        end = start + max(0.04, float(duration_sec or 0.0))
        best: tuple[float, int, EditorClip] | None = None
        for index, clip in enumerate(self._clips):
            if index == exclude_index:
                continue
            clip_start = max(0.0, clip.start_sec)
            clip_end = clip_start + clip.effective_duration_sec
            overlap = min(end, clip_end) - max(start, clip_start)
            if overlap <= 0.001:
                continue
            candidate = (overlap, index, clip)
            if best is None or candidate[0] > best[0]:
                best = candidate
        if best is None:
            return None
        return best[1], best[2]

    def _ripple_video_insert(
        self, desired_start_sec: float, duration_sec: float, *, exclude_index: int = -1
    ) -> float:
        desired_start = max(0.0, float(desired_start_sec or 0.0))
        duration = max(0.04, float(duration_sec or 0.0))
        target = self._video_overlap_target(desired_start, duration, exclude_index=exclude_index)
        if target is None:
            return desired_start
        _target_index, target_clip = target
        target_start = max(0.0, target_clip.start_sec)
        target_end = target_start + target_clip.effective_duration_sec
        desired_center = desired_start + duration / 2.0
        target_center = target_start + target_clip.effective_duration_sec / 2.0
        insert_start = target_start if desired_center <= target_center else target_end
        for index, clip in enumerate(self._clips):
            if index == exclude_index:
                continue
            if clip.start_sec >= insert_start - 0.001:
                clip.start_sec += duration
        return insert_start

    def _move_clip_to(self, source: int, target_start_sec: float) -> None:
        if not (0 <= source < len(self._clips)):
            return
        clip = self._clips[source]
        clip.start_sec = self._ripple_video_insert(
            target_start_sec,
            clip.effective_duration_sec,
            exclude_index=source,
        )
        self._sync_timeline(select_row=source)
        self._set_playhead_seconds(clip.start_sec)
        self.stateChanged.emit()

    def _resize_image_clip(self, index: int, start_sec: float, duration_sec: float) -> None:
        if not (0 <= index < len(self._clips)):
            return
        clip = self._clips[index]
        if not clip.is_image:
            return
        old_start = clip.start_sec
        old_duration = clip.effective_duration_sec
        old_end = old_start + old_duration
        new_start = max(0.0, float(start_sec or 0.0))
        new_duration = max(0.10, float(duration_sec or 0.0))
        if new_start < old_start - 0.001:
            previous_ends = [
                other.start_sec + other.effective_duration_sec
                for other_index, other in enumerate(self._clips)
                if other_index != index
                and other.start_sec + other.effective_duration_sec <= old_start + 0.001
            ]
            if previous_ends:
                previous_end = max(previous_ends)
                new_start = max(new_start, previous_end)
                new_duration = max(0.10, old_end - new_start)
        clip.start_sec = new_start
        clip.duration_sec = new_duration
        clip.trim_in_sec = 0.0
        clip.trim_out_sec = 0.0
        if abs(new_start - old_start) <= 0.001:
            delta = new_duration - old_duration
            if abs(delta) > 0.001:
                for following_index, following in enumerate(self._clips):
                    if following_index != index and following.start_sec >= old_end - 0.001:
                        following.start_sec = max(0.0, following.start_sec + delta)
        self._sync_timeline(select_row=index)
        self.stateChanged.emit()

    def _move_audio_to(self, target_start_sec: float) -> None:
        self._audio_start_sec = max(0.0, float(target_start_sec or 0.0))
        self._timeline_strip.set_audio_track(self._audio_relpath, self._audio_duration_sec, self._audio_start_sec)
        self._sync_timeline(select_row=-1)
        self.stateChanged.emit()

    def _delete_gap(self, start_sec: float, end_sec: float) -> None:
        gap = max(0.0, float(end_sec or 0.0) - float(start_sec or 0.0))
        if gap <= 0.001:
            return
        for clip in self._clips:
            if clip.start_sec >= end_sec - 0.001:
                clip.start_sec = max(0.0, clip.start_sec - gap)
        if self._audio_relpath and self._audio_start_sec >= end_sec - 0.001:
            self._audio_start_sec = max(0.0, self._audio_start_sec - gap)
            self._timeline_strip.set_audio_track(self._audio_relpath, self._audio_duration_sec, self._audio_start_sec)
        self._sync_timeline(select_row=self._selected_row())
        self.stateChanged.emit()

    def _insert_media_relpaths(self, rels: list[str], target_start_sec: float) -> None:
        parent = self._project_parent()
        if parent is None or not rels:
            return
        target = max(0.0, float(target_start_sec or 0.0))
        video_items: list[tuple[str, float]] = []
        audio_items: list[tuple[str, float]] = []
        for rel in rels:
            if not rel:
                continue
            path = parent / rel
            if not path.is_file():
                continue
            duration = (
                DEFAULT_IMAGE_DURATION_SEC
                if path.suffix.lower() in IMAGE_SUFFIXES
                else self._duration(path)
            )
            if path.suffix.lower() == ".wav":
                audio_items.append((rel, duration))
            else:
                video_items.append((rel, duration))

        inserted = len(video_items)
        audio_changed = False
        video_start = target
        if video_items:
            total_video_duration = sum(max(0.04, duration) for _rel, duration in video_items)
            video_start = self._ripple_video_insert(target, total_video_duration)
            cursor = video_start
            for rel, duration in video_items:
                self._clips.append(EditorClip(relpath=rel, duration_sec=duration, start_sec=cursor))
                cursor += max(0.04, duration)
        for rel, duration in audio_items:
            self._set_audio_track(rel, duration, explicit=True, start_sec=target)
            self._audio_selected = True
            self._timeline_strip.set_audio_selected(True)
            audio_changed = True
        if inserted <= 0 and not audio_changed:
            return
        select_row = len(self._clips) - inserted if inserted > 0 else -1
        self._sync_timeline(select_row=select_row)
        if inserted > 0:
            self._set_playhead_seconds(video_start)
        self.stateChanged.emit()

    def _clip_start_seconds(self, row: int) -> float:
        if 0 <= row < len(self._clips):
            return self._clips[row].start_sec
        return 0.0

    def _timeline_total_seconds(self) -> float:
        video_end = max((clip.start_sec + clip.effective_duration_sec for clip in self._clips), default=0.0)
        audio_end = self._audio_start_sec + self._audio_duration_sec if self._audio_relpath else 0.0
        return max(video_end, audio_end)

    def _video_total_seconds(self) -> float:
        return max((clip.start_sec + clip.effective_duration_sec for clip in self._clips), default=0.0)

    def _timeline_preview_signature_for(self, parent: Path) -> str:
        payload = []
        for clip in self._clips:
            path = (parent / clip.relpath).resolve()
            try:
                stat = path.stat()
                stamp = [stat.st_size, int(stat.st_mtime)]
            except OSError:
                stamp = [0, 0]
            payload.append(
                {
                    "relpath": clip.relpath,
                    "duration": round(clip.duration_sec, 3),
                    "trim_in": round(clip.trim_in_sec, 3),
                    "trim_out": round(clip.trim_out_sec, 3),
                    "start": round(clip.start_sec, 3),
                    "stamp": stamp,
                }
            )
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _ensure_timeline_preview_file(self, parent: Path) -> Path:
        signature = self._timeline_preview_signature_for(parent)
        out_dir = parent / "export" / "video_editor_preview"
        out = out_dir / "timeline_preview.mp4"
        if signature == self._timeline_preview_signature and out.is_file():
            self._timeline_preview_path = out
            return out
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = [
            self._prepare_export_clip(parent=parent, clip=clip, index=index)
            for index, clip in enumerate(self._clips, start=1)
        ]
        list_path = out_dir / "concat.txt"
        list_lines = [f"file '{str(path.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in paths]
        list_path.write_text("\n".join(list_lines) + "\n", encoding="utf-8")
        cmd = [
            which_ffmpeg(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path.resolve()),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out.resolve()),
        ]
        try:
            run_ffmpeg(cmd, cwd=parent, timeout_sec=1800.0)
        except Exception:
            concat_segments_normalized(
                ffmpeg=which_ffmpeg(),
                segment_paths=paths,
                out_mp4=out,
                cwd=parent,
                width=1920,
                height=1080,
                fps=24,
            )
        self._timeline_preview_signature = signature
        self._timeline_preview_path = out
        return out

    def _timeline_seconds_for_player(self, row: int, player_position_ms: int) -> float:
        if not (0 <= row < len(self._clips)):
            return 0.0
        clip = self._clips[row]
        local = max(0.0, (player_position_ms / 1000.0) - clip.trim_in_sec)
        return self._clip_start_seconds(row) + min(local, clip.effective_duration_sec)

    def _row_at_timeline_seconds(self, seconds: float) -> tuple[int, float]:
        for row, clip in enumerate(self._clips):
            start = max(0.0, clip.start_sec)
            end = start + clip.effective_duration_sec
            if start <= seconds < end:
                return row, max(0.0, min(clip.effective_duration_sec, seconds - start))
        return -1, 0.0

    def _set_playhead_seconds(self, seconds: float) -> None:
        self._playhead_sec = max(0.0, float(seconds or 0.0))
        self._timeline_strip.set_playhead_seconds(self._playhead_sec)
        total = self._timeline_total_seconds()
        self._label_time.setText(f"{_fmt_badge_duration(self._playhead_sec)} / {_fmt_badge_duration(total)}")
        self._update_subtitle_overlay()

    def _show_black_preview(self) -> None:
        size = self._frame_label.size()
        pixmap = QPixmap(max(1, size.width()), max(1, size.height()))
        pixmap.fill(QColor("#000000"))
        self._frame_label.setPixmap(pixmap)
        self._preview_stack.setCurrentWidget(self._frame_label)

    def _show_black_preview_if_no_video(self, seconds: float) -> bool:
        row, _local = self._row_at_timeline_seconds(seconds)
        if row >= 0:
            return False
        self._show_black_preview()
        return True

    def _set_play_button_playing(self, playing: bool) -> None:
        self._btn_play.set_playing(playing)
        self._btn_play.setToolTip("정지" if playing else "재생")

    def _pause_all_playback(self) -> None:
        self._user_pause_requested = True
        self._ignore_engine_position_updates = True
        self._audio_tail_playing = False
        self._timeline_engine_playing = False
        self._preview_load_token += 1
        self._preview_transitioning = False
        self._timeline_engine.pause()
        self._timeline_audio.pause()
        self._player.pause()
        self._set_play_button_playing(False)

        def finish_pause() -> None:
            self._user_pause_requested = False
            self._ignore_engine_position_updates = False

        QTimer.singleShot(150, finish_pause)

    def _seek_to_start(self) -> None:
        self._ignore_engine_position_updates = True
        was_playing = self._timeline_engine_playing or self._timeline_audio.is_playing()
        if was_playing:
            self._timeline_engine.pause()
            self._pause_timeline_audio()
            self._audio_tail_playing = False
        self._seek_timeline_seconds(0.0)
        self._set_play_button_playing(False)
        QTimer.singleShot(250, lambda: setattr(self, "_ignore_engine_position_updates", False))

    def _seek_relative_seconds(self, delta: float) -> None:
        total = self._timeline_total_seconds()
        target = max(0.0, min(total, self._playhead_sec + delta))
        was_playing = self._timeline_engine_playing or self._timeline_audio.is_playing()
        if not was_playing:
            self._seek_timeline_seconds(target)
            return
        parent = self._project_parent()
        if parent is None:
            return
        self._playback_completed = False
        self._set_playhead_seconds(target)
        self._seek_timeline_audio(target, play=True)
        video_total = self._video_total_seconds()
        if target < video_total:
            self._audio_tail_playing = False
            self._timeline_engine.configure(parent, self._engine_clips())
            self._timeline_engine.play(target)
        else:
            if self._timeline_engine_playing:
                self._timeline_engine.pause()
            self._audio_tail_playing = True
        self._set_play_button_playing(True)

    def _sync_timeline(self, *, select_row: int = -1) -> None:
        self._timeline.setRowCount(0)
        for idx, clip in enumerate(self._clips, start=1):
            row = self._timeline.rowCount()
            self._timeline.insertRow(row)
            self._timeline.setItem(row, 0, QTableWidgetItem(str(idx)))
            self._timeline.setItem(row, 1, QTableWidgetItem(clip.relpath))
            self._timeline.setItem(row, 2, QTableWidgetItem(_fmt_duration(clip.effective_duration_sec)))
            self._timeline.setItem(row, 3, QTableWidgetItem(f"{clip.trim_in_sec:.2f}s"))
            self._timeline.setItem(row, 4, QTableWidgetItem(f"{clip.trim_out_sec:.2f}s"))
        self._timeline.resizeColumnsToContents()
        if 0 <= select_row < self._timeline.rowCount():
            self._audio_selected = False
            self._timeline_strip.set_audio_selected(False)
            self._timeline.selectRow(select_row)
        self._timeline_strip.set_clips(self._clips, self._selected_row())
        self._set_playhead_seconds(min(self._playhead_sec, self._timeline_total_seconds()))
        self._timeline_engine.configure(self._project_parent(), self._engine_clips())
        self._load_selected_trim_controls()

    def _engine_clips(self) -> list[TimelinePlaybackClip]:
        return [
            TimelinePlaybackClip(
                relpath=clip.relpath,
                duration_sec=clip.duration_sec,
                trim_in_sec=clip.trim_in_sec,
                trim_out_sec=clip.trim_out_sec,
                start_sec=clip.start_sec,
            )
            for clip in self._clips
        ]

    def _select_timeline_row(self, row: int) -> None:
        if 0 <= row < self._timeline.rowCount():
            self._audio_selected = False
            self._timeline_strip.set_audio_selected(False)
            self._timeline.selectRow(row)
            self._load_selected_trim_controls()

    def _load_selected_trim_controls(self) -> None:
        row = self._selected_row()
        if not (0 <= row < len(self._clips)):
            self._spin_trim_in.setValue(0.0)
            self._spin_trim_out.setValue(0.0)
            self._spin_image_duration.setValue(DEFAULT_IMAGE_DURATION_SEC)
            self._timeline_strip.set_clips(self._clips, -1)
            return
        self._set_inspector_expanded(True)
        self._show_clip_inspector()
        self._audio_selected = False
        self._timeline_strip.set_audio_selected(False)
        clip = self._clips[row]
        is_image = clip.is_image
        self._trim_in_label.setVisible(not is_image)
        self._spin_trim_in.setVisible(not is_image)
        self._trim_out_label.setVisible(not is_image)
        self._spin_trim_out.setVisible(not is_image)
        self._image_duration_label.setVisible(False)
        self._spin_image_duration.setVisible(False)
        self._btn_apply_trim.setVisible(not is_image)
        self._btn_apply_trim.setText("적용")
        if is_image:
            self._spin_image_duration.blockSignals(True)
            self._spin_image_duration.setValue(clip.duration_sec)
            self._spin_image_duration.blockSignals(False)
            self._timeline_strip.set_clips(self._clips, row)
            return
        self._spin_trim_in.blockSignals(True)
        self._spin_trim_out.blockSignals(True)
        self._spin_trim_in.setMaximum(max(0.0, clip.duration_sec - clip.trim_out_sec - 0.04))
        self._spin_trim_out.setMaximum(max(0.0, clip.duration_sec - clip.trim_in_sec - 0.04))
        self._spin_trim_in.setValue(clip.trim_in_sec)
        self._spin_trim_out.setValue(clip.trim_out_sec)
        self._spin_trim_in.blockSignals(False)
        self._spin_trim_out.blockSignals(False)
        self._timeline_strip.set_clips(self._clips, row)

    def _apply_trim_to_selected(self) -> None:
        row = self._selected_row()
        if not (0 <= row < len(self._clips)):
            return
        clip = self._clips[row]
        if clip.is_image:
            old_duration = clip.effective_duration_sec
            clip.duration_sec = max(0.10, float(self._spin_image_duration.value()))
            clip.trim_in_sec = 0.0
            clip.trim_out_sec = 0.0
            delta = clip.duration_sec - old_duration
            clip_end_before = clip.start_sec + old_duration
            if abs(delta) > 0.001:
                for index, following in enumerate(self._clips):
                    if index != row and following.start_sec >= clip_end_before - 0.001:
                        following.start_sec = max(0.0, following.start_sec + delta)
            self._sync_timeline(select_row=row)
            self.stateChanged.emit()
            return
        trim_in = max(0.0, float(self._spin_trim_in.value()))
        trim_out = max(0.0, float(self._spin_trim_out.value()))
        if trim_in + trim_out >= clip.duration_sec:
            trim_out = max(0.0, clip.duration_sec - trim_in - 0.04)
        clip.trim_in_sec = trim_in
        clip.trim_out_sec = trim_out
        self._sync_timeline(select_row=row)
        self.stateChanged.emit()

    def _seek_timeline_seconds(self, seconds: float) -> None:
        self._playback_completed = False
        self._audio_tail_playing = False
        self._set_playhead_seconds(seconds)
        video_total = self._video_total_seconds()
        audio_end = self._audio_start_sec + self._audio_duration_sec if self._has_timeline_audio() else 0.0
        if seconds >= video_total and self._has_timeline_audio() and audio_end > video_total:
            was_playing = self._timeline_engine_playing or self._timeline_audio.is_playing()
            self._audio_tail_playing = bool(was_playing and seconds < audio_end)
            if self._timeline_engine_playing:
                self._timeline_engine.pause()
            self._seek_timeline_audio(seconds, play=was_playing)
            return
        if self._timeline_engine_playing:
            self._timeline_engine.seek(seconds)
            self._seek_timeline_audio(seconds, play=True)
            self._show_black_preview_if_no_video(seconds)
            return
        row, local = self._row_at_timeline_seconds(seconds)
        if row < 0:
            self._show_black_preview()
            return
        self._load_preview_at(row, local, play=False)

    def _toggle_playback(self) -> None:
        is_playing = (
            self._timeline_engine_playing
            or self._timeline_audio.is_playing()
            or self._audio_tail_playing
            or self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        if is_playing:
            self._pause_all_playback()
            return
        parent = self._project_parent()
        if parent is None or (not self._clips and not self._has_timeline_audio()):
            return
        if any(not clip.is_image for clip in self._clips) and importlib.util.find_spec("av") is None:
            QMessageBox.warning(
                self,
                "타임라인 재생 엔진",
                "PyAV가 설치되어 있지 않아 타임라인 재생 엔진을 사용할 수 없습니다.\n"
                "requirements.txt 설치 후 다시 실행하세요.",
            )
            return
        total = self._timeline_total_seconds()
        if self._playback_completed or (total > 0 and self._playhead_sec >= total - 0.01):
            self._set_playhead_seconds(0.0)
        self._playback_completed = False
        self._audio_tail_playing = False
        self._timeline_engine.configure(parent, self._engine_clips())
        self._timeline_preview_mode = False
        self._preview_transitioning = False
        self._preview_row = -1
        self._player.stop()
        self._preview_stack.setCurrentWidget(self._frame_label)
        self._show_black_preview_if_no_video(self._playhead_sec)
        if not self._clips and self._has_timeline_audio() and self._playhead_sec < self._audio_start_sec:
            self._set_playhead_seconds(self._audio_start_sec)
        if self._clips and self._playhead_sec < self._video_total_seconds():
            self._timeline_engine.play(self._playhead_sec)
        else:
            self._audio_tail_playing = True
        self._play_timeline_audio_at(self._playhead_sec)
        if self._audio_tail_playing:
            self._set_play_button_playing(True)

    def _preview_selected(self) -> None:
        parent = self._project_parent()
        row = self._selected_row()
        if parent is None or not (0 <= row < len(self._clips)):
            return
        self._playback_completed = False
        self._timeline_engine.pause()
        self._pause_timeline_audio()
        self._audio_tail_playing = False
        self._timeline_preview_mode = False
        self._preview_stack.setCurrentWidget(self._video)
        self._load_preview_at(row, 0.0, play=True)

    def _load_preview_at(self, row: int, local_sec: float, *, play: bool) -> None:
        parent = self._project_parent()
        if parent is None or not (0 <= row < len(self._clips)):
            return
        self._timeline_engine.pause()
        self._pause_timeline_audio()
        self._audio_tail_playing = False
        self._preview_stack.setCurrentWidget(self._video)
        self._timeline_preview_mode = False
        path = (parent / self._clips[row].relpath).resolve()
        if not path.is_file():
            return
        clip = self._clips[row]
        if clip.is_image:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self._frame_label.setPixmap(
                    pixmap.scaled(
                        self._frame_label.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            self._preview_stack.setCurrentWidget(self._frame_label)
            self._preview_row = row
            self._set_playhead_seconds(
                clip.start_sec + max(0.0, min(local_sec, clip.effective_duration_sec))
            )
            self._label_status.setText(f"미리보기: {clip.relpath}")
            if play:
                self._timeline_engine.configure(parent, self._engine_clips())
                self._timeline_engine.play(self._playhead_sec)
                self._play_timeline_audio_at(self._playhead_sec)
            return
        was_playing = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        same_preview = self._preview_row == row
        self._preview_row = row
        seek_sec = clip.trim_in_sec + max(0.0, min(local_sec, clip.effective_duration_sec))
        self._preview_seek_sec = seek_sec
        self._preview_position_started = same_preview
        should_play = play or was_playing
        self._set_playhead_seconds(self._clip_start_seconds(row) + max(0.0, min(local_sec, clip.effective_duration_sec)))
        self._label_status.setText(f"미리보기: {self._clips[row].relpath}")
        if not same_preview:
            self._preview_load_token += 1
            token = self._preview_load_token
            self._preview_transitioning = True
            self._player.stop()
            self._player.setSource(QUrl.fromLocalFile(str(path)))
            QTimer.singleShot(
                80,
                lambda row=row, token=token, seek_sec=seek_sec, should_play=should_play: self._finish_preview_load(
                    row, token, seek_sec, should_play
                ),
            )
            return
        self._preview_transitioning = False
        self._player.setPosition(int(seek_sec * 1000))
        if should_play:
            self._player.play()
        else:
            self._player.pause()

    def _finish_preview_load(self, row: int, token: int, seek_sec: float, play: bool) -> None:
        if token != self._preview_load_token or row != self._preview_row:
            return
        if not (0 <= row < len(self._clips)):
            return
        self._player.setPosition(int(seek_sec * 1000))
        self._preview_position_started = False
        self._preview_transitioning = False
        if play:
            self._player.play()
        else:
            self._player.pause()

    def _on_timeline_frame_ready(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        if not pixmap.isNull():
            target_size = self._frame_label.size()
            if target_size.width() > 0 and target_size.height() > 0 and pixmap.size() != target_size:
                pixmap = pixmap.scaled(
                    target_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
            self._frame_label.setPixmap(
                pixmap
            )

    def _on_timeline_engine_position_changed(self, seconds: float) -> None:
        if self._ignore_engine_position_updates:
            return
        if self._audio_tail_playing:
            return
        self._show_black_preview_if_no_video(seconds)
        if (
            self._has_timeline_audio()
            and not self._timeline_audio.is_playing()
            and self._audio_start_sec <= seconds < self._audio_start_sec + self._audio_duration_sec
        ):
            self._play_timeline_audio_at(seconds)
        self._set_playhead_seconds(seconds)

    def _on_timeline_engine_finished(self) -> None:
        video_total = self._video_total_seconds()
        audio_end = self._audio_start_sec + self._audio_duration_sec if self._has_timeline_audio() else 0.0
        if self._has_timeline_audio() and audio_end > video_total:
            self._audio_tail_playing = True
            self._set_playhead_seconds(video_total)
            self._show_black_preview_if_no_video(video_total)
            if not self._timeline_audio.is_playing():
                self._play_timeline_audio_at(video_total)
            self._set_play_button_playing(True)
            return
        self._playback_completed = True
        self._audio_tail_playing = False
        self._pause_timeline_audio()
        self._set_playhead_seconds(self._timeline_total_seconds())

    def _on_timeline_engine_state_changed(self, playing: bool) -> None:
        if self._user_pause_requested:
            self._timeline_engine_playing = False
            self._audio_tail_playing = False
            self._set_play_button_playing(False)
            return
        self._timeline_engine_playing = playing
        audio_end = self._audio_start_sec + self._audio_duration_sec if self._has_timeline_audio() else 0.0
        if not playing and self._has_timeline_audio() and audio_end > self._video_total_seconds():
            if self._playhead_sec >= self._video_total_seconds() - 0.15 and self._timeline_audio.is_playing():
                self._audio_tail_playing = True
        if not playing and not self._audio_tail_playing:
            self._pause_timeline_audio()
        if playing or self._audio_tail_playing or self._timeline_audio.is_playing():
            self._set_play_button_playing(True)
        else:
            self._set_play_button_playing(False)

    def _on_timeline_engine_error(self, message: str) -> None:
        self._timeline_engine_playing = False
        self._pause_timeline_audio()
        self._set_play_button_playing(False)
        self._label_status.setText(message)
        QMessageBox.warning(self, "타임라인 재생 엔진", message)

    def _on_timeline_audio_position_changed(self, seconds: float) -> None:
        if self._ignore_engine_position_updates:
            return
        if not self._has_timeline_audio():
            return
        timeline_seconds = min(self._audio_start_sec + seconds, self._timeline_total_seconds())
        self._audio_clock_sec = timeline_seconds
        if self._audio_tail_playing:
            self._show_black_preview_if_no_video(timeline_seconds)
            self._set_playhead_seconds(timeline_seconds)
        else:
            self._update_subtitle_overlay()

    def _on_timeline_audio_finished(self) -> None:
        audio_end = self._audio_start_sec + self._audio_duration_sec
        if audio_end >= self._video_total_seconds():
            self._audio_tail_playing = False
            self._playback_completed = True
            self._set_playhead_seconds(self._timeline_total_seconds())
            self._set_play_button_playing(False)

    def _on_timeline_audio_state_changed(self, playing: bool) -> None:
        if self._user_pause_requested:
            self._set_play_button_playing(False)
            return
        if playing:
            self._set_play_button_playing(True)
        elif not self._timeline_engine_playing and not self._audio_tail_playing:
            self._set_play_button_playing(False)

    def _on_timeline_audio_error(self, message: str) -> None:
        self._label_status.setText(message)
        QMessageBox.warning(self, "타임라인 오디오", message)

    def _on_player_position_changed(self, position_ms: int) -> None:
        if self._preview_transitioning or self._playback_completed:
            return
        if self._timeline_preview_mode:
            total = self._timeline_total_seconds()
            seconds = min(total, max(0.0, position_ms / 1000.0))
            self._set_playhead_seconds(seconds)
            return
        row = self._preview_row
        if not (0 <= row < len(self._clips)):
            return
        clip = self._clips[row]
        position_sec = position_ms / 1000.0
        clip_end_sec = clip.duration_sec - clip.trim_out_sec
        if not self._preview_position_started:
            if position_sec <= self._preview_seek_sec + 0.75 or position_sec < clip_end_sec - 0.15:
                self._preview_position_started = True
            else:
                return
        if position_sec >= clip_end_sec:
            self._advance_to_next_clip(row)
            return
        self._set_playhead_seconds(self._timeline_seconds_for_player(row, position_ms))

    def _advance_to_next_clip(self, row: int) -> None:
        next_row = row + 1
        if 0 <= next_row < len(self._clips):
            self._load_preview_at(next_row, 0.0, play=True)
            return
        self._playback_completed = True
        self._preview_load_token += 1
        self._set_playhead_seconds(self._video_total_seconds())
        self._player.pause()

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if self._user_pause_requested:
            self._set_play_button_playing(False)
            return
        if self._timeline_engine_playing:
            return
        self._set_play_button_playing(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status != QMediaPlayer.MediaStatus.EndOfMedia or self._preview_transitioning:
            return
        if self._timeline_preview_mode:
            self._playback_completed = True
            self._set_playhead_seconds(self._timeline_total_seconds())
            return
        row = self._preview_row
        if not (0 <= row < len(self._clips)):
            return
        QTimer.singleShot(0, lambda row=row: self._advance_if_current(row))

    def _advance_if_current(self, row: int) -> None:
        if self._preview_transitioning or row != self._preview_row:
            return
        self._advance_to_next_clip(row)

    def _prepare_export_clip(self, *, parent: Path, clip: EditorClip, index: int) -> Path:
        src = (parent / clip.relpath).resolve()
        if clip.is_image:
            out_dir = parent / "export" / "video_editor_images"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"image_{index:03d}.mp4"
            cmd = [
                which_ffmpeg(),
                "-y",
                "-loop",
                "1",
                "-i",
                str(src),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t",
                f"{clip.effective_duration_sec:.3f}",
                "-vf",
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p",
                "-r",
                "24",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out.resolve()),
            ]
            run_ffmpeg(cmd, cwd=parent)
            return out
        if clip.trim_in_sec <= 0.001 and clip.trim_out_sec <= 0.001:
            return src
        out_dir = parent / "export" / "video_editor_trimmed"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"clip_{index:03d}.mp4"
        duration = clip.effective_duration_sec
        cmd = [
            which_ffmpeg(),
            "-y",
            "-ss",
            f"{clip.trim_in_sec:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(out.resolve()),
        ]
        run_ffmpeg(cmd, cwd=parent)
        return out

    def _export_timeline(self) -> None:
        parent = self._project_parent()
        if parent is None or not self._clips:
            QMessageBox.warning(self, "내보내기", "타임라인에 클립이 없습니다.")
            return
        source_paths = [(parent / clip.relpath).resolve() for clip in self._clips]
        missing = [str(p) for p in source_paths if not p.is_file()]
        if missing:
            QMessageBox.warning(self, "내보내기", "없는 파일이 있습니다:\n" + "\n".join(missing[:5]))
            return
        out = parent / "export" / "video_editor_timeline.mp4"
        self._label_status.setText("내보내는 중...")
        try:
            paths = [
                self._prepare_export_clip(parent=parent, clip=clip, index=index)
                for index, clip in enumerate(self._clips, start=1)
            ]
            concat_segments_normalized(
                ffmpeg=which_ffmpeg(),
                segment_paths=paths,
                out_mp4=out,
                cwd=parent,
                width=1920,
                height=1080,
                fps=24,
            )
        except Exception as e:
            QMessageBox.critical(self, "내보내기 실패", str(e))
            self._label_status.setText("내보내기 실패")
            return
        self._label_status.setText(f"내보내기 완료: {_relative(out, parent)}")
        QMessageBox.information(self, "내보내기", f"완료: {_relative(out, parent)}")
