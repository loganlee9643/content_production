from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.models.storyboard import (
    PROJECT_KIND_STORYBOARD,
    PROJECT_KIND_VIDEO_PRODUCTION,
    PROJECT_KIND_WAV_SEQUENCE,
)


class NewProjectModeDialog(QDialog):
    """새 프로젝트: 스토리보드, WAV 목록 중심, 영상 제작 프로젝트."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("새 프로젝트")
        self.resize(520, 340)
        self._chosen = PROJECT_KIND_STORYBOARD

        v = QVBoxLayout(self)
        v.addWidget(
            QLabel(
                "어떤 방식으로 주로 작업할지 선택하세요.\n"
                "(나중에도 기능은 둘 다 사용할 수 있으며, JSON에만 구분이 저장됩니다.)"
            )
        )
        self._btn_story = QPushButton(
            "스토리보드 (기본)\n"
            "— LLM 씬 · Piper TTS · 병합 SRT · 씬별 MP4 렌더"
        )
        self._btn_wav = QPushButton(
            "WAV 목록 중심\n"
            "— 「도구」→「WAV 목록으로 영상…」로 여러 WAV+자막을 한 영상으로"
        )
        self._btn_video = QPushButton(
            "영상 제작 프로젝트\n"
            "— Gemini 대본·이미지 · Veo 영상 · ElevenLabs 음성/자막"
        )
        self._btn_story.clicked.connect(self._pick_storyboard)
        self._btn_wav.clicked.connect(self._pick_wav)
        self._btn_video.clicked.connect(self._pick_video)
        v.addWidget(self._btn_story)
        v.addWidget(self._btn_wav)
        v.addWidget(self._btn_video)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        box.rejected.connect(self.reject)
        v.addWidget(box)

    def _pick_storyboard(self) -> None:
        self._chosen = PROJECT_KIND_STORYBOARD
        self.accept()

    def _pick_wav(self) -> None:
        self._chosen = PROJECT_KIND_WAV_SEQUENCE
        self.accept()

    def _pick_video(self) -> None:
        self._chosen = PROJECT_KIND_VIDEO_PRODUCTION
        self.accept()

    def selected_kind(self) -> str:
        return self._chosen
