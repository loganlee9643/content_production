from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class WavSeqRow:
    wav_source: Path
    narration: str
    transition: str
    image_relpath: str
    subtitle_relpath: str = ""
    start_sec: float | None = None
    end_sec: float | None = None
    segments: list[dict[str, object]] | None = None


class WavSequenceDialog(QDialog):
    """순서가 있는 WAV + 구간별 자막 → 단일 MP4(기존 렌더 파이프라인과 동일)."""

    def __init__(self, parent: QWidget | None, project_parent: Path) -> None:
        super().__init__(parent)
        self._project_parent = project_parent
        self.setWindowTitle("WAV 목록으로 영상 만들기")
        self.resize(920, 480)

        root = QVBoxLayout(self)
        hint = QLabel(
            "위에서 아래 순서로 이어 붙입니다. 각 WAV에 「자막 생성」으로 만든 SRT가 있으면 "
            "병합해 최종 영상에 입힙니다.\n"
            "해상도·FPS·전역 배경·BGM은 현재 화면(프롬프트)의 프로젝트 설정을 사용합니다. "
            "결과는 작업 폴더 아래 subs/, export/ 등에 만들어집니다."
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["WAV 파일(절대 경로)", "자막(이 구간)", "전환", "씬 배경 이미지(선택)"]
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        root.addWidget(self._table, stretch=1)

        row_btns = QHBoxLayout()
        self._btn_add_wavs = QPushButton("WAV 여러 개 추가…")
        self._btn_add_wavs.clicked.connect(self._on_add_wavs)
        self._btn_add_blank = QPushButton("빈 행 추가")
        self._btn_add_blank.clicked.connect(self._on_add_blank)
        self._btn_remove = QPushButton("선택 행 삭제")
        self._btn_remove.clicked.connect(self._on_remove_row)
        self._btn_up = QPushButton("위로")
        self._btn_up.clicked.connect(lambda: self._move_row(-1))
        self._btn_down = QPushButton("아래로")
        self._btn_down.clicked.connect(lambda: self._move_row(1))
        row_btns.addWidget(self._btn_add_wavs)
        row_btns.addWidget(self._btn_add_blank)
        row_btns.addWidget(self._btn_remove)
        row_btns.addWidget(self._btn_up)
        row_btns.addWidget(self._btn_down)
        row_btns.addStretch(1)
        root.addLayout(row_btns)

        form = QFormLayout()
        self._edit_out = QLineEdit("export/wav_sequence.mp4")
        self._edit_out.setPlaceholderText("예: export/wav_sequence.mp4")
        form.addRow("출력 MP4(프로젝트 폴더 기준 상대)", self._edit_out)
        root.addLayout(form)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

        self._accepted_rows: list[WavSeqRow] = []

    def output_relpath(self) -> str:
        return self._edit_out.text().strip().replace("\\", "/") or "export/wav_sequence.mp4"

    def _on_add_wavs(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "WAV 파일 선택",
            str(self._project_parent),
            "WAV (*.wav);;모든 파일 (*.*)",
        )
        for p in paths:
            self._append_row(Path(p))

    def _on_add_blank(self) -> None:
        self._append_row(None)

    def _append_row(self, wav: Path | None) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)
        w0 = wav.as_posix() if wav is not None and wav.is_file() else ""
        self._table.setItem(r, 0, QTableWidgetItem(w0))
        self._table.setItem(r, 1, QTableWidgetItem(""))
        combo = QComboBox()
        combo.addItems(["fade", "cut"])
        self._table.setCellWidget(r, 2, combo)
        self._table.setItem(r, 3, QTableWidgetItem(""))

    def _on_remove_row(self) -> None:
        r = self._table.currentRow()
        if r >= 0:
            self._table.removeRow(r)

    def _move_row(self, delta: int) -> None:
        r = self._table.currentRow()
        if r < 0:
            return
        nr = r + delta
        if nr < 0 or nr >= self._table.rowCount():
            return
        for c in (0, 1, 3):
            a = self._table.takeItem(r, c)
            b = self._table.takeItem(nr, c)
            self._table.setItem(r, c, b)
            self._table.setItem(nr, c, a)
        wa = self._table.cellWidget(r, 2)
        wb = self._table.cellWidget(nr, 2)
        if isinstance(wa, QComboBox) and isinstance(wb, QComboBox):
            ta, tb = wa.currentText(), wb.currentText()
            wa.setCurrentText(tb)
            wb.setCurrentText(ta)
        self._table.setCurrentCell(nr, 0)

    def _transition_at(self, row: int) -> str:
        w = self._table.cellWidget(row, 2)
        if isinstance(w, QComboBox):
            return w.currentText().strip() or "fade"
        return "fade"

    def _on_accept(self) -> None:
        rows = self._collect_rows()
        if rows is None:
            return
        if not rows:
            QMessageBox.warning(
                self,
                "WAV 목록",
                "최소 1개의 유효한 WAV 경로가 필요합니다. 빈 행은 자동으로 건너뜁니다.",
            )
            return
        self._accepted_rows = rows
        self.accept()

    def _collect_rows(self) -> list[WavSeqRow] | None:
        out: list[WavSeqRow] = []
        for r in range(self._table.rowCount()):
            it0 = self._table.item(r, 0)
            p = (it0.text() if it0 else "").strip()
            if not p:
                continue
            src = Path(p)
            if not src.is_file():
                QMessageBox.warning(self, "WAV 목록", f"파일을 찾을 수 없습니다:\n{src}")
                return None
            it1 = self._table.item(r, 1)
            narr = (it1.text() if it1 else "").strip() or " "
            it3 = self._table.item(r, 3)
            img = (it3.text() if it3 else "").strip()
            out.append(
                WavSeqRow(
                    wav_source=src.resolve(),
                    narration=narr,
                    transition=self._transition_at(r),
                    image_relpath=img,
                )
            )
        return out

    def accepted_data(self) -> tuple[list[WavSeqRow], str]:
        """exec() 후 Accept일 때만 호출."""
        return self._accepted_rows, self.output_relpath()
