from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout, QWidget

from app.models.storyboard import PROJECT_KIND_VIDEO_PRODUCTION, PROJECT_KIND_WAV_SEQUENCE


class NewProjectModeDialog(QDialog):
    """새 프로젝트: 영상 제작 프로젝트 또는 WAV 목록 중심."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("새 프로젝트")
        self.resize(520, 260)
        self._chosen = PROJECT_KIND_VIDEO_PRODUCTION

        v = QVBoxLayout(self)
        v.addWidget(QLabel("새 프로젝트 방식을 선택하세요."))

        self._btn_video = QPushButton(
            "영상 제작 프로젝트\n"
            "Gemini 대본/이미지, Veo 영상, ElevenLabs 음성/자막 단계별 생성"
        )
        self._btn_wav = QPushButton(
            "WAV 목록 중심\n"
            "여러 WAV와 자막/이미지를 하나의 영상으로 조립"
        )

        self._btn_video.clicked.connect(self._pick_video)
        self._btn_wav.clicked.connect(self._pick_wav)
        v.addWidget(self._btn_video)
        v.addWidget(self._btn_wav)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        box.rejected.connect(self.reject)
        v.addWidget(box)

    def _pick_video(self) -> None:
        self._chosen = PROJECT_KIND_VIDEO_PRODUCTION
        self.accept()

    def _pick_wav(self) -> None:
        self._chosen = PROJECT_KIND_WAV_SEQUENCE
        self.accept()

    def selected_kind(self) -> str:
        return self._chosen
