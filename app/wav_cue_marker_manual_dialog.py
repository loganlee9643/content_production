from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


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


class WavCueMarkerManualDialog(QDialog):
    """QtMultimedia 미설치 환경용 수동 타임코드 입력 도구."""

    def __init__(self, parent=None, *, default_csv_path: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("WAV 구간 마커 (수동)")
        self.resize(900, 640)
        self._last_saved_csv: Path | None = None

        root = QVBoxLayout(self)
        hint = QLabel(
            "현재 환경에서 오디오 재생 기능(QtMultimedia)을 사용할 수 없어 수동 입력 모드로 엽니다.\n"
            "start/end 시간을 직접 입력하세요. 형식: 초(12.5) 또는 MM:SS.mmm(00:12.500)"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["start", "end", "narration", "transition", "image_relpath"])
        self._table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self._table, stretch=1)

        row_top = QHBoxLayout()
        self._btn_add = QPushButton("행 추가")
        self._btn_add.clicked.connect(self._on_add_row)
        self._btn_remove = QPushButton("선택 행 삭제")
        self._btn_remove.clicked.connect(self._on_remove_row)
        self._btn_sort = QPushButton("start 기준 정렬")
        self._btn_sort.clicked.connect(self._on_sort_rows)
        self._btn_last_open = QPushButton("마지막 end 비우기")
        self._btn_last_open.clicked.connect(self._on_clear_last_end)
        row_top.addWidget(self._btn_add)
        row_top.addWidget(self._btn_remove)
        row_top.addWidget(self._btn_sort)
        row_top.addWidget(self._btn_last_open)
        row_top.addStretch(1)
        root.addLayout(row_top)

        form = QFormLayout()
        default_csv = default_csv_path if default_csv_path is not None else Path.cwd() / "scripts" / "wav_cues.csv"
        self._edit_out_csv = QLineEdit(str(default_csv))
        form.addRow("출력 CSV", self._edit_out_csv)
        root.addLayout(form)

        row_bottom = QHBoxLayout()
        self._btn_save = QPushButton("CSV 저장")
        self._btn_save.clicked.connect(self._on_save_csv)
        self._btn_close = QPushButton("닫기")
        self._btn_close.clicked.connect(self.accept)
        row_bottom.addWidget(self._btn_save)
        row_bottom.addStretch(1)
        row_bottom.addWidget(self._btn_close)
        root.addLayout(row_bottom)

        self._on_add_row()

    def last_saved_csv(self) -> Path | None:
        return self._last_saved_csv

    def _on_add_row(self) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)
        self._table.setItem(r, 0, QTableWidgetItem(""))
        self._table.setItem(r, 1, QTableWidgetItem(""))
        self._table.setItem(r, 2, QTableWidgetItem(""))
        self._table.setItem(r, 3, QTableWidgetItem("fade"))
        self._table.setItem(r, 4, QTableWidgetItem(""))
        self._table.setCurrentCell(r, 0)

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
