from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


def _fmt_ms(ms: int) -> str:
    if ms < 0:
        ms = 0
    total_sec = ms // 1000
    mm = total_sec // 60
    ss = total_sec % 60
    msec = ms % 1000
    return f"{mm:02d}:{ss:02d}.{msec:03d}"


def _parse_time_to_sec(raw: str) -> float:
    s = (raw or "").strip()
    if not s:
        raise ValueError("빈 시간")
    if ":" not in s:
        return float(s)
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError("시간 형식은 MM:SS.mmm 또는 초")
    mm = float(parts[0])
    ss = float(parts[1])
    return (mm * 60.0) + ss


class WavCueMarkerDialog(QDialog):
    def __init__(self, parent=None, *, default_csv_path: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("WAV 구간 마커")
        self.resize(980, 680)

        self._audio_path: Path | None = None
        self._mark_start_ms: int | None = None
        self._updating_slider = False
        self._last_saved_csv: Path | None = None

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._audio_output.setVolume(0.8)

        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._tick_position)

        root = QVBoxLayout(self)

        row_top = QHBoxLayout()
        self._btn_open = QPushButton("오디오 열기")
        self._btn_open.clicked.connect(self._on_open_audio)
        self._label_audio = QLabel("(파일 없음)")
        self._label_audio.setWordWrap(True)
        row_top.addWidget(self._btn_open)
        row_top.addWidget(self._label_audio, stretch=1)
        root.addLayout(row_top)

        row_play = QHBoxLayout()
        self._btn_play = QPushButton("재생")
        self._btn_play.clicked.connect(self._on_toggle_play)
        self._btn_play.setEnabled(False)
        self._label_pos = QLabel("00:00.000 / 00:00.000")
        row_play.addWidget(self._btn_play)
        row_play.addWidget(self._label_pos)
        row_play.addStretch(1)
        root.addLayout(row_play)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.sliderPressed.connect(self._on_slider_pressed)
        self._slider.sliderReleased.connect(self._on_slider_released)
        root.addWidget(self._slider)

        row_mark = QGridLayout()
        self._btn_mark_start = QPushButton("시작 마커")
        self._btn_mark_start.clicked.connect(self._on_mark_start)
        self._btn_mark_start.setEnabled(False)
        self._label_mark_start = QLabel("시작: (없음)")
        self._btn_add_segment = QPushButton("현재 위치로 구간 추가")
        self._btn_add_segment.clicked.connect(self._on_add_segment)
        self._btn_add_segment.setEnabled(False)
        self._btn_clear_mark = QPushButton("마커 초기화")
        self._btn_clear_mark.clicked.connect(self._on_clear_mark)
        self._btn_clear_mark.setEnabled(False)
        row_mark.addWidget(self._btn_mark_start, 0, 0)
        row_mark.addWidget(self._label_mark_start, 0, 1)
        row_mark.addWidget(self._btn_add_segment, 1, 0)
        row_mark.addWidget(self._btn_clear_mark, 1, 1)
        root.addLayout(row_mark)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["start", "end", "narration", "transition", "image_relpath"])
        self._table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self._table, stretch=1)

        row_actions = QHBoxLayout()
        self._btn_remove_row = QPushButton("선택 행 삭제")
        self._btn_remove_row.clicked.connect(self._on_remove_row)
        self._btn_sort = QPushButton("start 기준 정렬")
        self._btn_sort.clicked.connect(self._on_sort_rows)
        self._btn_clear_last_end = QPushButton("마지막 end 비우기")
        self._btn_clear_last_end.clicked.connect(self._on_clear_last_end)
        row_actions.addWidget(self._btn_remove_row)
        row_actions.addWidget(self._btn_sort)
        row_actions.addWidget(self._btn_clear_last_end)
        row_actions.addStretch(1)
        root.addLayout(row_actions)

        form = QFormLayout()
        default_csv = default_csv_path if default_csv_path is not None else Path.cwd() / "scripts" / "wav_cues.csv"
        self._edit_out_csv = QLineEdit(str(default_csv))
        form.addRow("출력 CSV", self._edit_out_csv)
        root.addLayout(form)

        row_bottom = QHBoxLayout()
        self._btn_save_csv = QPushButton("CSV 저장")
        self._btn_save_csv.clicked.connect(self._on_save_csv)
        self._btn_close = QPushButton("닫기")
        self._btn_close.clicked.connect(self.accept)
        row_bottom.addWidget(self._btn_save_csv)
        row_bottom.addStretch(1)
        row_bottom.addWidget(self._btn_close)
        root.addLayout(row_bottom)

        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.positionChanged.connect(self._on_player_position_changed)

    def last_saved_csv(self) -> Path | None:
        return self._last_saved_csv

    def _on_open_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "오디오 파일",
            str(Path.cwd()),
            "Audio (*.wav *.mp3 *.m4a *.flac *.ogg);;All files (*.*)",
        )
        if not path:
            return
        p = Path(path).resolve()
        self._audio_path = p
        self._label_audio.setText(str(p))
        self._player.setSource(QUrl.fromLocalFile(str(p)))
        self._btn_play.setEnabled(True)
        self._btn_mark_start.setEnabled(True)
        self._btn_add_segment.setEnabled(True)
        self._btn_clear_mark.setEnabled(True)
        self._mark_start_ms = None
        self._label_mark_start.setText("시작: (없음)")

    def _on_toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            return
        self._player.play()

    def _on_duration_changed(self, duration_ms: int) -> None:
        self._slider.setRange(0, max(0, duration_ms))
        self._label_pos.setText(f"{_fmt_ms(self._player.position())} / {_fmt_ms(duration_ms)}")

    def _on_playback_state_changed(self, _state: QMediaPlayer.PlaybackState) -> None:
        playing = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self._btn_play.setText("일시정지" if playing else "재생")
        if playing:
            self._timer.start()
        else:
            self._timer.stop()

    def _on_player_position_changed(self, pos_ms: int) -> None:
        if self._updating_slider:
            return
        self._slider.setValue(pos_ms)
        self._label_pos.setText(f"{_fmt_ms(pos_ms)} / {_fmt_ms(self._player.duration())}")

    def _on_slider_pressed(self) -> None:
        self._updating_slider = True

    def _on_slider_released(self) -> None:
        self._player.setPosition(self._slider.value())
        self._updating_slider = False

    def _tick_position(self) -> None:
        if not self._updating_slider:
            self._slider.setValue(self._player.position())

    def _on_mark_start(self) -> None:
        self._mark_start_ms = self._player.position()
        self._label_mark_start.setText(f"시작: {_fmt_ms(self._mark_start_ms)}")

    def _on_add_segment(self) -> None:
        if self._mark_start_ms is None:
            QMessageBox.information(self, "구간", "먼저 시작 마커를 찍으세요.")
            return
        end_ms = self._player.position()
        if end_ms <= self._mark_start_ms:
            QMessageBox.warning(self, "구간", "끝 위치가 시작보다 커야 합니다.")
            return
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(f"{self._mark_start_ms / 1000.0:.3f}"))
        self._table.setItem(row, 1, QTableWidgetItem(f"{end_ms / 1000.0:.3f}"))
        self._table.setItem(row, 2, QTableWidgetItem(""))
        self._table.setItem(row, 3, QTableWidgetItem("fade"))
        self._table.setItem(row, 4, QTableWidgetItem(""))
        self._mark_start_ms = end_ms
        self._label_mark_start.setText(f"시작: {_fmt_ms(end_ms)}")

    def _on_clear_mark(self) -> None:
        self._mark_start_ms = None
        self._label_mark_start.setText("시작: (없음)")

    def _on_remove_row(self) -> None:
        r = self._table.currentRow()
        if r >= 0:
            self._table.removeRow(r)

    def _rows_as_dicts(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for r in range(self._table.rowCount()):
            start = (self._table.item(r, 0).text() if self._table.item(r, 0) else "").strip()
            end = (self._table.item(r, 1).text() if self._table.item(r, 1) else "").strip()
            narration = (self._table.item(r, 2).text() if self._table.item(r, 2) else "").strip()
            transition = (self._table.item(r, 3).text() if self._table.item(r, 3) else "").strip() or "fade"
            image_relpath = (self._table.item(r, 4).text() if self._table.item(r, 4) else "").strip()
            if not start:
                continue
            rows.append(
                {
                    "start": start,
                    "end": end,
                    "narration": narration,
                    "transition": transition,
                    "image_relpath": image_relpath,
                }
            )
        return rows

    def _on_sort_rows(self) -> None:
        try:
            rows = self._rows_as_dicts()
            rows.sort(key=lambda x: _parse_time_to_sec(x["start"]))
        except Exception as e:
            QMessageBox.warning(self, "정렬", f"start 파싱 실패: {e}")
            return
        self._table.setRowCount(0)
        for row in rows:
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(r, 0, QTableWidgetItem(row["start"]))
            self._table.setItem(r, 1, QTableWidgetItem(row["end"]))
            self._table.setItem(r, 2, QTableWidgetItem(row["narration"]))
            self._table.setItem(r, 3, QTableWidgetItem(row["transition"]))
            self._table.setItem(r, 4, QTableWidgetItem(row["image_relpath"]))

    def _on_clear_last_end(self) -> None:
        if self._table.rowCount() <= 0:
            return
        last = self._table.rowCount() - 1
        self._table.setItem(last, 1, QTableWidgetItem(""))

    def _on_save_csv(self) -> None:
        out_raw = self._edit_out_csv.text().strip()
        if not out_raw:
            QMessageBox.warning(self, "CSV 저장", "출력 CSV 경로를 입력하세요.")
            return
        rows = self._rows_as_dicts()
        if not rows:
            QMessageBox.warning(self, "CSV 저장", "저장할 구간 행이 없습니다.")
            return
        try:
            rows_sorted = sorted(rows, key=lambda x: _parse_time_to_sec(x["start"]))
        except Exception as e:
            QMessageBox.warning(self, "CSV 저장", f"start 파싱 실패: {e}")
            return
        out_csv = Path(out_raw).resolve()
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["start", "end", "narration", "transition", "image_relpath"])
            for row in rows_sorted:
                w.writerow([row["start"], row["end"], row["narration"], row["transition"], row["image_relpath"]])
        self._last_saved_csv = out_csv
        QMessageBox.information(self, "CSV 저장", f"저장됨:\n{out_csv}")
