from __future__ import annotations

import json
import logging
import os
import re
import shutil
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QItemSelectionModel, QSettings, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QDoubleSpinBox,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.models.storyboard import (
    PROJECT_KIND_STORYBOARD,
    PROJECT_KIND_VIDEO_PRODUCTION,
    PROJECT_KIND_WAV_SEQUENCE,
    Scene,
    StoryProject,
)
from app.services.piper_tts import (
    piper_config_path_for_model,
    rhasspy_piper_phoneme_incompatible_reason,
    wrong_stem_json_hint,
)
from app.services.gemini_image_model_catalog import (
    DEFAULT_GEMINI_IMAGE_MODEL,
    GEMINI_IMAGE_MODEL_PRESET_IDS,
)
from app.services.gemini_model_catalog import DEFAULT_GEMINI_MODEL, GEMINI_MODEL_PRESET_IDS
from app.services.ffprobe_audio import FfprobeError, ffprobe_duration_seconds
from app.services.srt_build import build_merged_srt, merge_wav_subtitle_srts
from app.services.stt_transcribe import set_stt_runtime_options
from app.settings_dialog import SettingsDialog
from app.new_project_dialog import NewProjectModeDialog
from app.wav_sequence_dialog import WavSeqRow, WavSequenceDialog
from app.video_production_panel import VideoProductionPanel
from app.workers.final_render_worker import EXPORT_FINAL_REL, FinalRenderWorker
from app.workers.wav_sequence_render_worker import WavSequenceRenderWorker
from app.workers.gemini_scene_images_worker import GeminiSceneImagesWorker
from app.workers.llm_scenes_worker import LlmScenesWorker
from app.workers.piper_tts_worker import PiperTtsWorker
from app.workers.pipeline_worker import PipelineWorker
from app.workers.subtitle_worker import SubtitleWorker
from app.workers.gemini_wav_segments_worker import GeminiWavSegmentsWorker
from app.workers.gemini_wav_segment_images_worker import GeminiWavSegmentImagesWorker
from app.workers.stt_wav_segments_worker import SttWavSegmentsWorker
from app.workers.music_analysis_worker import MusicAnalysisWorker

_JSON_FILTER = "스토리보드 JSON (*.json);;모든 파일 (*.*)"

_ROLE_NAV_KIND = Qt.ItemDataRole.UserRole
_ROLE_SCENE_ROW = Qt.ItemDataRole.UserRole + 1
_ROLE_WAV_SEGMENTS = Qt.ItemDataRole.UserRole + 10
_ROLE_WAV_DURATION_SEC = Qt.ItemDataRole.UserRole + 11
_ROLE_WAV_SUBTITLE = Qt.ItemDataRole.UserRole + 12
_ROLE_WAV_REFERENCE_LYRICS = Qt.ItemDataRole.UserRole + 13
_ROLE_WAV_SUBTITLE_INTRO_SEC = Qt.ItemDataRole.UserRole + 14
_ROLE_WAV_SUBTITLE_OFFSET_SEC = Qt.ItemDataRole.UserRole + 15
_ROLE_WAV_INTRO_TITLE = Qt.ItemDataRole.UserRole + 16
_ROLE_WAV_INTRO_TITLE_DURATION_SEC = Qt.ItemDataRole.UserRole + 17

_WAV_SUBTITLE_PREFIX = "wav_subtitle_relpath:"
_WAV_REFERENCE_PREFIX = "wav_reference_lyrics:"
_WAV_SUBTITLE_INTRO_PREFIX = "wav_subtitle_intro_sec:"
_WAV_SUBTITLE_OFFSET_PREFIX = "wav_subtitle_offset_sec:"
_WAV_INTRO_TITLE_PREFIX = "wav_intro_title:"
_WAV_INTRO_TITLE_DURATION_PREFIX = "wav_intro_title_duration_sec:"
_WAV_SEGMENTS_PREFIX = "wav_segments_json:"

_NAV_PROMPT = 1
_NAV_SCENES_GROUP = 2
_NAV_SCENE_ROW = 3
_NAV_LOG = 4
_NAV_WAV_SEQUENCE = 5
_NAV_WAV_ROW = 6
_NAV_VIDEO_PRODUCTION = 7

_RIGHT_PROMPT = 0
_RIGHT_SCENES_TABLE = 1
_RIGHT_SCENE_ONE = 2
_RIGHT_LOG = 3
_RIGHT_WAV_SEQUENCE = 4
_RIGHT_WAV_ONE = 5
_RIGHT_VIDEO_PRODUCTION = 6

logger = logging.getLogger(__name__)


class WavBoundaryBar(QWidget):
    boundaryDragged = Signal(int, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._segments: list[dict[str, object]] = []
        self._duration_sec: float = 1.0
        self._selected_boundary: int = -1
        self._drag_boundary: int = -1
        self.setMinimumHeight(20)

    def set_data(self, segments: list[dict[str, object]], duration_sec: float, selected_boundary: int) -> None:
        self._segments = list(segments)
        self._duration_sec = max(0.001, duration_sec)
        self._selected_boundary = selected_boundary
        self.update()

    def _segment_color(self, idx: int) -> QColor:
        palette = ["#f28b82", "#fbbc04", "#fff475", "#ccff90", "#a7ffeb", "#cbf0f8", "#aecbfa", "#d7aefb"]
        return QColor(palette[idx % len(palette)])

    def _boundary_x(self, sec: float) -> int:
        w = max(1, self.width() - 1)
        ratio = min(1.0, max(0.0, sec / self._duration_sec))
        return int(round(ratio * w))

    def _x_to_sec(self, x: int) -> float:
        w = max(1, self.width() - 1)
        ratio = min(1.0, max(0.0, x / float(w)))
        return ratio * self._duration_sec

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#f0f0f0"))
        if not self._segments:
            p.setPen(QColor("#999999"))
            p.drawRect(self.rect().adjusted(0, 0, -1, -1))
            return
        h = self.height()
        for i, seg in enumerate(self._segments):
            st = float(seg["start_sec"])
            en = float(seg["end_sec"])
            x1 = self._boundary_x(st)
            x2 = self._boundary_x(en)
            if x2 <= x1:
                x2 = x1 + 1
            p.fillRect(x1, 0, x2 - x1, h, self._segment_color(i))
        p.setPen(QPen(QColor("#666666"), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        for i, seg in enumerate(self._segments):
            x = self._boundary_x(float(seg["end_sec"]))
            pen = QPen(QColor("#222222"), 3 if i == self._selected_boundary else 1)
            p.setPen(pen)
            p.drawLine(x, 0, x, h)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._segments:
            return
        x = int(event.position().x())
        best_idx = -1
        best_dist = 999999
        for i, seg in enumerate(self._segments):
            bx = self._boundary_x(float(seg["end_sec"]))
            d = abs(bx - x)
            if d < best_dist:
                best_idx = i
                best_dist = d
        if best_idx >= 0 and best_dist <= 10:
            self._drag_boundary = best_idx
            self.boundaryDragged.emit(best_idx, self._x_to_sec(x))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_boundary < 0:
            return
        x = int(event.position().x())
        self.boundaryDragged.emit(self._drag_boundary, self._x_to_sec(x))

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        self._drag_boundary = -1


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._project = StoryProject.empty_default()
        self._project_path: Path | None = None
        self._dirty = False
        self._validate_worker: PipelineWorker | None = None
        self._llm_worker: LlmScenesWorker | None = None
        self._tts_worker: PiperTtsWorker | None = None
        self._subtitle_worker: SubtitleWorker | None = None
        self._render_worker: FinalRenderWorker | None = None
        self._scene_image_worker: GeminiSceneImagesWorker | None = None
        self._wav_segments_worker: GeminiWavSegmentsWorker | None = None
        self._wav_segment_images_worker: GeminiWavSegmentImagesWorker | None = None
        self._stt_wav_segments_worker: SttWavSegmentsWorker | None = None
        self._music_analysis_worker: MusicAnalysisWorker | None = None
        self._job_cancel_worker: QThread | None = None
        self._music_analysis_target_row: int = -1
        self._wav_segments_target_row: int = -1
        self._wav_segment_images_target_row: int = -1
        self._stt_segments_target_row: int = -1
        self._settings = QSettings("ContentProduction", "ContentProductionApp")
        self._apply_stt_runtime_options_from_settings()
        self._single_scene_row: int = -1
        self._single_wav_row: int = -1
        self._render_from_wav_list_only: bool = False
        self._wav_preview_player = None
        self._wav_preview_audio_output = None
        self._wav_preview_source: str = ""
        self._wav_slider_dragging = False
        self._wav_boundary_bar_updating = False
        self._syncing_ui = False

        self.setWindowTitle("콘텐츠 제작")
        self.resize(1240, 760)

        self._build_actions()
        self._build_central()
        self.setStatusBar(QStatusBar())
        self._restore_gemini_image_combo_from_settings()
        if not self._try_restore_last_project():
            self._sync_ui_from_project(mark_clean=True, tree_focus=("prompt", 0))
        self._update_window_title()

    def _remember_last_project_path(self, path: Path) -> None:
        self._settings.setValue("project/last_path", str(path.resolve()))
        self._settings.sync()

    def _try_restore_last_project(self) -> bool:
        raw = str(self._settings.value("project/last_path", "") or "").strip()
        if not raw:
            return False
        p = Path(raw)
        if not p.is_file():
            return False
        try:
            self._project = StoryProject.load_json(p)
        except (OSError, ValueError, KeyError) as e:
            logger.warning("마지막 프로젝트 자동 열기 실패: %s", e)
            return False
        self._project_path = p
        self._sync_ui_from_project(mark_clean=True, tree_focus=("prompt", 0))
        self.statusBar().showMessage(f"마지막 프로젝트 자동 열기: {p}", 5000)
        return True

    def _build_actions(self) -> None:
        self._act_new = QAction("새 프로젝트", self)
        self._act_new.setShortcut("Ctrl+N")
        self._act_new.triggered.connect(self._on_new_project)

        self._act_open = QAction("열기…", self)
        self._act_open.setShortcut("Ctrl+O")
        self._act_open.triggered.connect(self._on_open)

        self._act_save = QAction("저장", self)
        self._act_save.setShortcut("Ctrl+S")
        self._act_save.triggered.connect(self._on_save)

        self._act_save_as = QAction("다른 이름으로 저장…", self)
        self._act_save_as.setShortcut("Ctrl+Shift+S")
        self._act_save_as.triggered.connect(self._on_save_as)

        self._act_settings = QAction("환경 설정…", self)
        self._act_settings.triggered.connect(self._on_open_settings)

        self._act_validate = QAction("환경 검증", self)
        self._act_validate.setShortcut("F5")
        self._act_validate.triggered.connect(self._on_validate)

        self._act_generate_scenes = QAction("LLM으로 씬 생성", self)
        self._act_generate_scenes.setShortcut("Ctrl+G")
        self._act_generate_scenes.triggered.connect(self._on_generate_scenes)

        self._act_tts_wav = QAction("WAV 생성 (컨텍스트에 따라 일괄/단일)", self)
        self._act_tts_wav.setShortcut("F6")
        self._act_tts_wav.triggered.connect(self._on_tts_generate_from_shortcut)

        self._act_subtitle_srt = QAction("병합 SRT 자막 생성", self)
        self._act_subtitle_srt.setShortcut("F7")
        self._act_subtitle_srt.triggered.connect(self._on_subtitle_generate_from_shortcut)

        self._act_export_mp4 = QAction("최종 MP4 렌더", self)
        self._act_export_mp4.setShortcut("F8")
        self._act_export_mp4.triggered.connect(self._on_export_render_from_shortcut)

        self._act_quit = QAction("종료", self)
        self._act_quit.setShortcut("Ctrl+Q")
        self._act_quit.triggered.connect(self.close)

        self._act_about = QAction("정보…", self)
        self._act_about.triggered.connect(self._on_about)

        menu_file = self.menuBar().addMenu("파일")
        menu_file.addAction(self._act_new)
        menu_file.addAction(self._act_open)
        menu_file.addSeparator()
        menu_file.addAction(self._act_save)
        menu_file.addAction(self._act_save_as)
        menu_file.addSeparator()
        menu_file.addAction(self._act_settings)
        menu_file.addSeparator()
        menu_file.addAction(self._act_quit)

        menu_tools = self.menuBar().addMenu("도구")
        menu_tools.addAction(self._act_validate)

        menu_help = self.menuBar().addMenu("도움말")
        menu_help.addAction(self._act_about)

        for a in (
            self._act_new,
            self._act_open,
            self._act_save,
            self._act_save_as,
            self._act_generate_scenes,
            self._act_tts_wav,
            self._act_subtitle_srt,
            self._act_export_mp4,
            self._act_validate,
        ):
            self.addAction(a)

    def _build_central(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        self._context_bar = QWidget()
        ctx = QHBoxLayout(self._context_bar)
        ctx.setContentsMargins(6, 4, 6, 4)

        self._btn_ctx_llm = QPushButton("LLM으로 씬 생성")
        self._btn_ctx_llm.clicked.connect(self._on_generate_scenes)

        self._btn_ctx_wav_add = QPushButton("WAV 파일 추가")
        self._btn_ctx_wav_add.clicked.connect(self._on_add_wav_sequence_files)

        self._btn_ctx_wav_all = QPushButton("전체 씬 WAV (Piper)")
        self._btn_ctx_wav_all.clicked.connect(self._on_tts_generate_batch)
        self._btn_ctx_srt = QPushButton("병합 SRT")
        self._btn_ctx_srt.clicked.connect(self._on_subtitle_generate)
        self._btn_ctx_mp4 = QPushButton("최종 MP4")
        self._btn_ctx_mp4.clicked.connect(self._on_export_render)

        self._btn_ctx_wav_one = QPushButton("이 씬만 WAV")
        self._btn_ctx_wav_one.clicked.connect(self._on_tts_generate_single)
        self._btn_ctx_wav_sequence_render = QPushButton("영상 만들기")
        self._btn_ctx_wav_sequence_render.clicked.connect(self._on_wav_sequence_mp4)

        ctx.addWidget(self._btn_ctx_llm)
        ctx.addWidget(self._btn_ctx_wav_add)
        ctx.addSpacing(16)
        ctx.addWidget(self._btn_ctx_wav_all)
        ctx.addWidget(self._btn_ctx_srt)
        ctx.addWidget(self._btn_ctx_mp4)
        ctx.addSpacing(16)
        ctx.addWidget(self._btn_ctx_wav_one)
        ctx.addWidget(self._btn_ctx_wav_sequence_render)
        ctx.addStretch(1)
        root.addWidget(self._context_bar)

        self._mode_banner = QLabel()
        self._mode_banner.setWordWrap(True)
        self._mode_banner.setObjectName("modeBanner")
        self._mode_banner.setStyleSheet(
            "#modeBanner { padding: 8px 10px; border-radius: 4px; "
            "background: #e8f0fe; border: 1px solid #c5d9f5; color: #1a1a1a; }"
        )
        root.addWidget(self._mode_banner)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._nav_tree = QTreeWidget()
        self._nav_tree.setHeaderHidden(True)
        self._nav_tree.setMinimumWidth(230)
        self._nav_tree.setRootIsDecorated(True)
        self._nav_tree.currentItemChanged.connect(self._on_nav_tree_current_changed)

        self._stack_right = QStackedWidget()
        self._stack_right.addWidget(self._build_panel_prompt())
        self._stack_right.addWidget(self._build_panel_scenes_table())
        self._stack_right.addWidget(self._build_panel_scene_one())
        self._stack_right.addWidget(self._build_panel_log())
        self._stack_right.addWidget(self._build_panel_wav_sequence())
        self._stack_right.addWidget(self._build_panel_wav_one())
        self._video_production_panel = VideoProductionPanel(self._video_project_parent, self)
        self._video_production_panel.stateChanged.connect(self._mark_dirty)
        self._stack_right.addWidget(self._video_production_panel)

        splitter.addWidget(self._nav_tree)
        splitter.addWidget(self._stack_right)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

        self._job_panel = QFrame()
        self._job_panel.setObjectName("jobPanel")
        self._job_panel.setStyleSheet(
            "#jobPanel { border-top: 1px solid #c8c8c8; padding-top: 4px; }"
        )
        self._job_panel.setVisible(False)
        job_lay = QVBoxLayout(self._job_panel)
        job_lay.setContentsMargins(0, 4, 0, 0)
        job_lay.setSpacing(4)
        job_title_row = QHBoxLayout()
        self._label_job_title = QLabel("")
        self._label_job_title.setStyleSheet("font-weight: bold;")
        job_title_row.addWidget(self._label_job_title, stretch=1)
        self._btn_job_cancel = QPushButton("중지")
        self._btn_job_cancel.setEnabled(False)
        self._btn_job_cancel.setToolTip("진행 중인 작업을 중지합니다. 현재 단계가 끝난 뒤 멈출 수 있습니다.")
        self._btn_job_cancel.clicked.connect(self._on_cancel_job)
        job_title_row.addWidget(self._btn_job_cancel)
        job_lay.addLayout(job_title_row)
        self._job_progress = QProgressBar()
        self._job_progress.setTextVisible(True)
        job_lay.addWidget(self._job_progress)
        self._job_log = QPlainTextEdit()
        self._job_log.setReadOnly(True)
        self._job_log.setMaximumHeight(110)
        self._job_log.setPlaceholderText("작업 내역이 여기에 표시됩니다.")
        job_lay.addWidget(self._job_log)
        root.addWidget(self._job_panel)

        self.setCentralWidget(central)
        self._update_context_bar_visibility()
        self._update_mode_banner()

    def _update_mode_banner(self) -> None:
        if self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION:
            self._mode_banner.setText(
                "모드: 영상 제작 프로젝트 — Gemini 대본·이미지, Veo 영상, ElevenLabs 음성·자막을 단계별로 생성합니다."
            )
        elif self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            self._mode_banner.setText(
                "모드: WAV 목록 — 정밀 자막(SRT) 생성 후 구간·이미지를 맞추고 MP4로 합칩니다."
            )
        else:
            self._mode_banner.setText(
                "모드: 스토리보드 — 프롬프트·씬·LLM·TTS·병합 SRT·최종 MP4 파이프라인으로 작업합니다. "
                "(WAV 목록 영상 기능도 「도구」에서 사용할 수 있습니다.)"
            )

    def _build_panel_prompt(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)

        form = QFormLayout()
        self._spin_target_minutes = QSpinBox()
        self._spin_target_minutes.setRange(1, 120)
        self._spin_target_minutes.setValue(7)
        self._spin_target_minutes.valueChanged.connect(self._mark_dirty)

        self._combo_resolution = QComboBox()
        self._combo_resolution.addItems(["1920x1080", "1280x720", "1080x1920"])
        self._combo_resolution.currentIndexChanged.connect(self._mark_dirty)

        self._spin_fps = QSpinBox()
        self._spin_fps.setRange(1, 60)
        self._spin_fps.setValue(30)
        self._spin_fps.valueChanged.connect(self._mark_dirty)

        form.addRow("목표 길이(분)", self._spin_target_minutes)
        form.addRow("해상도", self._combo_resolution)
        form.addRow("FPS", self._spin_fps)

        row_bg = QHBoxLayout()
        self._edit_bg_image = QLineEdit()
        self._edit_bg_image.setPlaceholderText("선택) 예: assets/bg.jpg (프로젝트 JSON과 같은 폴더 기준)")
        self._edit_bg_image.editingFinished.connect(self._mark_dirty)
        self._btn_browse_bg = QPushButton("찾기…")
        self._btn_browse_bg.clicked.connect(self._browse_background_image)
        row_bg.addWidget(self._edit_bg_image, stretch=1)
        row_bg.addWidget(self._btn_browse_bg)
        form.addRow("전역 배경 이미지", row_bg)

        row_bgm = QHBoxLayout()
        self._edit_bgm = QLineEdit()
        self._edit_bgm.setPlaceholderText("선택) 예: assets/bgm.mp3")
        self._edit_bgm.editingFinished.connect(self._mark_dirty)
        self._btn_browse_bgm = QPushButton("찾기…")
        self._btn_browse_bgm.clicked.connect(self._browse_bgm)
        row_bgm.addWidget(self._edit_bgm, stretch=1)
        row_bgm.addWidget(self._btn_browse_bgm)
        form.addRow("BGM 파일", row_bgm)

        self._spin_bgm_volume = QSpinBox()
        self._spin_bgm_volume.setRange(1, 50)
        self._spin_bgm_volume.setValue(20)
        self._spin_bgm_volume.setSuffix(" %")
        self._spin_bgm_volume.valueChanged.connect(self._mark_dirty)
        form.addRow("BGM 상대 볼륨", self._spin_bgm_volume)

        self._label_merged_srt = QLabel()
        self._label_merged_srt.setWordWrap(True)
        form.addRow("자막", self._label_merged_srt)

        self._label_export_final = QLabel()
        self._label_export_final.setWordWrap(True)
        form.addRow("보내기", self._label_export_final)

        outer.addLayout(form)

        hint = QLabel(
            "주제·톤·길이·타깃 시청자 등을 입력하세요. "
            "LLM·Piper·STT·자막 줄 길이 등은 메뉴 「파일」→「환경 설정」에서 바꿀 수 있습니다."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self._prompt = QPlainTextEdit()
        self._prompt.setPlaceholderText("프롬프트(한국어)…")
        self._prompt.textChanged.connect(self._mark_dirty)
        outer.addWidget(self._prompt, stretch=1)
        return w

    def _build_panel_scenes_table(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        hint = QLabel(
            "「씬」을 선택하면 전체 표를 편집합니다. 행 순서가 씬 순서입니다. "
            "자막 한 줄 글자 수는 환경 설정에서 지정합니다."
        )
        hint.setWordWrap(True)
        v.addWidget(hint)

        row_gemini_img = QHBoxLayout()
        row_gemini_img.addWidget(QLabel("배경 이미지 모델"))
        self._combo_gemini_image_model = QComboBox()
        self._combo_gemini_image_model.setEditable(True)
        self._combo_gemini_image_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_gemini_image_model.addItems(GEMINI_IMAGE_MODEL_PRESET_IDS)
        img_le = self._combo_gemini_image_model.lineEdit()
        if img_le is not None:
            img_le.setPlaceholderText("예: gemini-2.5-flash-image")
            img_le.editingFinished.connect(self._persist_scene_image_settings)
        self._combo_gemini_image_model.currentIndexChanged.connect(self._persist_scene_image_settings)
        self._btn_generate_scene_images = QPushButton("Gemini로 씬 배경 이미지 생성")
        self._btn_generate_scene_images.clicked.connect(self._on_generate_scene_images)
        row_gemini_img.addWidget(self._combo_gemini_image_model, stretch=1)
        row_gemini_img.addWidget(self._btn_generate_scene_images)
        v.addLayout(row_gemini_img)

        row_btns = QHBoxLayout()
        self._btn_add_scene = QPushButton("씬 추가")
        self._btn_add_scene.clicked.connect(self._on_add_scene)
        self._btn_remove_scene = QPushButton("선택 씬 삭제")
        self._btn_remove_scene.clicked.connect(self._on_remove_scene)
        row_btns.addWidget(self._btn_add_scene)
        row_btns.addWidget(self._btn_remove_scene)
        row_btns.addStretch(1)
        v.addLayout(row_btns)

        self._scene_table = QTableWidget(0, 5)
        self._scene_table.setHorizontalHeaderLabels(
            ["씬", "나레이션(한국어)", "비주얼 프롬프트", "전환", "씬 배경 이미지(상대)"]
        )
        self._scene_table.horizontalHeader().setStretchLastSection(True)
        self._scene_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._scene_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._scene_table.cellChanged.connect(self._mark_dirty)
        v.addWidget(self._scene_table, stretch=1)
        return w

    def _build_panel_scene_one(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self._label_scene_one_title = QLabel("씬 편집")
        self._label_scene_one_title.setStyleSheet("font-weight: bold;")
        v.addWidget(self._label_scene_one_title)

        form = QFormLayout()
        self._edit_single_narr = QPlainTextEdit()
        self._edit_single_narr.setPlaceholderText("나레이션(한국어)")
        self._edit_single_narr.textChanged.connect(self._on_single_scene_field_changed)
        form.addRow("나레이션", self._edit_single_narr)

        self._edit_single_visual = QPlainTextEdit()
        self._edit_single_visual.setPlaceholderText("비주얼 프롬프트")
        self._edit_single_visual.textChanged.connect(self._on_single_scene_field_changed)
        form.addRow("비주얼", self._edit_single_visual)

        self._edit_single_transition = QLineEdit()
        self._edit_single_transition.setPlaceholderText("fade 또는 cut")
        self._edit_single_transition.textChanged.connect(self._on_single_scene_field_changed)
        form.addRow("전환", self._edit_single_transition)

        row_img = QHBoxLayout()
        self._edit_single_image = QLineEdit()
        self._edit_single_image.setPlaceholderText("씬 배경 이미지 상대 경로")
        self._edit_single_image.editingFinished.connect(self._on_single_scene_field_changed)
        self._btn_browse_single_image = QPushButton("찾기…")
        self._btn_browse_single_image.clicked.connect(self._browse_scene_background_image)
        row_img.addWidget(self._edit_single_image, stretch=1)
        row_img.addWidget(self._btn_browse_single_image)
        form.addRow("씬 배경 이미지", row_img)

        self._label_single_audio = QLabel("(WAV 없음)")
        self._label_single_audio.setWordWrap(True)
        form.addRow("오디오", self._label_single_audio)

        v.addLayout(form)
        v.addStretch(1)
        return w

    def _build_panel_log(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("환경 검증·TTS·자막·렌더 로그")
        lay.addWidget(self._log, stretch=1)
        return w

    def _build_panel_wav_sequence(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        hint = QLabel(
            "위에서 아래 순서로 이어 붙입니다. 각 WAV는 「자막 생성」으로 만든 SRT를 MP4에 사용합니다.\n"
            "해상도·FPS·전역 배경·BGM은 「프롬프트」의 프로젝트 설정을 사용합니다. "
            "결과는 작업 폴더 아래 audio/, subs/, export/ 등에 만들어집니다."
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        self._wav_sequence_table = QTableWidget(0, 5)
        self._wav_sequence_table.setHorizontalHeaderLabels(
            ["WAV 파일(절대 경로)", "자막 SRT", "기본 전환", "기본 씬 배경 이미지(선택)", "선택"]
        )
        hdr = self._wav_sequence_table.horizontalHeader()
        hdr.setStretchLastSection(True)
        self._wav_sequence_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._wav_sequence_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._wav_sequence_table.cellChanged.connect(self._on_wav_sequence_cell_changed)
        self._wav_sequence_table.cellDoubleClicked.connect(self._on_wav_sequence_row_activated)
        self._wav_sequence_table.setColumnWidth(4, 56)
        self._wav_sequence_table.setColumnWidth(0, 420)
        # 내부 인덱스는 유지하면서 화면 표시만 "선택" 컬럼을 맨 앞으로 이동
        hdr.moveSection(4, 0)
        root.addWidget(self._wav_sequence_table, stretch=1)

        row_btns = QHBoxLayout()
        self._btn_wav_seq_remove = QPushButton("선택 행 삭제")
        self._btn_wav_seq_remove.clicked.connect(self._on_remove_wav_sequence_row)
        self._btn_wav_seq_up = QPushButton("위로")
        self._btn_wav_seq_up.clicked.connect(lambda: self._move_wav_sequence_row(-1))
        self._btn_wav_seq_down = QPushButton("아래로")
        self._btn_wav_seq_down.clicked.connect(lambda: self._move_wav_sequence_row(1))
        row_btns.addWidget(self._btn_wav_seq_remove)
        row_btns.addWidget(self._btn_wav_seq_up)
        row_btns.addWidget(self._btn_wav_seq_down)
        row_btns.addStretch(1)
        root.addLayout(row_btns)

        form = QFormLayout()
        self._edit_wav_sequence_out = QLineEdit("export/wav_sequence.mp4")
        self._edit_wav_sequence_out.setPlaceholderText("예: export/wav_sequence.mp4")
        self._edit_wav_sequence_out.editingFinished.connect(self._mark_dirty)
        form.addRow("출력 MP4(프로젝트 폴더 기준 상대)", self._edit_wav_sequence_out)
        root.addLayout(form)
        return w

    def _build_panel_wav_one(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        row_wav_header = QHBoxLayout()
        self._label_wav_one_title = QLabel("")
        self._label_wav_one_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        row_wav_header.addWidget(self._label_wav_one_title, stretch=1)
        self._btn_music_analysis = QPushButton("음악 분석")
        self._btn_music_analysis.clicked.connect(self._on_music_analysis)
        row_wav_header.addWidget(self._btn_music_analysis)
        v.addLayout(row_wav_header)

        form = QFormLayout()
        row_wav = QHBoxLayout()
        self._edit_single_wav_path = QLineEdit()
        self._edit_single_wav_path.setPlaceholderText("WAV 절대 경로")
        self._edit_single_wav_path.editingFinished.connect(self._on_single_wav_field_changed)
        self._btn_browse_single_wav = QPushButton("찾기…")
        self._btn_browse_single_wav.clicked.connect(self._browse_single_wav_file)
        row_wav.addWidget(self._edit_single_wav_path, stretch=1)
        row_wav.addWidget(self._btn_browse_single_wav)
        form.addRow("WAV 파일", row_wav)

        self._edit_single_wav_seg_prompt = QPlainTextEdit()
        self._edit_single_wav_seg_prompt.setPlaceholderText("선택 구간 배경 이미지 프롬프트")
        self._edit_single_wav_seg_prompt.textChanged.connect(self._on_selected_segment_editor_changed)

        self._combo_single_wav_transition = QComboBox()
        self._combo_single_wav_transition.addItems(["fade", "cut"])
        self._combo_single_wav_transition.currentIndexChanged.connect(self._on_selected_segment_editor_changed)

        self._edit_single_wav_image = QLineEdit()
        self._edit_single_wav_image.setPlaceholderText("선택 구간 씬 배경 이미지 상대 경로")
        self._edit_single_wav_image.editingFinished.connect(self._on_selected_segment_editor_changed)
        self._btn_browse_single_wav_image = QPushButton("찾기…")
        self._btn_browse_single_wav_image.clicked.connect(self._browse_single_wav_image)

        self._edit_single_wav_reference_lyrics = QPlainTextEdit()
        self._edit_single_wav_reference_lyrics.setPlaceholderText(
            "원곡 가사 전문(선택). 자막 생성·자동 구간 분석 시 LLM 참고 자료로 사용합니다."
        )
        self._edit_single_wav_reference_lyrics.setMinimumHeight(120)
        self._edit_single_wav_reference_lyrics.textChanged.connect(self._mark_dirty)
        form.addRow("원곡 가사", self._edit_single_wav_reference_lyrics)

        row_subtitle_tools = QHBoxLayout()
        self._btn_single_wav_stt_segments = QPushButton("자막 생성")
        self._btn_single_wav_stt_segments.clicked.connect(self._on_generate_stt_segments)
        row_subtitle_tools.addWidget(self._btn_single_wav_stt_segments)
        lbl_sub_path = QLabel("자막 위치")
        lbl_sub_path.setFixedWidth(56)
        row_subtitle_tools.addWidget(lbl_sub_path)
        self._edit_single_wav_subtitle_path = QLineEdit()
        self._edit_single_wav_subtitle_path.setPlaceholderText("subs/wav_subtitles/곡이름.srt")
        self._edit_single_wav_subtitle_path.editingFinished.connect(self._on_single_wav_subtitle_path_changed)
        row_subtitle_tools.addWidget(self._edit_single_wav_subtitle_path, stretch=1)
        self._btn_browse_single_wav_subtitle = QPushButton("저장 위치")
        self._btn_browse_single_wav_subtitle.clicked.connect(self._browse_single_wav_subtitle_path)
        self._btn_default_single_wav_subtitle = QPushButton("기본값")
        self._btn_default_single_wav_subtitle.setFixedWidth(56)
        self._btn_default_single_wav_subtitle.clicked.connect(self._on_default_single_wav_subtitle_path)
        self._btn_view_single_wav_subtitle = QPushButton("자막 파일 보기")
        self._btn_view_single_wav_subtitle.clicked.connect(self._on_show_single_wav_subtitle_dialog)
        row_subtitle_tools.addWidget(self._btn_browse_single_wav_subtitle)
        row_subtitle_tools.addWidget(self._btn_default_single_wav_subtitle)
        row_subtitle_tools.addWidget(self._btn_view_single_wav_subtitle)
        form.addRow("", row_subtitle_tools)

        row_subtitle_timing = QHBoxLayout()
        row_subtitle_timing.addWidget(QLabel("인트로 무자막"))
        self._spin_single_wav_subtitle_intro = QDoubleSpinBox()
        self._spin_single_wav_subtitle_intro.setRange(0.0, 600.0)
        self._spin_single_wav_subtitle_intro.setDecimals(1)
        self._spin_single_wav_subtitle_intro.setSingleStep(0.5)
        self._spin_single_wav_subtitle_intro.setSuffix(" 초")
        self._spin_single_wav_subtitle_intro.setToolTip(
            "이 시간 이전에는 자막을 표시하지 않습니다(인트로·간주 앞부분)."
        )
        self._spin_single_wav_subtitle_intro.valueChanged.connect(self._on_single_wav_subtitle_timing_changed)
        row_subtitle_timing.addWidget(self._spin_single_wav_subtitle_intro)
        row_subtitle_timing.addWidget(QLabel("자막 지연"))
        self._spin_single_wav_subtitle_offset = QDoubleSpinBox()
        self._spin_single_wav_subtitle_offset.setRange(-120.0, 120.0)
        self._spin_single_wav_subtitle_offset.setDecimals(1)
        self._spin_single_wav_subtitle_offset.setSingleStep(0.5)
        self._spin_single_wav_subtitle_offset.setSuffix(" 초")
        self._spin_single_wav_subtitle_offset.setToolTip("모든 자막 시간을 앞뒤로 밀거나 당깁니다.")
        self._spin_single_wav_subtitle_offset.valueChanged.connect(self._on_single_wav_subtitle_timing_changed)
        row_subtitle_timing.addWidget(self._spin_single_wav_subtitle_offset)
        row_subtitle_timing.addStretch(1)
        form.addRow("", row_subtitle_timing)

        row_intro_title = QHBoxLayout()
        row_intro_title.addWidget(QLabel("인트로 제목"))
        self._edit_single_wav_intro_title = QLineEdit()
        self._edit_single_wav_intro_title.setPlaceholderText("예: 나의 제주 오름 산책 (인트로에만 표시)")
        self._edit_single_wav_intro_title.editingFinished.connect(self._on_single_wav_intro_title_changed)
        row_intro_title.addWidget(self._edit_single_wav_intro_title, stretch=1)
        self._btn_intro_title_from_wav = QPushButton("파일명")
        self._btn_intro_title_from_wav.setFixedWidth(56)
        self._btn_intro_title_from_wav.setToolTip("WAV 파일 이름(확장자 제외)을 제목으로 넣습니다.")
        self._btn_intro_title_from_wav.clicked.connect(self._on_fill_intro_title_from_wav)
        row_intro_title.addWidget(self._btn_intro_title_from_wav)
        row_intro_title.addWidget(QLabel("표시"))
        self._spin_single_wav_intro_title_duration = QDoubleSpinBox()
        self._spin_single_wav_intro_title_duration.setRange(0.0, 120.0)
        self._spin_single_wav_intro_title_duration.setDecimals(1)
        self._spin_single_wav_intro_title_duration.setSingleStep(0.5)
        self._spin_single_wav_intro_title_duration.setSpecialValueText("자동")
        self._spin_single_wav_intro_title_duration.setSuffix(" 초")
        self._spin_single_wav_intro_title_duration.setToolTip(
            "0이면 「인트로 무자막」 시간(없으면 5초) 동안 제목을 표시합니다."
        )
        self._spin_single_wav_intro_title_duration.valueChanged.connect(
            self._on_single_wav_intro_title_changed
        )
        row_intro_title.addWidget(self._spin_single_wav_intro_title_duration)
        form.addRow("", row_intro_title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        v.addLayout(form)
        v.addWidget(sep)

        row_marker = QHBoxLayout()
        self._btn_single_wav_play = QPushButton("재생")
        self._btn_single_wav_play.clicked.connect(self._on_toggle_single_wav_playback)
        self._btn_single_wav_add_marker = QPushButton("마커 찍기")
        self._btn_single_wav_add_marker.clicked.connect(self._on_add_wav_segment_marker)
        self._btn_single_wav_auto_segment = QPushButton("자동 구간 분석")
        self._btn_single_wav_auto_segment.clicked.connect(self._on_auto_segment_single_wav_with_gemini)
        self._btn_single_wav_auto_images = QPushButton("구간별 이미지 자동 생성")
        self._btn_single_wav_auto_images.clicked.connect(self._on_auto_generate_segment_images)
        row_marker.addWidget(self._btn_single_wav_play)
        row_marker.addWidget(self._btn_single_wav_add_marker)
        row_marker.addWidget(self._btn_single_wav_auto_segment)
        row_marker.addWidget(self._btn_single_wav_auto_images)
        row_marker.addStretch(1)
        form_marker = QFormLayout()
        form_marker.addRow("구간 마커", row_marker)
        v.addLayout(form_marker)

        self._label_single_wav_pos = QLabel("00:00.000 / 00:00.000")
        self._slider_single_wav_pos = QSlider(Qt.Orientation.Horizontal)
        self._slider_single_wav_pos.setRange(0, 0)
        self._slider_single_wav_pos.sliderPressed.connect(self._on_single_wav_slider_pressed)
        self._slider_single_wav_pos.sliderReleased.connect(self._on_single_wav_slider_released)
        v.addWidget(self._label_single_wav_pos)
        v.addWidget(self._slider_single_wav_pos)

        self._wav_boundary_bar = WavBoundaryBar()
        self._wav_boundary_bar.boundaryDragged.connect(self._on_wav_boundary_bar_dragged)
        v.addWidget(self._wav_boundary_bar)

        self._table_single_wav_segments = QTableWidget(0, 6)
        self._table_single_wav_segments.setHorizontalHeaderLabels(
            ["색", "시작", "끝", "이미지 프롬프트", "전환", "씬 배경 이미지"]
        )
        self._table_single_wav_segments.horizontalHeader().setStretchLastSection(True)
        self._table_single_wav_segments.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table_single_wav_segments.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table_single_wav_segments.cellChanged.connect(self._on_single_wav_segments_table_changed)
        self._table_single_wav_segments.currentCellChanged.connect(self._on_single_wav_segment_selection_changed)
        v.addWidget(self._table_single_wav_segments, stretch=1)

        editor_form = QFormLayout()
        editor_form.addRow("선택 구간 이미지 프롬프트", self._edit_single_wav_seg_prompt)
        editor_form.addRow("선택 구간 전환", self._combo_single_wav_transition)
        row_sel_img = QHBoxLayout()
        row_sel_img.addWidget(self._edit_single_wav_image, stretch=1)
        row_sel_img.addWidget(self._btn_browse_single_wav_image)
        editor_form.addRow("선택 구간 씬 배경 이미지", row_sel_img)
        self._btn_delete_selected_segment = QPushButton("선택 구간 삭제")
        self._btn_delete_selected_segment.clicked.connect(self._on_delete_selected_wav_segment)
        editor_form.addRow("", self._btn_delete_selected_segment)
        v.addLayout(editor_form)
        v.addStretch(1)
        return w

    def _on_open_settings(self) -> None:
        dlg = SettingsDialog(self, self._settings)
        dlg.exec()
        self._apply_stt_runtime_options_from_settings()
        self._restore_gemini_image_combo_from_settings()
        if hasattr(self, "_video_production_panel"):
            self._video_production_panel._refresh_voice_label()

    def _settings_bool(self, key: str, default: bool) -> bool:
        v = self._settings.value(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def _settings_float(self, key: str, default: float) -> float:
        v = self._settings.value(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _settings_int(self, key: str, default: int) -> int:
        v = self._settings.value(key, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _apply_stt_runtime_options_from_settings(self) -> None:
        from app.stt_settings_defaults import (
            STT_RUNTIME_DEFAULTS_VERSION,
            STT_SETTINGS_DEFAULTS,
        )

        defaults = STT_SETTINGS_DEFAULTS
        v = self._settings.value("stt/runtime_defaults_version", 0)
        try:
            defaults_ver = int(v)
        except (TypeError, ValueError):
            defaults_ver = 0

        if defaults_ver < STT_RUNTIME_DEFAULTS_VERSION:
            for k, dv in defaults.items():
                self._settings.setValue(k, dv)
            self._settings.setValue("stt/runtime_defaults_version", STT_RUNTIME_DEFAULTS_VERSION)
        else:
            for k, dv in defaults.items():
                if self._settings.value(k, None) is None:
                    self._settings.setValue(k, dv)
        self._settings.sync()

        def _def_str(key: str) -> str:
            return str(defaults.get(key, "") or "")

        def _def_bool(key: str) -> bool:
            v = defaults.get(key, False)
            return bool(v) if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")

        def _def_float(key: str) -> float:
            return float(defaults.get(key, 0.0))

        def _def_int(key: str) -> int:
            return int(defaults.get(key, 1))

        set_stt_runtime_options(
            model=str(self._settings.value("stt/model", _def_str("stt/model")) or _def_str("stt/model")),
            compute_type=str(
                self._settings.value("stt/compute_type", _def_str("stt/compute_type")) or _def_str("stt/compute_type")
            ),
            vad_filter=self._settings_bool("stt/vad_filter", _def_bool("stt/vad_filter")),
            vad_threshold=self._settings_float("stt/vad_threshold", _def_float("stt/vad_threshold")),
            vad_min_silence_duration_ms=self._settings_int(
                "stt/vad_min_silence_duration_ms", _def_int("stt/vad_min_silence_duration_ms")
            ),
            vad_min_speech_duration_ms=self._settings_int(
                "stt/vad_min_speech_duration_ms", _def_int("stt/vad_min_speech_duration_ms")
            ),
            vad_speech_pad_ms=self._settings_int("stt/vad_speech_pad_ms", _def_int("stt/vad_speech_pad_ms")),
            beam_size=max(1, self._settings_int("stt/beam_size", _def_int("stt/beam_size"))),
            no_speech_threshold=self._settings_float("stt/no_speech_threshold", _def_float("stt/no_speech_threshold")),
            max_no_speech_prob=self._settings_float("stt/max_no_speech_prob", _def_float("stt/max_no_speech_prob")),
            log_prob_threshold=self._settings_float("stt/log_prob_threshold", _def_float("stt/log_prob_threshold")),
        )

    def _restore_gemini_image_combo_from_settings(self) -> None:
        im = self._settings.value("gemini/image_model", DEFAULT_GEMINI_IMAGE_MODEL)
        im_str = str(im) if im else DEFAULT_GEMINI_IMAGE_MODEL
        self._combo_gemini_image_model.blockSignals(True)
        self._combo_gemini_image_model.setCurrentText(im_str)
        self._combo_gemini_image_model.blockSignals(False)

    def _persist_scene_image_settings(self) -> None:
        self._settings.setValue(
            "gemini/image_model",
            self._combo_gemini_image_model.currentText().strip(),
        )

    def _subtitle_max_chars(self) -> int:
        mc = self._settings.value("subtitle/max_line_chars", 34)
        try:
            v = int(mc)
        except (TypeError, ValueError):
            v = 34
        return max(8, min(80, v))

    def _nav_snapshot(self) -> tuple[str, int]:
        it = self._nav_tree.currentItem()
        if it is None:
            if self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION:
                return ("video_production", 0)
            if self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
                return ("wav_sequence", 0)
            return ("prompt", 0)
        k = it.data(0, _ROLE_NAV_KIND)
        try:
            kind = int(k) if k is not None else _NAV_PROMPT
        except (TypeError, ValueError):
            kind = _NAV_PROMPT
        row = 0
        if kind in (_NAV_SCENE_ROW, _NAV_WAV_ROW):
            rv = it.data(0, _ROLE_SCENE_ROW)
            try:
                row = int(rv) if rv is not None else 0
            except (TypeError, ValueError):
                row = 0
        if kind == _NAV_PROMPT:
            return ("prompt", 0)
        if kind == _NAV_SCENES_GROUP:
            return ("scenes", 0)
        if kind == _NAV_LOG:
            return ("log", 0)
        if kind == _NAV_WAV_SEQUENCE:
            return ("wav_sequence", 0)
        if kind == _NAV_WAV_ROW:
            return ("wav", row)
        if kind == _NAV_VIDEO_PRODUCTION:
            return ("video_production", 0)
        if kind == _NAV_SCENE_ROW:
            return ("scene", row)
        if self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION:
            return ("video_production", 0)
        if self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            return ("wav_sequence", 0)
        return ("prompt", 0)

    def _rebuild_nav_tree(self) -> None:
        self._nav_tree.blockSignals(True)
        self._nav_tree.clear()

        if self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION:
            it_video = QTreeWidgetItem(["영상 제작"])
            it_video.setData(0, _ROLE_NAV_KIND, _NAV_VIDEO_PRODUCTION)
            self._nav_tree.addTopLevelItem(it_video)
        elif self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            it_wav = QTreeWidgetItem(["전체"])
            it_wav.setData(0, _ROLE_NAV_KIND, _NAV_WAV_SEQUENCE)
            for row in range(self._wav_sequence_table.rowCount()):
                child = QTreeWidgetItem([self._wav_nav_label(row)])
                child.setData(0, _ROLE_NAV_KIND, _NAV_WAV_ROW)
                child.setData(0, _ROLE_SCENE_ROW, row)
                it_wav.addChild(child)
            self._nav_tree.addTopLevelItem(it_wav)
            it_wav.setExpanded(True)
        else:
            it_prompt = QTreeWidgetItem(["프롬프트"])
            it_prompt.setData(0, _ROLE_NAV_KIND, _NAV_PROMPT)
            self._nav_tree.addTopLevelItem(it_prompt)

            it_scenes = QTreeWidgetItem(["씬"])
            it_scenes.setData(0, _ROLE_NAV_KIND, _NAV_SCENES_GROUP)
            for idx, s in enumerate(self._project.scenes):
                lab = f"씬 {s.scene_id}"
                ch = QTreeWidgetItem([lab])
                ch.setData(0, _ROLE_NAV_KIND, _NAV_SCENE_ROW)
                ch.setData(0, _ROLE_SCENE_ROW, idx)
                it_scenes.addChild(ch)
            self._nav_tree.addTopLevelItem(it_scenes)
            it_scenes.setExpanded(True)

        it_log = QTreeWidgetItem(["로그"])
        it_log.setData(0, _ROLE_NAV_KIND, _NAV_LOG)
        self._nav_tree.addTopLevelItem(it_log)

        self._nav_tree.blockSignals(False)

    def _apply_tree_focus(self, focus: tuple[str, int]) -> None:
        kind_s, row = focus
        target: QTreeWidgetItem | None = None
        if self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION:
            if kind_s == "log":
                target = self._nav_tree.topLevelItem(1)
            else:
                target = self._nav_tree.topLevelItem(0)
        elif self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            if kind_s == "log":
                target = self._nav_tree.topLevelItem(1)
            elif kind_s == "wav":
                parent = self._nav_tree.topLevelItem(0)
                if parent is not None:
                    n = parent.childCount()
                    if n > 0:
                        r = max(0, min(row, n - 1))
                        target = parent.child(r)
                    else:
                        target = parent
            else:
                target = self._nav_tree.topLevelItem(0)
        else:
            if kind_s == "prompt":
                target = self._nav_tree.topLevelItem(0)
            elif kind_s == "scenes":
                target = self._nav_tree.topLevelItem(1)
            elif kind_s == "log":
                target = self._nav_tree.topLevelItem(2)
            elif kind_s == "scene":
                parent = self._nav_tree.topLevelItem(1)
                if parent is not None:
                    n = parent.childCount()
                    if n > 0:
                        r = max(0, min(row, n - 1))
                        target = parent.child(r)
        if target is None:
            target = self._nav_tree.topLevelItem(0)
        self._nav_tree.setCurrentItem(target)

    def _on_nav_tree_current_changed(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if current is None:
            return
        self._flush_single_scene_to_table()
        self._flush_single_wav_to_table()
        k = current.data(0, _ROLE_NAV_KIND)
        try:
            kind = int(k) if k is not None else _NAV_PROMPT
        except (TypeError, ValueError):
            kind = _NAV_PROMPT

        if kind == _NAV_PROMPT:
            self._single_scene_row = -1
            self._stack_right.setCurrentIndex(_RIGHT_PROMPT)
        elif kind == _NAV_SCENES_GROUP:
            self._single_scene_row = -1
            self._stack_right.setCurrentIndex(_RIGHT_SCENES_TABLE)
        elif kind == _NAV_LOG:
            self._single_scene_row = -1
            self._stack_right.setCurrentIndex(_RIGHT_LOG)
        elif kind == _NAV_SCENE_ROW:
            rv = current.data(0, _ROLE_SCENE_ROW)
            try:
                row = int(rv) if rv is not None else 0
            except (TypeError, ValueError):
                row = 0
            row = max(0, min(row, max(0, self._scene_table.rowCount() - 1)))
            self._single_scene_row = row
            self._stack_right.setCurrentIndex(_RIGHT_SCENE_ONE)
            self._load_single_scene_form(row)
        elif kind == _NAV_WAV_SEQUENCE:
            self._single_scene_row = -1
            self._single_wav_row = -1
            self._stack_right.setCurrentIndex(_RIGHT_WAV_SEQUENCE)
        elif kind == _NAV_WAV_ROW:
            rv = current.data(0, _ROLE_SCENE_ROW)
            try:
                row = int(rv) if rv is not None else 0
            except (TypeError, ValueError):
                row = 0
            row = max(0, min(row, max(0, self._wav_sequence_table.rowCount() - 1)))
            self._single_scene_row = -1
            self._single_wav_row = row
            self._stack_right.setCurrentIndex(_RIGHT_WAV_ONE)
            self._load_single_wav_form(row)
        elif kind == _NAV_VIDEO_PRODUCTION:
            self._single_scene_row = -1
            self._single_wav_row = -1
            self._stack_right.setCurrentIndex(_RIGHT_VIDEO_PRODUCTION)
        else:
            self._single_scene_row = -1
            self._single_wav_row = -1
            if self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION:
                self._stack_right.setCurrentIndex(_RIGHT_VIDEO_PRODUCTION)
            elif self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
                self._stack_right.setCurrentIndex(_RIGHT_WAV_SEQUENCE)
            else:
                self._stack_right.setCurrentIndex(_RIGHT_PROMPT)

        self._update_context_bar_visibility()

    def _load_single_scene_form(self, row: int) -> None:
        if row < 0 or row >= self._scene_table.rowCount():
            return
        sid = self._cell_text(row, 0) or str(row + 1)
        self._label_scene_one_title.setText(f"씬 {sid} 편집")
        self._edit_single_narr.blockSignals(True)
        self._edit_single_narr.setPlainText(self._cell_text(row, 1))
        self._edit_single_narr.blockSignals(False)
        self._edit_single_visual.blockSignals(True)
        self._edit_single_visual.setPlainText(self._cell_text(row, 2))
        self._edit_single_visual.blockSignals(False)
        self._edit_single_transition.blockSignals(True)
        self._edit_single_transition.setText(self._cell_text(row, 3) or "fade")
        self._edit_single_transition.blockSignals(False)
        self._edit_single_image.blockSignals(True)
        self._edit_single_image.setText(self._cell_text(row, 4))
        self._edit_single_image.blockSignals(False)
        audio = ""
        if row < len(self._project.scenes):
            audio = (self._project.scenes[row].audio_relpath or "").strip()
        self._label_single_audio.setText(audio if audio else "(아직 WAV 없음 — 「이 씬만 WAV」 또는 전체 WAV)")

    def _on_single_scene_field_changed(self, *_args: object) -> None:
        if self._single_scene_row < 0:
            return
        self._flush_single_scene_to_table()
        self._mark_dirty()

    def _flush_single_scene_to_table(self) -> None:
        r = self._single_scene_row
        if r < 0 or r >= self._scene_table.rowCount():
            return
        self._scene_table.blockSignals(True)
        self._scene_table.setItem(r, 1, QTableWidgetItem(self._edit_single_narr.toPlainText()))
        self._scene_table.setItem(r, 2, QTableWidgetItem(self._edit_single_visual.toPlainText()))
        self._scene_table.setItem(r, 3, QTableWidgetItem(self._edit_single_transition.text().strip() or "fade"))
        self._scene_table.setItem(r, 4, QTableWidgetItem(self._edit_single_image.text().strip()))
        self._scene_table.blockSignals(False)

    def _browse_scene_background_image(self) -> None:
        if self._project_path is None:
            QMessageBox.warning(self, "이미지", "먼저 프로젝트를 저장하세요.")
            return
        start = str(self._project_path.parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "씬 배경 이미지",
            start,
            "이미지 (*.png *.jpg *.jpeg *.webp);;모든 파일 (*.*)",
        )
        if not path:
            return
        p = Path(path).resolve()
        parent = self._project_path.parent.resolve()
        try:
            rel = str(p.relative_to(parent)).replace("\\", "/")
        except ValueError:
            rel = p.as_posix()
        self._edit_single_image.setText(rel)
        self._on_single_scene_field_changed()

    def _update_context_bar_visibility(self) -> None:
        snap = self._nav_snapshot()
        kind_s, _ = snap
        is_wav_mode = self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE
        is_video_mode = self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION
        show_prompt = (not is_wav_mode and not is_video_mode) and kind_s == "prompt"
        show_batch_media = (not is_wav_mode and not is_video_mode) and kind_s == "scenes"
        show_single_wav = (not is_wav_mode and not is_video_mode) and kind_s == "scene"
        show_wav_tools = is_wav_mode

        self._btn_ctx_llm.setVisible(show_prompt)
        self._btn_ctx_wav_add.setVisible(show_wav_tools)
        self._btn_ctx_wav_all.setVisible(show_batch_media)
        self._btn_ctx_srt.setVisible(show_batch_media)
        self._btn_ctx_mp4.setVisible(show_batch_media)
        self._btn_ctx_wav_one.setVisible(show_single_wav)
        self._btn_ctx_wav_sequence_render.setVisible(show_wav_tools)

    def _on_add_wav_sequence_files(self) -> None:
        start = str(self._project_path.parent) if self._project_path is not None else str(self.project_root())
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "WAV 파일 선택",
            start,
            "WAV (*.wav);;모든 파일 (*.*)",
        )
        for p in paths:
            self._append_wav_sequence_row(Path(p))
        if paths:
            self._refresh_wav_mode_tree(("wav", self._wav_sequence_table.rowCount() - 1))
            self._mark_dirty()

    def _append_wav_sequence_row(self, wav: Path | None) -> None:
        r = self._wav_sequence_table.rowCount()
        self._wav_sequence_table.insertRow(r)
        w0 = wav.as_posix() if wav is not None and wav.is_file() else ""
        wav_item = QTableWidgetItem(w0)
        wav_item.setData(_ROLE_WAV_DURATION_SEC, None)
        self._wav_sequence_table.setItem(r, 0, wav_item)
        self._wav_sequence_table.setItem(r, 1, QTableWidgetItem("(없음)"))
        combo = QComboBox()
        combo.addItems(["fade", "cut"])
        combo.currentTextChanged.connect(lambda _t: self._mark_dirty())
        self._wav_sequence_table.setCellWidget(r, 2, combo)
        self._wav_sequence_table.setItem(r, 3, QTableWidgetItem(""))
        sel_item = QTableWidgetItem("")
        sel_item.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        sel_item.setCheckState(Qt.CheckState.Unchecked)
        self._wav_sequence_table.setItem(r, 4, sel_item)
        self._set_wav_segments_for_row(r, [])

    def _on_remove_wav_sequence_row(self) -> None:
        r = self._wav_sequence_table.currentRow()
        if r < 0:
            return
        if self._single_wav_row == r:
            self._flush_single_wav_to_table()
            self._single_wav_row = -1
        elif self._single_wav_row > r:
            self._single_wav_row -= 1
        self._wav_sequence_table.removeRow(r)
        if self._wav_sequence_table.rowCount() > 0:
            new_row = min(r, self._wav_sequence_table.rowCount() - 1)
            self._wav_sequence_table.setCurrentCell(new_row, 0)
        self._sync_wav_list_nav_tree()
        self._mark_dirty()

    def _move_wav_sequence_row(self, delta: int) -> None:
        r = self._wav_sequence_table.currentRow()
        if r < 0:
            return
        nr = r + delta
        if nr < 0 or nr >= self._wav_sequence_table.rowCount():
            return
        for c in (0, 1, 3, 4):
            a = self._wav_sequence_table.takeItem(r, c)
            b = self._wav_sequence_table.takeItem(nr, c)
            self._wav_sequence_table.setItem(r, c, b)
            self._wav_sequence_table.setItem(nr, c, a)
        wa = self._wav_sequence_table.cellWidget(r, 2)
        wb = self._wav_sequence_table.cellWidget(nr, 2)
        if isinstance(wa, QComboBox) and isinstance(wb, QComboBox):
            ta, tb = wa.currentText(), wb.currentText()
            wa.setCurrentText(tb)
            wb.setCurrentText(ta)
        self._wav_sequence_table.setCurrentCell(nr, 0)
        self._sync_wav_list_nav_tree()
        self._mark_dirty()

    def _transition_at_wav_sequence(self, row: int) -> str:
        w = self._wav_sequence_table.cellWidget(row, 2)
        if isinstance(w, QComboBox):
            return w.currentText().strip() or "fade"
        return "fade"

    def _wav_sequence_checkbox_column(self) -> int:
        """「선택」열은 표시상 맨 앞(visual 0)으로 옮겨져 있음."""
        return self._wav_sequence_table.horizontalHeader().logicalIndex(0)

    def _checked_wav_sequence_rows(self) -> set[int]:
        col = self._wav_sequence_checkbox_column()
        out: set[int] = set()
        for r in range(self._wav_sequence_table.rowCount()):
            it = self._wav_sequence_table.item(r, col)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.add(r)
        return out

    def _on_wav_sequence_cell_changed(self, _row: int, col: int) -> None:
        """「선택」 체크박스는 렌더용 UI 상태이므로 dirty에 포함하지 않는다."""
        if col == self._wav_sequence_checkbox_column():
            return
        self._mark_dirty()

    def _apply_wav_sequence_row_checks(self, rows: set[int]) -> None:
        col = self._wav_sequence_checkbox_column()
        tbl = self._wav_sequence_table
        prev = tbl.blockSignals(True)
        try:
            for r in range(tbl.rowCount()):
                it = tbl.item(r, col)
                if it is None:
                    continue
                it.setCheckState(
                    Qt.CheckState.Checked if r in rows else Qt.CheckState.Unchecked
                )
        finally:
            tbl.blockSignals(prev)

    def _restore_wav_sequence_highlighted_rows(self, rows: set[int]) -> None:
        sm = self._wav_sequence_table.selectionModel()
        if sm is None or not rows:
            return
        sm.clearSelection()
        model = self._wav_sequence_table.model()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for r in sorted(rows):
            if 0 <= r < self._wav_sequence_table.rowCount():
                sm.select(model.index(r, 0), flags)

    def _restore_wav_sequence_render_pick(self, pick: tuple[str, set[int] | None]) -> None:
        mode, rows = pick
        if mode == "checkbox" and rows:
            self._apply_wav_sequence_row_checks(rows)
        elif mode == "highlight" and rows:
            self._restore_wav_sequence_highlighted_rows(rows)

    def _resolve_wav_sequence_render_pick(self) -> tuple[str, set[int] | None]:
        """
        (mode, rows) — mode: checkbox | highlight | all
        - checkbox: 체크된 행만 렌더
        - highlight: 표에서 하이라이트된 행만
        - all: 전체 (rows는 None)
        """
        checked = self._checked_wav_sequence_rows()
        if checked:
            return ("checkbox", checked)
        highlighted = {
            i.row() for i in self._wav_sequence_table.selectionModel().selectedRows()
        }
        if highlighted:
            return ("highlight", highlighted)
        return ("all", None)

    def _resolve_wav_sequence_render_rows(self) -> set[int] | None:
        mode, rows = self._resolve_wav_sequence_render_pick()
        if mode == "all":
            return None
        return rows

    def _parse_wav_cue_time_seconds(self, raw: str, *, field_name: str, row_index: int) -> float:
        s = (raw or "").strip()
        if not s:
            raise ValueError(f"{row_index}행 {field_name}이 비어 있습니다.")
        if ":" not in s:
            try:
                sec = float(s)
            except ValueError as e:
                raise ValueError(f"{row_index}행 {field_name} 시간 형식이 올바르지 않습니다: {raw!r}") from e
            if sec < 0:
                raise ValueError(f"{row_index}행 {field_name}은 0 이상이어야 합니다.")
            return sec
        parts = s.split(":")
        if len(parts) > 3:
            raise ValueError(f"{row_index}행 {field_name} 시간 형식이 올바르지 않습니다: {raw!r}")
        try:
            nums = [float(p) for p in parts]
        except ValueError as e:
            raise ValueError(f"{row_index}행 {field_name} 시간 형식이 올바르지 않습니다: {raw!r}") from e
        if any(v < 0 for v in nums):
            raise ValueError(f"{row_index}행 {field_name}은 0 이상이어야 합니다.")
        if len(nums) == 2:
            mm, ss = nums
            return (mm * 60.0) + ss
        hh, mm, ss = nums
        return (hh * 3600.0) + (mm * 60.0) + ss

    def _parse_simple_time_seconds(self, raw: str) -> float:
        s = (raw or "").strip()
        if not s:
            raise ValueError("빈 시간")
        if ":" not in s:
            v = float(s)
            if v < 0:
                raise ValueError("음수 시간")
            return v
        parts = s.split(":")
        if len(parts) > 3:
            raise ValueError("시간 형식 오류")
        nums = [float(p) for p in parts]
        if any(v < 0 for v in nums):
            raise ValueError("음수 시간")
        if len(nums) == 2:
            mm, ss = nums
            return (mm * 60.0) + ss
        hh, mm, ss = nums
        return (hh * 3600.0) + (mm * 60.0) + ss

    def _project_parent_dir(self) -> Path:
        if self._project_path is not None:
            return self._project_path.parent.resolve()
        return (self.project_root() / "_session_project").resolve()

    def _path_to_project_relpath(self, path: Path) -> str:
        resolved = path.resolve()
        parent = self._project_parent_dir()
        try:
            return str(resolved.relative_to(parent)).replace("\\", "/")
        except ValueError:
            return resolved.as_posix()

    def _default_wav_subtitle_relpath(self, wav_path: Path) -> str:
        stem = (wav_path.stem or "audio").strip() or "audio"
        return f"subs/wav_subtitles/{stem}.srt"

    def _sync_single_wav_subtitle_path_edit(self) -> None:
        if self._single_wav_row < 0:
            self._edit_single_wav_subtitle_path.clear()
            return
        rel = self._wav_subtitle_relpath_for_row(self._single_wav_row)
        self._edit_single_wav_subtitle_path.blockSignals(True)
        self._edit_single_wav_subtitle_path.setText(rel)
        self._edit_single_wav_subtitle_path.blockSignals(False)

    def _decode_wav_subtitle_relpath(self, notes: str) -> str:
        for ln in (notes or "").splitlines():
            s = ln.strip()
            if s.startswith(_WAV_SUBTITLE_PREFIX):
                return s[len(_WAV_SUBTITLE_PREFIX) :].strip().replace("\\", "/")
        s = (notes or "").strip()
        if s.startswith(_WAV_SUBTITLE_PREFIX):
            return s[len(_WAV_SUBTITLE_PREFIX) :].strip().replace("\\", "/")
        return ""

    def _decode_wav_subtitle_intro_sec(self, notes: str) -> float | None:
        for ln in (notes or "").splitlines():
            s = ln.strip()
            if s.startswith(_WAV_SUBTITLE_INTRO_PREFIX):
                try:
                    return max(0.0, float(s[len(_WAV_SUBTITLE_INTRO_PREFIX) :].strip()))
                except ValueError:
                    return None
        return None

    def _decode_wav_intro_title(self, notes: str) -> str:
        for ln in (notes or "").splitlines():
            s = ln.strip()
            if s.startswith(_WAV_INTRO_TITLE_PREFIX):
                raw = s[len(_WAV_INTRO_TITLE_PREFIX) :].strip()
                if not raw:
                    return ""
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, str):
                        return parsed.strip()
                except (TypeError, ValueError):
                    return raw
        return ""

    def _decode_wav_intro_title_duration_sec(self, notes: str) -> float | None:
        for ln in (notes or "").splitlines():
            s = ln.strip()
            if s.startswith(_WAV_INTRO_TITLE_DURATION_PREFIX):
                try:
                    return max(0.0, float(s[len(_WAV_INTRO_TITLE_DURATION_PREFIX) :].strip()))
                except ValueError:
                    return None
        return None

    def _decode_wav_subtitle_offset_sec(self, notes: str) -> float | None:
        for ln in (notes or "").splitlines():
            s = ln.strip()
            if s.startswith(_WAV_SUBTITLE_OFFSET_PREFIX):
                try:
                    return float(s[len(_WAV_SUBTITLE_OFFSET_PREFIX) :].strip())
                except ValueError:
                    return None
        return None

    def _decode_wav_reference_lyrics(self, notes: str) -> str:
        for ln in (notes or "").splitlines():
            s = ln.strip()
            if s.startswith(_WAV_REFERENCE_PREFIX):
                raw = s[len(_WAV_REFERENCE_PREFIX) :].strip()
                if not raw:
                    return ""
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, str):
                        return parsed.strip()
                except (TypeError, ValueError):
                    return raw
        return ""

    def _encode_wav_scene_notes(
        self,
        segments: list[dict[str, object]],
        subtitle_relpath: str = "",
        reference_lyrics: str = "",
        subtitle_intro_sec: float | None = None,
        subtitle_offset_sec: float | None = None,
        intro_title: str = "",
        intro_title_duration_sec: float | None = None,
    ) -> str:
        parts: list[str] = []
        rel = (subtitle_relpath or "").strip().replace("\\", "/")
        if rel:
            parts.append(f"{_WAV_SUBTITLE_PREFIX}{rel}")
        if subtitle_intro_sec is not None and subtitle_intro_sec > 0:
            parts.append(f"{_WAV_SUBTITLE_INTRO_PREFIX}{subtitle_intro_sec:.3f}")
        if subtitle_offset_sec is not None and abs(subtitle_offset_sec) > 1e-6:
            parts.append(f"{_WAV_SUBTITLE_OFFSET_PREFIX}{subtitle_offset_sec:.3f}")
        ititle = (intro_title or "").strip()
        if ititle:
            parts.append(_WAV_INTRO_TITLE_PREFIX + json.dumps(ititle, ensure_ascii=False))
        if intro_title_duration_sec is not None and intro_title_duration_sec > 0:
            parts.append(f"{_WAV_INTRO_TITLE_DURATION_PREFIX}{intro_title_duration_sec:.3f}")
        ref = (reference_lyrics or "").strip()
        if ref:
            parts.append(_WAV_REFERENCE_PREFIX + json.dumps(ref, ensure_ascii=False))
        if segments:
            parts.append(_WAV_SEGMENTS_PREFIX + json.dumps(segments, ensure_ascii=False))
        return "\n".join(parts)

    def _decode_segments_from_scene_notes(self, notes: str) -> list[dict[str, object]]:
        bodies: list[str] = []
        for ln in (notes or "").splitlines():
            s = ln.strip()
            if s.startswith(_WAV_SEGMENTS_PREFIX):
                bodies.append(s[len(_WAV_SEGMENTS_PREFIX) :].strip())
        s = (notes or "").strip()
        if s.startswith(_WAV_SEGMENTS_PREFIX) and not bodies:
            bodies.append(s[len(_WAV_SEGMENTS_PREFIX) :].strip())
        out: list[dict[str, object]] = []
        for body in bodies:
            if not body:
                continue
            try:
                parsed = json.loads(body)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, list):
                continue
            for seg in parsed:
                if not isinstance(seg, dict):
                    continue
                try:
                    st = float(seg.get("start_sec", 0.0))
                    en = float(seg.get("end_sec", 0.0))
                except (TypeError, ValueError):
                    continue
                if en <= st:
                    continue
                out.append(
                    {
                        "start_sec": st,
                        "end_sec": en,
                        "image_prompt": str(
                            seg.get("image_prompt", seg.get("narration", ""))
                        ).strip(),
                        "transition": str(seg.get("transition", "fade")).strip() or "fade",
                        "image_relpath": str(seg.get("image_relpath", "")).strip(),
                    }
                )
        out.sort(key=lambda x: float(x["start_sec"]))
        return out

    def _ensure_wav_row_item(self, row: int) -> QTableWidgetItem:
        it = self._wav_sequence_table.item(row, 0)
        if it is None:
            it = QTableWidgetItem("")
            self._wav_sequence_table.setItem(row, 0, it)
        return it

    def _wav_subtitle_relpath_for_row(self, row: int) -> str:
        it = self._wav_sequence_table.item(row, 0)
        if it is not None:
            raw = it.data(_ROLE_WAV_SUBTITLE)
            if isinstance(raw, str) and raw.strip():
                return raw.strip().replace("\\", "/")
        col1 = self._wav_cell_text(row, 1).strip()
        if col1 and col1 not in ("(없음)", "-"):
            return col1.replace("\\", "/")
        return ""

    def _set_wav_table_cell_text(self, row: int, col: int, text: str) -> None:
        """WAV 목록 표 갱신 시 cellChanged로 dirty가 켜지지 않도록 신호 차단."""
        display = text
        tbl = self._wav_sequence_table
        prev = tbl.blockSignals(True)
        try:
            it = tbl.item(row, col)
            if it is None:
                tbl.setItem(row, col, QTableWidgetItem(display))
            elif it.text() != display:
                it.setText(display)
        finally:
            tbl.blockSignals(prev)

    def _set_wav_subtitle_relpath_for_row(self, row: int, relpath: str) -> None:
        it = self._ensure_wav_row_item(row)
        rel = (relpath or "").strip().replace("\\", "/")
        it.setData(_ROLE_WAV_SUBTITLE, rel)
        self._set_wav_table_cell_text(row, 1, rel or "(없음)")
        if row == self._single_wav_row:
            self._sync_single_wav_subtitle_path_edit()

    def _subtitle_abs_path_for_row(self, row: int) -> Path | None:
        rel = self._wav_subtitle_relpath_for_row(row)
        if not rel:
            return None
        parent = self._project_parent_dir()
        rel_path = Path(rel)
        if rel_path.is_absolute():
            path = rel_path.resolve()
        else:
            path = (parent / rel).resolve()
        return path if path.is_file() else path

    def _single_wav_subtitle_display(self) -> tuple[str, str]:
        """자막 다이얼로그용 (경로 라벨, 본문)."""
        if self._single_wav_row < 0:
            return "", "(WAV 항목을 선택하세요.)"
        rel = self._wav_subtitle_relpath_for_row(self._single_wav_row)
        if not rel:
            return "", "(자막 경로가 지정되지 않았습니다.)"
        path = self._subtitle_abs_path_for_row(self._single_wav_row)
        path_label = str(path) if path is not None else rel
        if path is None or not path.is_file():
            return path_label, f"(파일 없음 — 자막 생성 후 다시 보기)\n경로: {path_label}"
        try:
            return path_label, path.read_text(encoding="utf-8")
        except OSError as e:
            return path_label, f"(읽기 실패: {e})\n{path}"

    def _on_show_single_wav_subtitle_dialog(self) -> None:
        if self._single_wav_row < 0:
            QMessageBox.information(self, "자막 파일", "왼쪽에서 WAV 항목을 먼저 선택하세요.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("자막 파일 (SRT)")
        dlg.resize(720, 520)
        lay = QVBoxLayout(dlg)
        path_label = QLabel()
        path_label.setWordWrap(True)
        path_label.setStyleSheet("color: palette(mid);")
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)

        def reload() -> None:
            path_lbl, body = self._single_wav_subtitle_display()
            path_label.setText(path_lbl)
            view.setPlainText(body)

        reload()
        lay.addWidget(path_label)
        lay.addWidget(view, stretch=1)
        row_btns = QHBoxLayout()
        row_btns.addStretch(1)
        btn_refresh = QPushButton("새로고침")
        btn_refresh.clicked.connect(reload)
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(dlg.accept)
        btn_close.setDefault(True)
        row_btns.addWidget(btn_refresh)
        row_btns.addWidget(btn_close)
        lay.addLayout(row_btns)
        dlg.exec()

    def _wav_reference_lyrics_for_row(self, row: int) -> str:
        it = self._wav_sequence_table.item(row, 0)
        if it is None:
            return ""
        raw = it.data(_ROLE_WAV_REFERENCE_LYRICS)
        if isinstance(raw, str):
            return raw.strip()
        return ""

    def _set_wav_reference_lyrics_for_row(self, row: int, text: str) -> None:
        it = self._ensure_wav_row_item(row)
        it.setData(_ROLE_WAV_REFERENCE_LYRICS, (text or "").strip())

    def _default_subtitle_intro_sec(self) -> float:
        v = self._settings.value("subtitle/default_intro_skip_sec", 0.0)
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 0.0

    def _default_subtitle_offset_sec(self) -> float:
        v = self._settings.value("subtitle/default_offset_sec", 0.0)
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def _vocal_retime_with_lyrics_enabled(self) -> bool:
        v = self._settings.value("subtitle/vocal_retime_with_lyrics", True)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() not in ("0", "false", "no", "off")

    def _wav_subtitle_intro_sec_for_row(self, row: int) -> float:
        it = self._wav_sequence_table.item(row, 0)
        if it is not None:
            raw = it.data(_ROLE_WAV_SUBTITLE_INTRO_SEC)
            if raw is not None:
                try:
                    return max(0.0, float(raw))
                except (TypeError, ValueError):
                    pass
        return self._default_subtitle_intro_sec()

    def _wav_subtitle_offset_sec_for_row(self, row: int) -> float:
        it = self._wav_sequence_table.item(row, 0)
        if it is not None:
            raw = it.data(_ROLE_WAV_SUBTITLE_OFFSET_SEC)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
        return self._default_subtitle_offset_sec()

    def _set_wav_subtitle_intro_sec_for_row(self, row: int, sec: float) -> None:
        it = self._ensure_wav_row_item(row)
        it.setData(_ROLE_WAV_SUBTITLE_INTRO_SEC, max(0.0, float(sec)))

    def _set_wav_subtitle_offset_sec_for_row(self, row: int, sec: float) -> None:
        it = self._ensure_wav_row_item(row)
        it.setData(_ROLE_WAV_SUBTITLE_OFFSET_SEC, float(sec))

    def _wav_intro_title_for_row(self, row: int) -> str:
        it = self._wav_sequence_table.item(row, 0)
        if it is not None:
            raw = it.data(_ROLE_WAV_INTRO_TITLE)
            if isinstance(raw, str):
                return raw.strip()
        return ""

    def _wav_intro_title_duration_sec_for_row(self, row: int) -> float:
        it = self._wav_sequence_table.item(row, 0)
        if it is not None:
            raw = it.data(_ROLE_WAV_INTRO_TITLE_DURATION_SEC)
            if raw is not None:
                try:
                    return max(0.0, float(raw))
                except (TypeError, ValueError):
                    pass
        return 0.0

    def _set_wav_intro_title_for_row(self, row: int, title: str) -> None:
        it = self._ensure_wav_row_item(row)
        it.setData(_ROLE_WAV_INTRO_TITLE, (title or "").strip())

    def _set_wav_intro_title_duration_sec_for_row(self, row: int, sec: float) -> None:
        it = self._ensure_wav_row_item(row)
        it.setData(_ROLE_WAV_INTRO_TITLE_DURATION_SEC, max(0.0, float(sec)))

    def _on_single_wav_intro_title_changed(self, *_args: object) -> None:
        if self._single_wav_row < 0 or self._syncing_ui:
            return
        self._set_wav_intro_title_for_row(
            self._single_wav_row, self._edit_single_wav_intro_title.text()
        )
        self._set_wav_intro_title_duration_sec_for_row(
            self._single_wav_row, self._spin_single_wav_intro_title_duration.value()
        )
        self._mark_dirty()

    def _on_fill_intro_title_from_wav(self) -> None:
        wav_path = Path(self._edit_single_wav_path.text().strip())
        if wav_path.is_file():
            self._edit_single_wav_intro_title.setText(wav_path.stem)
        elif self._single_wav_row >= 0:
            p = Path(self._wav_cell_text(self._single_wav_row, 0).strip())
            if p.name:
                self._edit_single_wav_intro_title.setText(p.stem)
        self._on_single_wav_intro_title_changed()

    def _on_single_wav_subtitle_timing_changed(self, *_args: object) -> None:
        if self._single_wav_row < 0 or self._syncing_ui:
            return
        self._set_wav_subtitle_intro_sec_for_row(
            self._single_wav_row, self._spin_single_wav_subtitle_intro.value()
        )
        self._set_wav_subtitle_offset_sec_for_row(
            self._single_wav_row, self._spin_single_wav_subtitle_offset.value()
        )
        self._mark_dirty()

    def _collect_wav_sequence_rows(
        self,
        *,
        allow_empty: bool = True,
        selected_rows: set[int] | None = None,
    ) -> list[WavSeqRow] | None:
        self._flush_single_wav_to_table()
        out: list[WavSeqRow] = []
        for r in range(self._wav_sequence_table.rowCount()):
            if selected_rows is not None and r not in selected_rows:
                continue
            it0 = self._wav_sequence_table.item(r, 0)
            p = (it0.text() if it0 else "").strip()
            if not p:
                continue
            src = Path(p)
            segs = self._wav_segments_for_row(r)
            sub_rel = self._wav_subtitle_relpath_for_row(r)
            img_item = self._wav_sequence_table.item(r, 3)
            img = (img_item.text() if img_item else "").strip()
            if allow_empty:
                out.append(
                    WavSeqRow(
                        wav_source=src,
                        narration=" ",
                        transition=self._transition_at_wav_sequence(r),
                        image_relpath=img,
                        subtitle_relpath=sub_rel,
                        segments=segs,
                    )
                )
                continue
            if not src.is_file():
                QMessageBox.warning(self, "WAV 목록", f"파일을 찾을 수 없습니다:\n{src}")
                return None
            out.append(
                WavSeqRow(
                    wav_source=src.resolve(),
                    narration=" ",
                    transition=self._transition_at_wav_sequence(r),
                    image_relpath=img,
                    subtitle_relpath=sub_rel,
                    segments=segs,
                )
            )
        return out

    def _sync_wav_sequence_table_from_project(self) -> None:
        self._wav_sequence_table.blockSignals(True)
        self._wav_sequence_table.setRowCount(0)
        self._single_wav_row = -1
        base = self._project_path.parent if self._project_path is not None else None
        for s in self._project.scenes:
            audio = (s.audio_relpath or "").strip()
            if not audio:
                continue
            src = Path(audio)
            if base is not None and not src.is_absolute():
                src = base / src
            self._append_wav_sequence_row(src)
            row = self._wav_sequence_table.rowCount() - 1
            sub_rel = self._decode_wav_subtitle_relpath(s.notes)
            self._set_wav_subtitle_relpath_for_row(row, sub_rel)
            self._set_wav_reference_lyrics_for_row(row, self._decode_wav_reference_lyrics(s.notes))
            intro = self._decode_wav_subtitle_intro_sec(s.notes)
            off = self._decode_wav_subtitle_offset_sec(s.notes)
            if intro is not None:
                self._set_wav_subtitle_intro_sec_for_row(row, intro)
            if off is not None:
                self._set_wav_subtitle_offset_sec_for_row(row, off)
            self._set_wav_intro_title_for_row(row, self._decode_wav_intro_title(s.notes))
            idur = self._decode_wav_intro_title_duration_sec(s.notes)
            if idur is not None:
                self._set_wav_intro_title_duration_sec_for_row(row, idur)
            combo = self._wav_sequence_table.cellWidget(row, 2)
            if isinstance(combo, QComboBox):
                combo.blockSignals(True)
                combo.setCurrentText(s.transition or "fade")
                combo.blockSignals(False)
            self._wav_sequence_table.setItem(row, 3, QTableWidgetItem(s.image_relpath))
            self._set_wav_segments_for_row(row, self._decode_segments_from_scene_notes(s.notes))
        if not (self._project.export_final_relpath or "").strip():
            self._edit_wav_sequence_out.setText("export/wav_sequence.mp4")
        else:
            self._edit_wav_sequence_out.setText(self._project.export_final_relpath.strip())
        self._wav_sequence_table.blockSignals(False)

    def _refresh_wav_mode_tree(self, focus: tuple[str, int]) -> None:
        if self._project.project_kind != PROJECT_KIND_WAV_SEQUENCE:
            return
        self._rebuild_nav_tree()
        self._apply_tree_focus(focus)

    def _sync_wav_list_nav_tree(self) -> None:
        """WAV 목록 테이블과 좌측 트리를 맞추고, 삭제된 항목은 트리에서도 제거합니다."""
        if self._project.project_kind != PROJECT_KIND_WAV_SEQUENCE:
            return
        self._rebuild_nav_tree()
        self._apply_tree_focus(("wav_sequence", 0))

    def _on_wav_sequence_row_activated(self, row: int, _col: int) -> None:
        if self._project.project_kind != PROJECT_KIND_WAV_SEQUENCE:
            return
        self._refresh_wav_mode_tree(("wav", row))

    def _wav_nav_label(self, row: int) -> str:
        raw = self._wav_cell_text(row, 0).strip()
        if not raw:
            return f"WAV {row + 1}"
        try:
            p = Path(raw)
            stem = p.stem.strip()
            return stem or f"WAV {row + 1}"
        except (TypeError, ValueError):
            return f"WAV {row + 1}"

    def _load_single_wav_form(self, row: int) -> None:
        if row < 0 or row >= self._wav_sequence_table.rowCount():
            return
        self._label_wav_one_title.setText(self._wav_nav_label(row))
        self._edit_single_wav_path.blockSignals(True)
        self._edit_single_wav_path.setText(self._wav_cell_text(row, 0))
        self._edit_single_wav_path.blockSignals(False)
        self._btn_single_wav_play.setText("재생")
        p = Path(self._edit_single_wav_path.text().strip())
        if p.is_file():
            self._set_wav_preview_source(p)
        else:
            self._slider_single_wav_pos.setRange(0, 0)
            self._slider_single_wav_pos.setValue(0)
            self._label_single_wav_pos.setText("00:00.000 / 00:00.000")
        self._refresh_single_wav_segments_table()
        self._sync_single_wav_subtitle_path_edit()
        self._edit_single_wav_reference_lyrics.blockSignals(True)
        self._edit_single_wav_reference_lyrics.setPlainText(
            self._wav_reference_lyrics_for_row(row)
        )
        self._edit_single_wav_reference_lyrics.blockSignals(False)
        self._spin_single_wav_subtitle_intro.blockSignals(True)
        self._spin_single_wav_subtitle_intro.setValue(self._wav_subtitle_intro_sec_for_row(row))
        self._spin_single_wav_subtitle_intro.blockSignals(False)
        self._spin_single_wav_subtitle_offset.blockSignals(True)
        self._spin_single_wav_subtitle_offset.setValue(self._wav_subtitle_offset_sec_for_row(row))
        self._spin_single_wav_subtitle_offset.blockSignals(False)
        self._edit_single_wav_intro_title.blockSignals(True)
        self._edit_single_wav_intro_title.setText(self._wav_intro_title_for_row(row))
        self._edit_single_wav_intro_title.blockSignals(False)
        self._spin_single_wav_intro_title_duration.blockSignals(True)
        self._spin_single_wav_intro_title_duration.setValue(
            self._wav_intro_title_duration_sec_for_row(row)
        )
        self._spin_single_wav_intro_title_duration.blockSignals(False)
        self._load_selected_segment_editor()

    def _wav_cell_text(self, row: int, col: int) -> str:
        it = self._wav_sequence_table.item(row, col)
        return it.text() if it else ""

    def _on_single_wav_field_changed(self, *_args: object) -> None:
        if self._single_wav_row < 0:
            return
        if self._syncing_ui:
            return
        self._flush_single_wav_to_table()
        self._mark_dirty()

    def _load_selected_segment_editor(self) -> None:
        row = self._table_single_wav_segments.currentRow()
        if row < 0:
            self._edit_single_wav_seg_prompt.blockSignals(True)
            self._edit_single_wav_seg_prompt.setPlainText("")
            self._edit_single_wav_seg_prompt.blockSignals(False)
            self._combo_single_wav_transition.blockSignals(True)
            self._combo_single_wav_transition.setCurrentText("fade")
            self._combo_single_wav_transition.blockSignals(False)
            self._edit_single_wav_image.blockSignals(True)
            self._edit_single_wav_image.setText("")
            self._edit_single_wav_image.blockSignals(False)
            return
        self._edit_single_wav_seg_prompt.blockSignals(True)
        self._edit_single_wav_seg_prompt.setPlainText(
            (self._table_single_wav_segments.item(row, 3).text() if self._table_single_wav_segments.item(row, 3) else "")
        )
        self._edit_single_wav_seg_prompt.blockSignals(False)
        tr = (self._table_single_wav_segments.item(row, 4).text() if self._table_single_wav_segments.item(row, 4) else "").strip() or "fade"
        self._combo_single_wav_transition.blockSignals(True)
        self._combo_single_wav_transition.setCurrentText(tr)
        self._combo_single_wav_transition.blockSignals(False)
        self._edit_single_wav_image.blockSignals(True)
        self._edit_single_wav_image.setText(
            (self._table_single_wav_segments.item(row, 5).text() if self._table_single_wav_segments.item(row, 5) else "")
        )
        self._edit_single_wav_image.blockSignals(False)

    def _on_selected_segment_editor_changed(self, *_args: object) -> None:
        row = self._table_single_wav_segments.currentRow()
        if self._single_wav_row < 0 or row < 0:
            return
        self._table_single_wav_segments.blockSignals(True)
        self._table_single_wav_segments.setItem(row, 3, QTableWidgetItem(self._edit_single_wav_seg_prompt.toPlainText()))
        self._table_single_wav_segments.setItem(
            row, 4, QTableWidgetItem(self._combo_single_wav_transition.currentText().strip() or "fade")
        )
        self._table_single_wav_segments.setItem(row, 5, QTableWidgetItem(self._edit_single_wav_image.text().strip()))
        self._table_single_wav_segments.blockSignals(False)
        self._on_single_wav_segments_table_changed(row, 3)

    def _on_delete_selected_wav_segment(self) -> None:
        if self._single_wav_row < 0:
            return
        sel = self._table_single_wav_segments.currentRow()
        if sel < 0:
            QMessageBox.information(self, "구간 삭제", "삭제할 구간을 선택하세요.")
            return
        segs = self._wav_segments_for_row(self._single_wav_row)
        if sel >= len(segs):
            return
        if len(segs) <= 1:
            QMessageBox.information(self, "구간 삭제", "최소 1개의 구간은 필요합니다.")
            return
        removed = segs.pop(sel)
        if sel > 0:
            prev = segs[sel - 1]
            prev["end_sec"] = float(removed["end_sec"])
            if sel < len(segs):
                segs[sel]["start_sec"] = float(prev["end_sec"])
        else:
            segs[0]["start_sec"] = 0.0
        self._set_wav_segments_for_row(self._single_wav_row, segs)
        self._refresh_single_wav_segments_table()
        new_sel = min(sel, len(segs) - 1)
        if new_sel >= 0:
            self._table_single_wav_segments.setCurrentCell(new_sel, 3)
        self._mark_dirty()

    def _ensure_wav_preview_player(self) -> bool:
        if self._wav_preview_player is not None:
            return True
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except ModuleNotFoundError:
            QMessageBox.warning(
                self,
                "WAV 재생",
                "현재 실행 중인 Python 환경에서 PySide6.QtMultimedia를 사용할 수 없습니다.\n"
                "권장 실행: .venv\\Scripts\\python.exe main.py",
            )
            return False
        self._wav_preview_player = QMediaPlayer(self)
        self._wav_preview_audio_output = QAudioOutput(self)
        self._wav_preview_player.setAudioOutput(self._wav_preview_audio_output)
        self._wav_preview_audio_output.setVolume(0.9)
        self._wav_preview_player.durationChanged.connect(self._on_single_wav_duration_changed)
        self._wav_preview_player.positionChanged.connect(self._on_single_wav_position_changed)
        self._wav_preview_player.playbackStateChanged.connect(self._on_single_wav_playback_state_changed)
        return True

    def _format_wav_msec(self, ms: int) -> str:
        if ms < 0:
            ms = 0
        sec = ms // 1000
        mm = sec // 60
        ss = sec % 60
        msec = ms % 1000
        return f"{mm:02d}:{ss:02d}.{msec:03d}"

    def _set_wav_preview_source(self, wav_path: Path) -> None:
        if not self._ensure_wav_preview_player():
            return
        abs_src = wav_path.resolve().as_posix()
        if self._wav_preview_source == abs_src:
            return
        self._wav_preview_player.stop()
        self._btn_single_wav_play.setText("재생")
        self._wav_preview_player.setSource(QUrl.fromLocalFile(str(wav_path.resolve())))
        self._wav_preview_source = abs_src

    def _on_single_wav_duration_changed(self, duration_ms: int) -> None:
        self._slider_single_wav_pos.setRange(0, max(0, duration_ms))
        pos = 0 if self._wav_preview_player is None else self._wav_preview_player.position()
        self._label_single_wav_pos.setText(
            f"{self._format_wav_msec(pos)} / {self._format_wav_msec(duration_ms)}"
        )
        if self._single_wav_row >= 0:
            it = self._wav_sequence_table.item(self._single_wav_row, 0)
            if it is not None and duration_ms > 0:
                self._set_wav_duration_cache_row(self._single_wav_row, duration_ms / 1000.0)
        self._refresh_wav_boundary_bar()

    def _on_single_wav_position_changed(self, pos_ms: int) -> None:
        if not self._wav_slider_dragging:
            self._slider_single_wav_pos.setValue(pos_ms)
        dur = 0 if self._wav_preview_player is None else self._wav_preview_player.duration()
        self._label_single_wav_pos.setText(
            f"{self._format_wav_msec(pos_ms)} / {self._format_wav_msec(dur)}"
        )

    def _on_single_wav_playback_state_changed(self, _state: object) -> None:
        if self._wav_preview_player is None:
            self._btn_single_wav_play.setText("재생")
            return
        from PySide6.QtMultimedia import QMediaPlayer

        if self._wav_preview_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._btn_single_wav_play.setText("일시정지")
        else:
            self._btn_single_wav_play.setText("재생")

    def _on_single_wav_slider_pressed(self) -> None:
        self._wav_slider_dragging = True

    def _on_single_wav_slider_released(self) -> None:
        if self._wav_preview_player is not None:
            self._wav_preview_player.setPosition(self._slider_single_wav_pos.value())
        self._wav_slider_dragging = False

    def _on_toggle_single_wav_playback(self) -> None:
        if self._single_wav_row < 0:
            QMessageBox.information(self, "WAV 재생", "왼쪽에서 재생할 WAV 항목을 선택하세요.")
            return
        if not self._ensure_wav_preview_player():
            return
        from PySide6.QtMultimedia import QMediaPlayer

        wav_path = Path(self._edit_single_wav_path.text().strip())
        if not wav_path.is_file():
            QMessageBox.warning(self, "WAV 재생", f"파일을 찾을 수 없습니다:\n{wav_path}")
            return
        self._set_wav_preview_source(wav_path)
        state = self._wav_preview_player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._wav_preview_player.pause()
        else:
            self._wav_preview_player.play()

    def _current_wav_playback_sec(self) -> float | None:
        if self._wav_preview_player is None:
            return None
        return max(0.0, self._wav_preview_player.position() / 1000.0)

    def _wav_segments_for_row(self, row: int) -> list[dict[str, object]]:
        it = self._wav_sequence_table.item(row, 0)
        if it is None:
            return []
        raw = it.data(_ROLE_WAV_SEGMENTS)
        if not isinstance(raw, str) or not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        out: list[dict[str, object]] = []
        for seg in parsed:
            if not isinstance(seg, dict):
                continue
            try:
                st = float(seg.get("start_sec", 0.0))
                en = float(seg.get("end_sec", 0.0))
            except (TypeError, ValueError):
                continue
            if en <= st:
                continue
            out.append(
                {
                    "start_sec": st,
                    "end_sec": en,
                    "image_prompt": str(
                        seg.get("image_prompt", seg.get("narration", ""))
                    ).strip(),
                    "transition": str(seg.get("transition", "fade")).strip() or "fade",
                    "image_relpath": str(seg.get("image_relpath", "")).strip(),
                }
            )
        out.sort(key=lambda x: float(x["start_sec"]))
        if out:
            return out
        duration = self._wav_duration_for_row(row)
        if duration <= 0:
            return []
        return [
            {
                "start_sec": 0.0,
                "end_sec": duration,
                "image_prompt": "",
                "transition": "fade",
                "image_relpath": "",
            }
        ]

    def _wav_duration_for_row(self, row: int) -> float:
        it = self._wav_sequence_table.item(row, 0)
        if it is None:
            return 0.0
        cached = it.data(_ROLE_WAV_DURATION_SEC)
        try:
            c = float(cached) if cached is not None else 0.0
        except (TypeError, ValueError):
            c = 0.0
        if c > 0:
            return c
        wav_path = Path((it.text() or "").strip())
        if not wav_path.is_file():
            return 0.0
        try:
            d = float(ffprobe_duration_seconds(wav_path.resolve()))
        except (FfprobeError, OSError, ValueError):
            d = 0.0
        if d > 0:
            self._set_wav_duration_cache_row(row, d)
        return d

    def _set_wav_duration_cache_row(self, row: int, sec: float) -> None:
        it = self._wav_sequence_table.item(row, 0)
        if it is None:
            return
        prev = self._wav_sequence_table.blockSignals(True)
        try:
            it.setData(_ROLE_WAV_DURATION_SEC, sec)
        finally:
            self._wav_sequence_table.blockSignals(prev)

    def _rescale_segments_if_too_short(
        self, segments: list[dict[str, object]], target_duration_sec: float
    ) -> list[dict[str, object]]:
        if not segments or target_duration_sec <= 15:
            return segments
        try:
            max_end = max(float(s.get("end_sec", 0.0)) for s in segments)
        except (TypeError, ValueError):
            return segments
        if max_end <= 0 or max_end >= target_duration_sec * 0.85:
            return segments
        if max_end >= target_duration_sec * 0.5:
            return segments
        scale = target_duration_sec / max_end
        self.append_log(
            f"구간 시간 보정: Gemini 응답이 짧아 전체 길이({target_duration_sec:.1f}초)에 맞게 {scale:.1f}배 확대"
        )
        scaled: list[dict[str, object]] = []
        for seg in sorted(segments, key=lambda x: float(x.get("start_sec", 0.0))):
            item = dict(seg)
            try:
                item["start_sec"] = float(item.get("start_sec", 0.0)) * scale
                item["end_sec"] = float(item.get("end_sec", 0.0)) * scale
            except (TypeError, ValueError):
                scaled.append(item)
                continue
            scaled.append(item)
        if scaled:
            scaled[0]["start_sec"] = 0.0
            scaled[-1]["end_sec"] = target_duration_sec
        return scaled

    def _normalize_segments_to_row_duration(self, row: int, segments: list[dict[str, object]]) -> list[dict[str, object]]:
        if not segments:
            return []
        dur = self._wav_duration_for_row(row)
        if dur <= 0:
            out = [dict(s) for s in segments]
            out.sort(key=lambda x: float(x.get("start_sec", 0.0)))
            return out
        segments = self._rescale_segments_if_too_short(segments, dur)
        out: list[dict[str, object]] = []
        eps = 0.04
        for seg in sorted(segments, key=lambda x: float(x.get("start_sec", 0.0))):
            try:
                st = float(seg.get("start_sec", 0.0))
                en = float(seg.get("end_sec", 0.0))
            except (TypeError, ValueError):
                continue
            if out:
                prev_end = float(out[-1]["end_sec"])
                st = max(st, prev_end)
            st = max(0.0, min(st, dur))
            en = max(st, min(en, dur))
            if en - st < eps:
                continue
            norm = dict(seg)
            norm["start_sec"] = st
            norm["end_sec"] = en
            out.append(norm)
        if out and dur > 0:
            out[-1]["end_sec"] = dur
        return out

    def _set_wav_segments_for_row(self, row: int, segments: list[dict[str, object]]) -> None:
        it = self._ensure_wav_row_item(row)
        norm_segments = self._normalize_segments_to_row_duration(row, segments)
        prev = self._wav_sequence_table.blockSignals(True)
        try:
            it.setData(_ROLE_WAV_SEGMENTS, json.dumps(norm_segments, ensure_ascii=False))
        finally:
            self._wav_sequence_table.blockSignals(prev)

    def _segment_color(self, idx: int) -> str:
        palette = ["#f28b82", "#fbbc04", "#fff475", "#ccff90", "#a7ffeb", "#cbf0f8", "#aecbfa", "#d7aefb"]
        return palette[idx % len(palette)]

    def _refresh_single_wav_segments_table(self) -> None:
        if self._single_wav_row < 0:
            self._table_single_wav_segments.setRowCount(0)
            self._refresh_wav_boundary_bar()
            return
        segs = self._wav_segments_for_row(self._single_wav_row)
        cur_row = self._table_single_wav_segments.currentRow()
        self._table_single_wav_segments.blockSignals(True)
        self._table_single_wav_segments.setRowCount(0)
        for i, seg in enumerate(segs):
            r = self._table_single_wav_segments.rowCount()
            self._table_single_wav_segments.insertRow(r)
            color_item = QTableWidgetItem("")
            color_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            color_item.setBackground(QColor(self._segment_color(i)))
            self._table_single_wav_segments.setItem(r, 0, color_item)
            self._table_single_wav_segments.setItem(r, 1, QTableWidgetItem(f"{float(seg['start_sec']):.3f}"))
            self._table_single_wav_segments.setItem(r, 2, QTableWidgetItem(f"{float(seg['end_sec']):.3f}"))
            self._table_single_wav_segments.setItem(r, 3, QTableWidgetItem(str(seg.get("image_prompt", ""))))
            self._table_single_wav_segments.setItem(r, 4, QTableWidgetItem(str(seg.get("transition", "fade"))))
            self._table_single_wav_segments.setItem(r, 5, QTableWidgetItem(str(seg.get("image_relpath", ""))))
        self._table_single_wav_segments.blockSignals(False)
        if segs:
            keep = cur_row if 0 <= cur_row < len(segs) else 0
            self._table_single_wav_segments.setCurrentCell(keep, 2)
        self._refresh_wav_boundary_bar()

    def _refresh_wav_boundary_bar(self) -> None:
        segs = self._wav_segments_for_row(self._single_wav_row) if self._single_wav_row >= 0 else []
        idx = self._table_single_wav_segments.currentRow()
        duration_sec = 0.0
        if self._wav_preview_player is not None:
            duration_sec = max(0.0, self._wav_preview_player.duration() / 1000.0)
        if duration_sec <= 0 and segs:
            duration_sec = float(segs[-1]["end_sec"])
        if duration_sec <= 0:
            duration_sec = 1.0
        selected_boundary = idx if 0 <= idx < len(segs) else -1
        self._wav_boundary_bar_updating = True
        self._wav_boundary_bar.set_data(segs, duration_sec, selected_boundary)
        self._wav_boundary_bar_updating = False

    def _on_single_wav_segments_table_changed(self, _row: int, _col: int) -> None:
        if self._single_wav_row < 0:
            return
        segs = self._collect_segments_from_single_table()
        segs.sort(key=lambda x: float(x["start_sec"]))
        self._set_wav_segments_for_row(self._single_wav_row, segs)
        self._sync_wav_row_summary_from_segments(self._single_wav_row, segs)
        self._refresh_wav_boundary_bar()
        self._mark_dirty()

    def _sync_wav_row_summary_from_segments(self, row: int, segs: list[dict[str, object]]) -> None:
        if row < 0 or row >= self._wav_sequence_table.rowCount():
            return
        if not segs:
            self._set_wav_table_cell_text(row, 1, "")
            combo = self._wav_sequence_table.cellWidget(row, 2)
            if isinstance(combo, QComboBox):
                combo.setCurrentText("fade")
            self._set_wav_table_cell_text(row, 3, "")
            return
        first = segs[0]
        sub = self._wav_subtitle_relpath_for_row(row)
        self._set_wav_table_cell_text(row, 1, sub or "(없음)")
        combo = self._wav_sequence_table.cellWidget(row, 2)
        if isinstance(combo, QComboBox):
            combo.setCurrentText(str(first.get("transition", "fade")).strip() or "fade")
        self._set_wav_table_cell_text(row, 3, str(first.get("image_relpath", "")).strip())

    def _on_single_wav_segment_selection_changed(self, _cur_row: int, _cur_col: int, _prev_row: int, _prev_col: int) -> None:
        self._refresh_wav_boundary_bar()
        self._load_selected_segment_editor()

    def _on_wav_boundary_bar_dragged(self, boundary_idx: int, sec: float) -> None:
        if self._wav_boundary_bar_updating or self._single_wav_row < 0:
            return
        segs = self._wav_segments_for_row(self._single_wav_row)
        if boundary_idx < 0 or boundary_idx >= len(segs):
            return
        cur = segs[boundary_idx]
        start = float(cur["start_sec"])
        if sec <= start:
            sec = start + 0.01
        if boundary_idx + 1 < len(segs):
            next_end = float(segs[boundary_idx + 1]["end_sec"])
            if sec >= next_end:
                sec = next_end - 0.01
            segs[boundary_idx + 1]["start_sec"] = sec
        cur["end_sec"] = sec
        self._set_wav_segments_for_row(self._single_wav_row, segs)
        self._refresh_single_wav_segments_table()
        self._table_single_wav_segments.setCurrentCell(boundary_idx, 2)
        self._mark_dirty()

    def _collect_segments_from_single_table(self) -> list[dict[str, object]]:
        segs: list[dict[str, object]] = []
        for r in range(self._table_single_wav_segments.rowCount()):
            try:
                st = float(
                    (
                        self._table_single_wav_segments.item(r, 1).text()
                        if self._table_single_wav_segments.item(r, 1)
                        else "0"
                    ).strip()
                )
                en = float(
                    (
                        self._table_single_wav_segments.item(r, 2).text()
                        if self._table_single_wav_segments.item(r, 2)
                        else "0"
                    ).strip()
                )
            except ValueError:
                continue
            if en <= st:
                continue
            prompt = (
                self._table_single_wav_segments.item(r, 3).text()
                if self._table_single_wav_segments.item(r, 3)
                else ""
            ).strip()
            tr = (
                self._table_single_wav_segments.item(r, 4).text()
                if self._table_single_wav_segments.item(r, 4)
                else ""
            ).strip() or "fade"
            img = (
                self._table_single_wav_segments.item(r, 5).text()
                if self._table_single_wav_segments.item(r, 5)
                else ""
            ).strip()
            segs.append(
                {
                    "start_sec": st,
                    "end_sec": en,
                    "image_prompt": prompt,
                    "transition": tr,
                    "image_relpath": img,
                }
            )
        return segs

    def _wav_has_existing_analysis(self, row: int) -> tuple[bool, str]:
        parts: list[str] = []
        segs = self._wav_segments_for_row(row)
        if segs:
            parts.append(f"구간 {len(segs)}개")
        sub_rel = self._wav_subtitle_relpath_for_row(row)
        if sub_rel:
            path = self._subtitle_abs_path_for_row(row)
            if path is not None and path.is_file():
                parts.append("자막 SRT")
            elif sub_rel not in ("(없음)", "-"):
                parts.append("자막 경로")
        if self._wav_intro_title_for_row(row).strip():
            parts.append("인트로 제목")
        if not parts:
            return False, ""
        return True, ", ".join(parts)

    def _ask_reference_lyrics_for_analysis(self, *, song_name: str) -> str | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("원곡 가사")
        dlg.resize(560, 420)
        lay = QVBoxLayout(dlg)
        lay.addWidget(
            QLabel(
                f"「{song_name}」\n\n"
                "원곡 가사를 입력하면 음악 분석(자막·구간·이미지)이 더 정확해집니다.\n"
                "가사 전문을 붙여 넣은 뒤 「분석 시작」을 누르세요."
            )
        )
        edit = QPlainTextEdit()
        edit.setPlaceholderText("원곡 가사 전문")
        edit.setMinimumHeight(240)
        lay.addWidget(edit, stretch=1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("분석 시작")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        text = edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(dlg, "원곡 가사", "원곡 가사를 입력하세요.")
            return self._ask_reference_lyrics_for_analysis(song_name=song_name)
        return text

    def _apply_intro_title_from_filename(self, row: int) -> None:
        wav_path = Path(self._wav_cell_text(row, 0).strip())
        title = wav_path.stem.strip() if wav_path.name else self._wav_nav_label(row)
        self._set_wav_intro_title_for_row(row, title)
        if row == self._single_wav_row:
            self._edit_single_wav_intro_title.setText(title)

    def _on_music_analysis(self) -> None:
        if self._single_wav_row < 0:
            QMessageBox.information(self, "음악 분석", "왼쪽에서 WAV 항목을 먼저 선택하세요.")
            return
        if self._any_worker_running():
            QMessageBox.information(self, "음악 분석", "다른 작업이 실행 중입니다.")
            return
        row = self._single_wav_row
        wav_path = Path(self._edit_single_wav_path.text().strip())
        if not wav_path.is_file():
            QMessageBox.warning(self, "음악 분석", f"WAV 파일을 찾을 수 없습니다:\n{wav_path}")
            return
        self._flush_single_wav_to_table()
        song_name = self._wav_nav_label(row)
        reference_lyrics = self._wav_reference_lyrics_for_row(row)
        if not reference_lyrics.strip():
            reference_lyrics = self._edit_single_wav_reference_lyrics.toPlainText().strip()
        if not reference_lyrics.strip():
            entered = self._ask_reference_lyrics_for_analysis(song_name=song_name)
            if entered is None:
                return
            reference_lyrics = entered
            self._set_wav_reference_lyrics_for_row(row, reference_lyrics)
            self._edit_single_wav_reference_lyrics.setPlainText(reference_lyrics)
            self._mark_dirty()

        has_existing, summary = self._wav_has_existing_analysis(row)
        if has_existing:
            ret = QMessageBox.question(
                self,
                "음악 분석",
                f"「{song_name}」에 이미 편집 정보가 있습니다 ({summary}).\n\n"
                "음악 분석을 실행하면 기존 자막·구간·이미지 정보가 새 결과로 갱신됩니다.\n"
                "계속할까요?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return

        self._settings.sync()
        gemini_key = str(self._settings.value("gemini/api_key", "")).strip()
        gemini_model = str(self._settings.value("gemini/model", DEFAULT_GEMINI_MODEL)).strip() or DEFAULT_GEMINI_MODEL
        if "image" in gemini_model.lower():
            gemini_model = DEFAULT_GEMINI_MODEL
        gemini_image_model = (
            str(self._settings.value("gemini/image_model", DEFAULT_GEMINI_IMAGE_MODEL)).strip()
            or DEFAULT_GEMINI_IMAGE_MODEL
        )
        if not gemini_key and not (os.environ.get("GEMINI_API_KEY") or "").strip():
            QMessageBox.warning(
                self,
                "음악 분석",
                "환경 설정에서 Gemini API 키를 입력하거나 GEMINI_API_KEY를 설정하세요.",
            )
            return

        self._apply_intro_title_from_filename(row)
        out_rel = self._edit_single_wav_subtitle_path.text().strip().replace("\\", "/")
        if not out_rel or out_rel in ("(없음)", "-"):
            out_rel = self._default_wav_subtitle_relpath(wav_path)
            self._set_wav_subtitle_relpath_for_row(row, out_rel)
            self._sync_single_wav_subtitle_path_edit()

        project_parent = (
            self._project_path.parent
            if self._project_path is not None
            else (self.project_root() / "_session_project")
        )
        project_parent.mkdir(parents=True, exist_ok=True)

        intro_title = self._wav_intro_title_for_row(row)
        self._music_analysis_worker = MusicAnalysisWorker(
            wav_path=wav_path,
            reference_lyrics=reference_lyrics,
            project_parent=project_parent,
            output_relpath=out_rel,
            max_line_chars=self._subtitle_max_chars(),
            intro_title=intro_title,
            intro_title_duration_sec=self._wav_intro_title_duration_sec_for_row(row),
            intro_skip_sec=self._wav_subtitle_intro_sec_for_row(row),
            subtitle_offset_sec=self._wav_subtitle_offset_sec_for_row(row),
            gemini_api_key=gemini_key,
            gemini_model=gemini_model,
            gemini_image_model=gemini_image_model,
            resolution=self._project.resolution,
            vocal_retime_with_lyrics=self._vocal_retime_with_lyrics_enabled(),
            wav_segments=self._wav_segments_for_row(row),
        )
        self._music_analysis_worker.log_line.connect(self._append_job_log)
        self._music_analysis_worker.progress.connect(self._update_job_progress_percent)
        self._music_analysis_worker.succeeded.connect(self._on_music_analysis_succeeded)
        self._music_analysis_worker.failed.connect(self._on_music_analysis_failed)
        self._music_analysis_worker.finished.connect(self._on_music_analysis_thread_finished)
        self._music_analysis_target_row = row
        self._start_job("음악 분석")
        self._set_busy(True)
        self._arm_job_cancel(self._music_analysis_worker)
        self._music_analysis_worker.start()

    def _on_music_analysis_succeeded(self, payload: object) -> None:
        row = self._music_analysis_target_row if self._music_analysis_target_row >= 0 else self._single_wav_row
        if row < 0 or not isinstance(payload, dict):
            return
        rel = str(payload.get("subtitle_relpath", "")).strip().replace("\\", "/")
        if rel:
            self._set_wav_subtitle_relpath_for_row(row, rel)
        segments = payload.get("segments")
        if isinstance(segments, list):
            segs = self._payload_to_wav_segments(segments)
            if segs:
                self._apply_segments_to_target_row(row, segs)
        intro = str(payload.get("intro_title", "")).strip()
        if intro:
            self._set_wav_intro_title_for_row(row, intro)
        if row == self._single_wav_row:
            self._sync_single_wav_subtitle_path_edit()
            self._edit_single_wav_intro_title.setText(self._wav_intro_title_for_row(row))
        self._mark_dirty()
        self._append_job_log("음악 분석 완료")
        self._finish_job_bar(success=True)
        self.statusBar().showMessage("음악 분석이 완료되었습니다.", 8000)

    def _on_music_analysis_failed(self, msg: str) -> None:
        self._append_job_log(msg)
        self._finish_job_bar(success=False)
        if not self._is_job_cancel_message(msg):
            QMessageBox.warning(self, "음악 분석 실패", msg)

    def _on_music_analysis_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        self._music_analysis_target_row = -1
        if self._music_analysis_worker is not None:
            self._music_analysis_worker.deleteLater()
            self._music_analysis_worker = None

    def _on_add_wav_segment_marker(self) -> None:
        if self._single_wav_row < 0:
            QMessageBox.information(self, "구간 마커", "왼쪽에서 WAV 항목을 선택하세요.")
            return
        sec = self._current_wav_playback_sec()
        if sec is None:
            QMessageBox.information(self, "구간 마커", "먼저 WAV를 재생한 뒤 마커를 찍으세요.")
            return
        segs = self._wav_segments_for_row(self._single_wav_row)
        if not segs:
            QMessageBox.warning(self, "구간 마커", "오디오 길이를 확인할 수 없어 구간을 만들 수 없습니다.")
            return
        target_idx = -1
        for i, seg in enumerate(segs):
            st = float(seg["start_sec"])
            en = float(seg["end_sec"])
            if st + 0.01 < sec < en - 0.01:
                target_idx = i
                break
            if abs(sec - st) <= 0.01 or abs(sec - en) <= 0.01:
                QMessageBox.information(self, "구간 마커", "이미 해당 위치에 경계가 있습니다.")
                return
        if target_idx < 0:
            QMessageBox.warning(self, "구간 마커", "현재 위치가 유효한 구간 내부가 아닙니다.")
            return
        cur = dict(segs[target_idx])
        left = dict(cur)
        right = dict(cur)
        left["end_sec"] = sec
        right["start_sec"] = sec
        right["image_prompt"] = ""
        segs[target_idx : target_idx + 1] = [left, right]
        self._set_wav_segments_for_row(self._single_wav_row, segs)
        self._refresh_single_wav_segments_table()
        self._table_single_wav_segments.setCurrentCell(target_idx + 1, 3)
        self._mark_dirty()

    def _on_auto_segment_single_wav_with_gemini(self) -> None:
        if self._single_wav_row < 0:
            QMessageBox.information(self, "Gemini 구간 분석", "왼쪽에서 WAV 항목을 먼저 선택하세요.")
            return
        if self._any_worker_running():
            QMessageBox.information(self, "Gemini 구간 분석", "다른 작업이 실행 중입니다.")
            return
        wav_path = Path(self._edit_single_wav_path.text().strip())
        if not wav_path.is_file():
            QMessageBox.warning(self, "Gemini 구간 분석", f"WAV 파일을 찾을 수 없습니다:\n{wav_path}")
            return
        self._settings.sync()
        gemini_key = str(self._settings.value("gemini/api_key", "")).strip()
        gemini_model = str(self._settings.value("gemini/model", DEFAULT_GEMINI_MODEL)).strip() or DEFAULT_GEMINI_MODEL
        if "image" in gemini_model.lower():
            self.append_log(
                f"Gemini 분석 모델이 이미지 계열({gemini_model})이라 분석용 기본 모델({DEFAULT_GEMINI_MODEL})로 대체합니다."
            )
            gemini_model = DEFAULT_GEMINI_MODEL
        if not gemini_key and not (os.environ.get("GEMINI_API_KEY") or "").strip():
            QMessageBox.warning(
                self,
                "Gemini 구간 분석",
                "환경 설정에서 Gemini API 키를 입력하거나 환경 변수 GEMINI_API_KEY를 설정하세요.",
            )
            return
        project_parent = self._project_path.parent if self._project_path is not None else (self.project_root() / "_session_project")
        project_parent.mkdir(parents=True, exist_ok=True)
        self._flush_single_wav_to_table()
        reference_lyrics = self._edit_single_wav_reference_lyrics.toPlainText().strip()
        self._wav_segments_worker = GeminiWavSegmentsWorker(
            wav_path=wav_path,
            api_key=gemini_key,
            model=gemini_model,
            generate_images=False,
            image_api_key=gemini_key,
            image_model=DEFAULT_GEMINI_IMAGE_MODEL,
            resolution=self._project.resolution,
            project_parent=project_parent,
            reference_lyrics=reference_lyrics,
        )
        self._wav_segments_worker.log_line.connect(self._append_job_log)
        self._wav_segments_worker.progress.connect(self._update_job_progress_percent)
        self._wav_segments_worker.updated.connect(self._on_wav_segments_updated)
        self._wav_segments_worker.succeeded.connect(self._on_wav_segments_succeeded)
        self._wav_segments_worker.failed.connect(self._on_wav_segments_failed)
        self._wav_segments_worker.finished.connect(self._on_wav_segments_thread_finished)
        self._wav_segments_target_row = self._single_wav_row
        self._start_job("자동 구간 분석")
        self._set_busy(True)
        self._arm_job_cancel(self._wav_segments_worker)
        self._wav_segments_worker.start()

    def _ask_stt_refine_mode(self, *, has_reference_lyrics: bool) -> str | None:
        if has_reference_lyrics:
            box = QMessageBox(self)
            box.setWindowTitle("자막 생성")
            box.setText(
                "STT 후 교정 방식을 선택하세요.\n\n"
                "· 원곡 가사로 교정: 가사 교정 후 보컬 구간에만 자막 배치(인트로·간주 무자막)\n"
                "· 맞춤법만: 띄어쓰기·맞춤법만 수정\n"
                "· STT만: Whisper 결과 그대로 저장"
            )
            btn_lyrics = box.addButton("원곡 가사로 교정", QMessageBox.ButtonRole.AcceptRole)
            btn_polish = box.addButton("맞춤법만", QMessageBox.ButtonRole.ActionRole)
            btn_none = box.addButton("STT만", QMessageBox.ButtonRole.ActionRole)
            box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is None or clicked.text() == "취소":
                return None
            if clicked == btn_lyrics:
                return "lyrics"
            if clicked == btn_polish:
                return "polish"
            return "none"
        ret = QMessageBox.question(
            self,
            "자막 생성",
            "STT 후 LLM으로 맞춤법만 다듬을까요?\n(시간값은 유지)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Cancel:
            return None
        return "polish" if ret == QMessageBox.StandardButton.Yes else "none"

    def _start_stt_worker(
        self,
        *,
        wav_path: Path,
        refine_mode: str,
        reference_lyrics: str,
    ) -> None:
        self._settings.sync()
        gemini_key = str(self._settings.value("gemini/api_key", "")).strip()
        gemini_model = str(self._settings.value("gemini/model", DEFAULT_GEMINI_MODEL)).strip() or DEFAULT_GEMINI_MODEL
        if refine_mode in ("lyrics", "polish", "align_only") and not gemini_key and not (
            os.environ.get("GEMINI_API_KEY") or ""
        ).strip():
            QMessageBox.warning(
                self,
                "자막",
                "LLM 교정을 사용하려면 Gemini API 키가 필요합니다.\n환경 설정 또는 GEMINI_API_KEY를 설정하세요.",
            )
            return
        project_parent = self._project_path.parent if self._project_path is not None else (
            self.project_root() / "_session_project"
        )
        project_parent.mkdir(parents=True, exist_ok=True)
        self._flush_single_wav_to_table()
        out_rel = self._edit_single_wav_subtitle_path.text().strip().replace("\\", "/")
        if not out_rel or out_rel in ("(없음)", "-"):
            out_rel = self._default_wav_subtitle_relpath(wav_path)
            self._set_wav_subtitle_relpath_for_row(self._single_wav_row, out_rel)
        intro_sec = self._wav_subtitle_intro_sec_for_row(self._single_wav_row)
        offset_sec = self._wav_subtitle_offset_sec_for_row(self._single_wav_row)
        self._stt_wav_segments_worker = SttWavSegmentsWorker(
            wav_path=wav_path,
            language="ko",
            refine_mode=refine_mode,  # type: ignore[arg-type]
            reference_lyrics=reference_lyrics,
            gemini_api_key=gemini_key,
            gemini_model=gemini_model,
            project_parent=project_parent,
            output_relpath=out_rel,
            max_line_chars=self._subtitle_max_chars(),
            intro_skip_sec=intro_sec,
            subtitle_offset_sec=offset_sec,
            intro_title=self._wav_intro_title_for_row(self._single_wav_row),
            intro_title_duration_sec=self._wav_intro_title_duration_sec_for_row(
                self._single_wav_row
            ),
            vocal_retime_with_lyrics=self._vocal_retime_with_lyrics_enabled(),
            wav_segments=self._wav_segments_for_row(self._single_wav_row),
        )
        self._stt_wav_segments_worker.log_line.connect(self._append_job_log)
        self._stt_wav_segments_worker.progress.connect(self._update_job_progress_percent)
        self._stt_wav_segments_worker.succeeded.connect(self._on_stt_segments_succeeded)
        self._stt_wav_segments_worker.failed.connect(self._on_stt_segments_failed)
        self._stt_wav_segments_worker.finished.connect(self._on_stt_segments_thread_finished)
        self._stt_segments_target_row = self._single_wav_row
        self._set_busy(True)
        self._arm_job_cancel(self._stt_wav_segments_worker)
        self._stt_wav_segments_worker.start()

    def _on_generate_stt_segments(self) -> None:
        if self._single_wav_row < 0:
            QMessageBox.information(self, "자막 생성", "왼쪽에서 WAV 항목을 먼저 선택하세요.")
            return
        if self._any_worker_running():
            QMessageBox.information(self, "자막 생성", "다른 작업이 실행 중입니다.")
            return
        wav_path = Path(self._edit_single_wav_path.text().strip())
        if not wav_path.is_file():
            QMessageBox.warning(self, "자막 생성", f"WAV 파일을 찾을 수 없습니다:\n{wav_path}")
            return
        reference_lyrics = self._edit_single_wav_reference_lyrics.toPlainText().strip()
        refine_mode = self._ask_stt_refine_mode(has_reference_lyrics=bool(reference_lyrics))
        if refine_mode is None:
            return
        self._start_job("자막 생성")
        self._start_stt_worker(
            wav_path=wav_path,
            refine_mode=refine_mode,
            reference_lyrics=reference_lyrics,
        )

    def _on_stt_segments_succeeded(self, payload: object) -> None:
        row = self._stt_segments_target_row if self._stt_segments_target_row >= 0 else self._single_wav_row
        if row < 0:
            return
        if not isinstance(payload, dict):
            QMessageBox.warning(self, "자막 생성", "응답 형식이 올바르지 않습니다.")
            return
        rel = str(payload.get("subtitle_relpath", "")).strip().replace("\\", "/")
        if not rel:
            QMessageBox.warning(self, "자막 생성", "자막 파일 경로가 비어 있습니다.")
            return
        cue_count = int(payload.get("cue_count", 0) or 0)
        self._set_wav_subtitle_relpath_for_row(row, rel)
        if row == self._single_wav_row:
            self._sync_single_wav_subtitle_path_edit()
        self._mark_dirty()
        self._append_job_log(f"자막 생성 완료: {rel} ({cue_count}구간)")
        self._finish_job_bar(success=True)
        self.statusBar().showMessage(f"자막 SRT 저장: {rel}", 8000)

    def _on_stt_segments_failed(self, msg: str) -> None:
        self._append_job_log(msg)
        self._finish_job_bar(success=False)
        if not self._is_job_cancel_message(msg):
            QMessageBox.warning(self, "자막 생성 실패", msg)

    def _on_stt_segments_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        self._stt_segments_target_row = -1
        if self._stt_wav_segments_worker is not None:
            self._stt_wav_segments_worker.deleteLater()
            self._stt_wav_segments_worker = None

    def _wav_segments_row_target(self) -> int:
        if self._wav_segments_target_row >= 0:
            return self._wav_segments_target_row
        return self._single_wav_row

    def _payload_to_wav_segments(self, payload: object) -> list[dict[str, object]]:
        if not isinstance(payload, list):
            return []
        segs: list[dict[str, object]] = []
        base_transition = self._combo_single_wav_transition.currentText().strip() or "fade"
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                st = float(item.get("start_sec"))
                en = float(item.get("end_sec"))
            except (TypeError, ValueError):
                continue
            if en <= st:
                continue
            prompt = str(
                item.get(
                    "image_prompt",
                    item.get("narration", item.get("naration", item.get("label", ""))),
                )
            ).strip()
            segs.append(
                {
                    "start_sec": st,
                    "end_sec": en,
                    "image_prompt": prompt,
                    "transition": str(item.get("transition", base_transition)).strip() or base_transition,
                    # 실시간 업데이트에서는 image_relpath가 없는 구간에 기본 이미지를 채우지 않는다.
                    # (첫 번째 생성 이미지가 아직 미생성 구간으로 퍼지는 현상 방지)
                    "image_relpath": str(item.get("image_relpath", "")).strip(),
                }
            )
        segs.sort(key=lambda x: float(x["start_sec"]))
        return segs

    def _apply_segments_to_target_row(self, row: int, segs: list[dict[str, object]]) -> None:
        if row < 0 or row >= self._wav_sequence_table.rowCount():
            return
        cur_row = self._table_single_wav_segments.currentRow()
        self._set_wav_segments_for_row(row, segs)
        if self._single_wav_row == row:
            self._refresh_single_wav_segments_table()
            if segs:
                keep = cur_row if 0 <= cur_row < len(segs) else min(0, len(segs) - 1)
                if keep >= 0:
                    self._table_single_wav_segments.setCurrentCell(keep, 3)
        self._mark_dirty()

    def _on_wav_segments_updated(self, payload: object) -> None:
        row = self._wav_segments_row_target()
        if row < 0:
            return
        segs = self._payload_to_wav_segments(payload)
        if not segs:
            return
        self._apply_segments_to_target_row(row, segs)

    def _on_wav_segments_succeeded(self, payload: object) -> None:
        row = self._wav_segments_row_target()
        if row < 0:
            return
        if not isinstance(payload, list):
            QMessageBox.warning(self, "Gemini 구간 분석", "응답 형식이 올바르지 않습니다.")
            return
        segs = self._payload_to_wav_segments(payload)
        if not segs:
            QMessageBox.warning(self, "Gemini 구간 분석", "유효한 구간 결과가 없습니다.")
            return
        self._apply_segments_to_target_row(row, segs)
        self._append_job_log(f"자동 구간 분석 완료: {len(segs)}개")
        self._finish_job_bar(success=True)
        self.statusBar().showMessage(f"Gemini 구간 분석 완료: {len(segs)}개", 8000)

    def _on_wav_segments_failed(self, msg: str) -> None:
        self._append_job_log(msg)
        self._finish_job_bar(success=False)
        if not self._is_job_cancel_message(msg):
            QMessageBox.warning(self, "Gemini 구간 분석 실패", msg)

    def _on_wav_segments_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        self._wav_segments_target_row = -1
        if self._wav_segments_worker is not None:
            self._wav_segments_worker.deleteLater()
            self._wav_segments_worker = None

    def _on_auto_generate_segment_images(self) -> None:
        if self._single_wav_row < 0:
            QMessageBox.information(self, "구간 이미지", "왼쪽에서 WAV 항목을 먼저 선택하세요.")
            return
        if self._any_worker_running():
            QMessageBox.information(self, "구간 이미지", "다른 작업이 실행 중입니다.")
            return
        wav_path = Path(self._edit_single_wav_path.text().strip())
        if not wav_path.is_file():
            QMessageBox.warning(self, "구간 이미지", f"WAV 파일을 찾을 수 없습니다:\n{wav_path}")
            return
        segs = self._collect_segments_from_single_table()
        if not segs:
            QMessageBox.warning(self, "구간 이미지", "먼저 구간을 생성하세요.")
            return
        self._settings.sync()
        gemini_key = str(self._settings.value("gemini/api_key", "")).strip()
        image_model = str(self._settings.value("gemini/image_model", DEFAULT_GEMINI_IMAGE_MODEL)).strip() or DEFAULT_GEMINI_IMAGE_MODEL
        if not gemini_key and not (os.environ.get("GEMINI_API_KEY") or "").strip():
            QMessageBox.warning(
                self,
                "구간 이미지",
                "환경 설정에서 Gemini API 키를 입력하거나 환경 변수 GEMINI_API_KEY를 설정하세요.",
            )
            return
        project_parent = self._project_path.parent if self._project_path is not None else (self.project_root() / "_session_project")
        project_parent.mkdir(parents=True, exist_ok=True)
        self._wav_segment_images_worker = GeminiWavSegmentImagesWorker(
            wav_path=wav_path,
            segments=segs,
            api_key=gemini_key,
            image_model=image_model,
            resolution=self._project.resolution,
            project_parent=project_parent,
        )
        self._wav_segment_images_worker.log_line.connect(self._append_job_log)
        self._wav_segment_images_worker.progress.connect(self._update_job_progress_percent)
        self._wav_segment_images_worker.updated.connect(self._on_wav_segment_images_updated)
        self._wav_segment_images_worker.succeeded.connect(self._on_wav_segment_images_succeeded)
        self._wav_segment_images_worker.failed.connect(self._on_wav_segment_images_failed)
        self._wav_segment_images_worker.finished.connect(self._on_wav_segment_images_thread_finished)
        self._wav_segment_images_target_row = self._single_wav_row
        self._start_job("구간별 이미지 자동 생성")
        self._set_busy(True)
        self._arm_job_cancel(self._wav_segment_images_worker)
        self._wav_segment_images_worker.start()

    def _wav_segment_images_row_target(self) -> int:
        if self._wav_segment_images_target_row >= 0:
            return self._wav_segment_images_target_row
        return self._single_wav_row

    def _on_wav_segment_images_updated(self, payload: object) -> None:
        row = self._wav_segment_images_row_target()
        if row < 0:
            return
        segs = self._payload_to_wav_segments(payload)
        if not segs:
            return
        self._apply_segments_to_target_row(row, segs)

    def _on_wav_segment_images_succeeded(self, payload: object) -> None:
        row = self._wav_segment_images_row_target()
        if row < 0:
            return
        if not isinstance(payload, list):
            QMessageBox.warning(self, "구간 이미지", "응답 형식이 올바르지 않습니다.")
            return
        segs = self._payload_to_wav_segments(payload)
        if not segs:
            QMessageBox.warning(self, "구간 이미지", "생성된 이미지 결과가 없습니다.")
            return
        self._apply_segments_to_target_row(row, segs)
        self._append_job_log(f"구간별 이미지 자동 생성 완료: {len(segs)}개")
        self._finish_job_bar(success=True)
        self.statusBar().showMessage(f"구간별 이미지 자동 생성 완료: {len(segs)}개", 8000)

    def _on_wav_segment_images_failed(self, msg: str) -> None:
        self._append_job_log(msg)
        self._finish_job_bar(success=False)
        if not self._is_job_cancel_message(msg):
            QMessageBox.warning(self, "구간 이미지 생성 실패", msg)

    def _on_wav_segment_images_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        self._wav_segment_images_target_row = -1
        if self._wav_segment_images_worker is not None:
            self._wav_segment_images_worker.deleteLater()
            self._wav_segment_images_worker = None

    def _flush_single_wav_to_table(self) -> None:
        r = self._single_wav_row
        if r < 0 or r >= self._wav_sequence_table.rowCount():
            return
        old_path = self._wav_cell_text(r, 0).strip()
        new_path = self._edit_single_wav_path.text().strip()
        it = self._ensure_wav_row_item(r)
        self._wav_sequence_table.blockSignals(True)
        it.setText(new_path)
        self._wav_sequence_table.blockSignals(False)
        if old_path != new_path:
            # WAV가 바뀌면 이전 길이 캐시는 무효다. 자막 경로·구간 메타는 유지한다.
            it.setData(_ROLE_WAV_DURATION_SEC, None)
            self._set_wav_duration_cache_row(r, 0.0)
        segs = self._collect_segments_from_single_table()
        segs.sort(key=lambda x: float(x["start_sec"]))
        self._set_wav_segments_for_row(r, segs)
        self._set_wav_reference_lyrics_for_row(
            r, self._edit_single_wav_reference_lyrics.toPlainText().strip()
        )
        rel = self._edit_single_wav_subtitle_path.text().strip()
        if rel and rel not in ("(없음)", "-"):
            self._set_wav_subtitle_relpath_for_row(r, rel)
        self._set_wav_subtitle_intro_sec_for_row(r, self._spin_single_wav_subtitle_intro.value())
        self._set_wav_subtitle_offset_sec_for_row(r, self._spin_single_wav_subtitle_offset.value())
        self._set_wav_intro_title_for_row(r, self._edit_single_wav_intro_title.text())
        self._set_wav_intro_title_duration_sec_for_row(
            r, self._spin_single_wav_intro_title_duration.value()
        )

    def _on_single_wav_subtitle_path_changed(self) -> None:
        if self._single_wav_row < 0 or self._syncing_ui:
            return
        rel = self._edit_single_wav_subtitle_path.text().strip().replace("\\", "/")
        self._set_wav_subtitle_relpath_for_row(self._single_wav_row, rel)
        self._mark_dirty()

    def _browse_single_wav_subtitle_path(self) -> None:
        if self._single_wav_row < 0:
            QMessageBox.information(self, "자막 저장 위치", "왼쪽에서 WAV 항목을 먼저 선택하세요.")
            return
        parent = self._project_parent_dir()
        parent.mkdir(parents=True, exist_ok=True)
        wav_path = Path(self._edit_single_wav_path.text().strip())
        default_rel = self._edit_single_wav_subtitle_path.text().strip()
        if not default_rel or default_rel in ("(없음)", "-"):
            default_rel = (
                self._default_wav_subtitle_relpath(wav_path)
                if wav_path.is_file()
                else "subs/wav_subtitles/subtitle.srt"
            )
        start = str((parent / default_rel).resolve().parent)
        start_name = Path(default_rel).name
        path, _ = QFileDialog.getSaveFileName(
            self,
            "자막 SRT 저장 위치",
            str(Path(start) / start_name),
            "자막 (*.srt);;모든 파일 (*.*)",
        )
        if not path:
            return
        rel = self._path_to_project_relpath(Path(path))
        self._edit_single_wav_subtitle_path.setText(rel)
        self._set_wav_subtitle_relpath_for_row(self._single_wav_row, rel)
        self._mark_dirty()

    def _on_default_single_wav_subtitle_path(self) -> None:
        if self._single_wav_row < 0:
            return
        wav_path = Path(self._edit_single_wav_path.text().strip())
        if not wav_path.is_file():
            QMessageBox.warning(self, "자막 저장 위치", "WAV 파일을 먼저 지정하세요.")
            return
        rel = self._default_wav_subtitle_relpath(wav_path)
        self._edit_single_wav_subtitle_path.setText(rel)
        self._set_wav_subtitle_relpath_for_row(self._single_wav_row, rel)
        self._mark_dirty()

    def _browse_single_wav_file(self) -> None:
        start = str(self._project_path.parent) if self._project_path is not None else str(self.project_root())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "WAV 파일",
            start,
            "WAV (*.wav);;모든 파일 (*.*)",
        )
        if not path:
            return
        self._edit_single_wav_path.setText(Path(path).as_posix())
        self._on_single_wav_field_changed()

    def _browse_single_wav_image(self) -> None:
        start = str(self._project_path.parent) if self._project_path is not None else str(self.project_root())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "씬 배경 이미지",
            start,
            "이미지 (*.png *.jpg *.jpeg *.webp);;모든 파일 (*.*)",
        )
        if not path:
            return
        p = Path(path).resolve()
        if self._project_path is not None:
            parent = self._project_path.parent.resolve()
            try:
                rel = str(p.relative_to(parent)).replace("\\", "/")
            except ValueError:
                rel = p.as_posix()
        else:
            rel = p.as_posix()
        self._edit_single_wav_image.setText(rel)
        self._on_selected_segment_editor_changed()

    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "정보",
            "<p><b>콘텐츠 제작</b></p>"
            "<p>프로젝트는 JSON(storyboard)으로 저장됩니다. "
            "왼쪽 트리에서 「프롬프트」「씬」「로그」를 전환하고, 상단 막대에서 현재 선택에 맞는 작업을 실행합니다. "
            "「환경 설정」에서 LLM·Piper·STT·자막·이미지 모델을 지정합니다.</p>",
        )

    def append_log(self, text: str) -> None:
        self._log.appendPlainText(text.rstrip())

    def _start_job(self, title: str) -> None:
        self._job_panel.setVisible(True)
        self._label_job_title.setText(title)
        self._job_log.clear()
        self._append_job_log(f"{title} 시작…")
        self._job_progress.setRange(0, 0)
        self._job_progress.setValue(0)
        self._job_progress.setFormat("준비 중…")
        self._btn_job_cancel.setEnabled(self._job_cancel_worker is not None)

    def _arm_job_cancel(self, worker: QThread | None) -> None:
        from app.workers.cancellable_thread import CancellableQThread

        self._job_cancel_worker = worker
        self._btn_job_cancel.setEnabled(isinstance(worker, CancellableQThread))

    def _clear_job_cancel(self) -> None:
        self._job_cancel_worker = None
        self._btn_job_cancel.setEnabled(False)

    def _on_cancel_job(self) -> None:
        from app.workers.cancellable_thread import CancellableQThread

        worker = self._job_cancel_worker
        if not isinstance(worker, CancellableQThread) or not worker.isRunning():
            return
        worker.request_cancel()
        self._append_job_log("작업 중지 요청…")
        self._btn_job_cancel.setEnabled(False)
        self.statusBar().showMessage("작업 중지 중…", 5000)

    @staticmethod
    def _is_job_cancel_message(msg: str) -> bool:
        return "중지" in str(msg or "")

    def _append_job_log(self, text: str) -> None:
        line = text.rstrip()
        if not line:
            return
        self._job_log.appendPlainText(line)
        sb = self._job_log.verticalScrollBar()
        sb.setValue(sb.maximum())
        self.append_log(line)

    def _update_job_progress(self, value: int, maximum: int = 100, *, message: str = "") -> None:
        if maximum <= 0:
            self._job_progress.setRange(0, 0)
            self._job_progress.setFormat(message or "진행 중…")
        else:
            v = max(0, min(int(value), int(maximum)))
            pct = int(round((v / maximum) * 100.0))
            self._job_progress.setRange(0, maximum)
            self._job_progress.setValue(v)
            fmt = f"{pct}%"
            if message:
                fmt += f" — {message}"
            self._job_progress.setFormat(fmt)
        if message:
            self.statusBar().showMessage(message, 4000)

    def _update_job_progress_percent(self, percent: int, message: str = "") -> None:
        self._update_job_progress(percent, 100, message=message)

    def _finish_job_bar(self, *, success: bool) -> None:
        if success:
            self._job_progress.setRange(0, 100)
            self._job_progress.setValue(100)
            self._job_progress.setFormat("완료")
        else:
            self._job_progress.setRange(0, 100)
            self._job_progress.setValue(0)
            self._job_progress.setFormat("중단/실패")

    def _show_log_panel(self) -> None:
        self._stack_right.setCurrentIndex(_RIGHT_LOG)

    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def _video_project_parent(self) -> Path | None:
        if self._project_path is None:
            return None
        return self._project_path.parent

    def _browse_background_image(self) -> None:
        if self._project_path is None:
            QMessageBox.warning(self, "배경 이미지", "먼저 프로젝트를 저장해 경로 기준을 잡으세요.")
            return
        start = str(self._project_path.parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "배경 이미지",
            start,
            "이미지 (*.png *.jpg *.jpeg *.webp);;모든 파일 (*.*)",
        )
        if not path:
            return
        p = Path(path).resolve()
        parent = self._project_path.parent.resolve()
        try:
            rel = str(p.relative_to(parent)).replace("\\", "/")
        except ValueError:
            rel = p.as_posix()
        self._edit_bg_image.setText(rel)
        self._mark_dirty()

    def _browse_bgm(self) -> None:
        if self._project_path is None:
            QMessageBox.warning(self, "BGM", "먼저 프로젝트를 저장해 경로 기준을 잡으세요.")
            return
        start = str(self._project_path.parent)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "BGM 오디오",
            start,
            "오디오 (*.mp3 *.wav *.m4a *.flac *.ogg);;모든 파일 (*.*)",
        )
        if not path:
            return
        p = Path(path).resolve()
        parent = self._project_path.parent.resolve()
        try:
            rel = str(p.relative_to(parent)).replace("\\", "/")
        except ValueError:
            rel = p.as_posix()
        self._edit_bgm.setText(rel)
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        sender = self.sender()
        sender_name = ""
        sender_type = ""
        if sender is not None:
            sender_type = type(sender).__name__
            sender_name = (sender.objectName() or "").strip() if hasattr(sender, "objectName") else ""
        nav = self._nav_snapshot()
        sender_desc = f"{sender_type}({sender_name})" if sender_name else (sender_type or "None")
        if self._syncing_ui:
            logger.debug("dirty 무시(syncing_ui=True) sender=%s nav=%s", sender_desc, nav)
            return
        if self._dirty:
            logger.debug("dirty 무시(이미 dirty=True) sender=%s nav=%s", sender_desc, nav)
            return
        self._dirty = True
        stack_text = "".join(traceback.format_stack(limit=7)[:-1]).rstrip()
        logger.debug("dirty 설정됨 sender=%s nav=%s\n%s", sender_desc, nav, stack_text)
        self._update_window_title()

    def _set_clean_after_save(self) -> None:
        self._dirty = False
        self._update_window_title()

    def _update_window_title(self) -> None:
        name = self._project_path.name if self._project_path else "새 프로젝트"
        star = " *" if self._dirty else ""
        self.setWindowTitle(f"콘텐츠 제작 — {name}{star}")

    def _sync_ui_from_project(
        self,
        *,
        mark_clean: bool,
        tree_focus: tuple[str, int] | None = None,
    ) -> None:
        focus = tree_focus if tree_focus is not None else self._nav_snapshot()
        self._syncing_ui = True
        try:
            self._prompt.blockSignals(True)
            self._prompt.setPlainText(self._project.prompt_ko)
            self._prompt.blockSignals(False)

            self._spin_target_minutes.blockSignals(True)
            self._spin_target_minutes.setValue(max(1, int(self._project.target_minutes)))
            self._spin_target_minutes.blockSignals(False)

            self._combo_resolution.blockSignals(True)
            idx = self._combo_resolution.findText(self._project.resolution)
            self._combo_resolution.setCurrentIndex(idx if idx >= 0 else 0)
            self._combo_resolution.blockSignals(False)

            self._spin_fps.blockSignals(True)
            self._spin_fps.setValue(max(1, int(self._project.fps)))
            self._spin_fps.blockSignals(False)

            rel = (self._project.merged_srt_relpath or "").strip()
            self._label_merged_srt.setText(
                f"병합 SRT: {rel}" if rel else "병합 SRT: (아직 없음)"
            )

            self._edit_bg_image.blockSignals(True)
            self._edit_bg_image.setText(self._project.background_image_relpath)
            self._edit_bg_image.blockSignals(False)
            self._edit_bgm.blockSignals(True)
            self._edit_bgm.setText(self._project.bgm_relpath)
            self._edit_bgm.blockSignals(False)
            self._spin_bgm_volume.blockSignals(True)
            self._spin_bgm_volume.setValue(max(1, min(50, int(self._project.bgm_volume_percent))))
            self._spin_bgm_volume.blockSignals(False)
            ep = (self._project.export_final_relpath or "").strip() or EXPORT_FINAL_REL
            if self._project_path:
                abs_p = (self._project_path.parent / ep).as_posix()
                self._label_export_final.setText(f"최종 MP4(상대): {ep}\n절대: {abs_p}")
            else:
                self._label_export_final.setText(f"최종 MP4(상대): {ep}\n(프로젝트 저장 후 절대 경로 표시)")

            self._scene_table.blockSignals(True)
            self._scene_table.setRowCount(0)
            for s in self._project.scenes:
                r = self._scene_table.rowCount()
                self._scene_table.insertRow(r)
                id_item = QTableWidgetItem(str(s.scene_id))
                id_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                self._scene_table.setItem(r, 0, id_item)
                self._scene_table.setItem(r, 1, QTableWidgetItem(s.narration_ko))
                self._scene_table.setItem(r, 2, QTableWidgetItem(s.visual_prompt_ko))
                self._scene_table.setItem(r, 3, QTableWidgetItem(s.transition))
                self._scene_table.setItem(r, 4, QTableWidgetItem(s.image_relpath))
            self._scene_table.blockSignals(False)

            self._sync_wav_sequence_table_from_project()
            self._video_production_panel.set_project(self._project, self._video_project_parent())

            self._rebuild_nav_tree()
            fk = focus[0]
            fr = focus[1]
            if self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION:
                if fk not in ("video_production", "log"):
                    focus = ("video_production", 0)
            elif self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
                if fk == "wav":
                    fr = max(0, min(fr, max(0, self._wav_sequence_table.rowCount() - 1)))
                    focus = ("wav", fr)
                elif fk not in ("wav_sequence", "log"):
                    focus = ("wav_sequence", 0)
            else:
                if fk == "scene":
                    fr = max(0, min(fr, max(0, len(self._project.scenes) - 1)))
                    focus = ("scene", fr)
                elif fk == "wav_sequence":
                    focus = ("prompt", 0)
            self._apply_tree_focus(focus)

            if self._stack_right.currentIndex() == _RIGHT_SCENE_ONE and self._single_scene_row >= 0:
                self._load_single_scene_form(self._single_scene_row)
            if self._stack_right.currentIndex() == _RIGHT_WAV_ONE and self._single_wav_row >= 0:
                self._load_single_wav_form(self._single_wav_row)

            self._update_context_bar_visibility()
            self._update_mode_banner()

            if mark_clean:
                self._set_clean_after_save()
        finally:
            self._syncing_ui = False

    def _collect_from_ui(self) -> StoryProject:
        was_syncing = self._syncing_ui
        self._syncing_ui = True
        try:
            return self._collect_from_ui_impl()
        finally:
            self._syncing_ui = was_syncing

    def _collect_from_ui_impl(self) -> StoryProject:
        self._flush_single_scene_to_table()
        self._flush_single_wav_to_table()
        scenes: list[Scene] = []
        if self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION:
            scenes = list(self._project.scenes)
            self._video_production_panel.apply_to_project(self._project)
            scenes = list(self._project.scenes)
        elif self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            rows = self._collect_wav_sequence_rows(allow_empty=True) or []
            for r in range(self._wav_sequence_table.rowCount()):
                it0 = self._wav_sequence_table.item(r, 0)
                p = (it0.text() if it0 else "").strip()
                if not p:
                    continue
                segments = self._wav_segments_for_row(r)
                sub_rel = self._wav_subtitle_relpath_for_row(r)
                ref_lyrics = self._wav_reference_lyrics_for_row(r)
                img_item = self._wav_sequence_table.item(r, 3)
                img = (img_item.text() if img_item else "").strip()
                scenes.append(
                    Scene(
                        scene_id=len(scenes) + 1,
                        narration_ko=" ",
                        visual_prompt_ko="",
                        transition=self._transition_at_wav_sequence(r) or "fade",
                        notes=self._encode_wav_scene_notes(
                            segments,
                            sub_rel,
                            reference_lyrics=ref_lyrics,
                            subtitle_intro_sec=self._wav_subtitle_intro_sec_for_row(r),
                            subtitle_offset_sec=self._wav_subtitle_offset_sec_for_row(r),
                            intro_title=self._wav_intro_title_for_row(r),
                            intro_title_duration_sec=self._wav_intro_title_duration_sec_for_row(r),
                        ),
                        audio_relpath=Path(p).as_posix(),
                        image_relpath=img,
                    )
                )
        else:
            old = self._project.scenes
            for r in range(self._scene_table.rowCount()):
                nar = self._cell_text(r, 1)
                vis = self._cell_text(r, 2)
                tr = self._cell_text(r, 3) or "fade"
                image_relpath = self._cell_text(r, 4).strip()
                audio = ""
                if r < len(old):
                    audio = old[r].audio_relpath or ""
                scenes.append(
                    Scene(
                        scene_id=r + 1,
                        narration_ko=nar,
                        visual_prompt_ko=vis,
                        transition=tr,
                        audio_relpath=audio,
                        image_relpath=image_relpath,
                    )
                )
        res_text = self._combo_resolution.currentText()
        prompt_text = self._prompt.toPlainText()
        target_minutes = int(self._spin_target_minutes.value())
        fps = int(self._spin_fps.value())
        export_rel = self._project.export_final_relpath
        if self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            export_rel = self._edit_wav_sequence_out.text().strip()
        elif self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION:
            prompt_text = self._project.prompt_ko
            target_minutes = int(self._project.target_minutes)
            res_text = self._project.resolution
            fps = int(self._project.fps)
            export_rel = self._project.export_final_relpath
        return StoryProject(
            prompt_ko=prompt_text,
            project_kind=self._project.project_kind,
            target_minutes=target_minutes,
            resolution=res_text,
            fps=fps,
            merged_srt_relpath=self._project.merged_srt_relpath,
            export_final_relpath=export_rel,
            background_image_relpath=self._edit_bg_image.text().strip(),
            bgm_relpath=self._edit_bgm.text().strip(),
            bgm_volume_percent=int(self._spin_bgm_volume.value()),
            scenes=scenes,
        )

    def _cell_text(self, row: int, col: int) -> str:
        it = self._scene_table.item(row, col)
        return it.text() if it else ""

    def _on_add_scene(self) -> None:
        self._project = self._collect_from_ui()
        next_id = len(self._project.scenes) + 1
        self._project.scenes.append(Scene(scene_id=next_id, narration_ko="", visual_prompt_ko=""))
        self._project.renumber_scene_ids()
        last = max(0, len(self._project.scenes) - 1)
        self._sync_ui_from_project(mark_clean=False, tree_focus=("scene", last))
        self._mark_dirty()

    def _on_remove_scene(self) -> None:
        row = self._scene_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "씬 삭제", "표에서 삭제할 씬 행을 선택하세요.")
            return
        self._project = self._collect_from_ui()
        if len(self._project.scenes) <= 1:
            QMessageBox.warning(self, "씬 삭제", "최소 1개의 씬이 필요합니다.")
            return
        del self._project.scenes[row]
        self._project.renumber_scene_ids()
        new_row = min(row, max(0, len(self._project.scenes) - 1))
        self._sync_ui_from_project(mark_clean=False, tree_focus=("scene", new_row))
        self._mark_dirty()

    def _on_new_project(self) -> None:
        if not self._confirm_discard_unsaved():
            return
        dlg = NewProjectModeDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        picked = QFileDialog.getExistingDirectory(
            self,
            "새 프로젝트 폴더 선택/생성",
            str(self.project_root()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not picked:
            return
        project_dir = Path(picked)
        project_path = project_dir / "storyboard.json"
        if project_path.exists():
            answer = QMessageBox.question(
                self,
                "새 프로젝트",
                f"선택한 폴더에 storyboard.json 파일이 이미 있습니다.\n덮어쓰고 새 프로젝트를 만들까요?\n\n{project_path}",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._project = StoryProject.empty_default(project_kind=dlg.selected_kind())
        self._project_path = project_path
        try:
            self._project.save_json(project_path)
        except OSError as e:
            QMessageBox.critical(self, "새 프로젝트 저장 실패", str(e))
            self._project_path = None
            return
        self._remember_last_project_path(project_path)
        focus = ("video_production", 0) if self._project.project_kind == PROJECT_KIND_VIDEO_PRODUCTION else ("prompt", 0)
        self._sync_ui_from_project(mark_clean=True, tree_focus=focus)
        self.statusBar().showMessage(f"새 프로젝트: {project_path}", 5000)

    def _on_open(self) -> None:
        if not self._confirm_discard_unsaved():
            return
        path, _ = QFileDialog.getOpenFileName(self, "프로젝트 열기", str(self.project_root()), _JSON_FILTER)
        if not path:
            return
        p = Path(path)
        try:
            self._project = StoryProject.load_json(p)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.critical(self, "열기 실패", str(e))
            return
        self._project_path = p
        self._remember_last_project_path(p)
        self._sync_ui_from_project(mark_clean=True, tree_focus=("prompt", 0))
        self.statusBar().showMessage(f"열음: {p}", 5000)

    def _on_save(self) -> None:
        if self._project_path is None:
            self._on_save_as()
            return
        self._save_to_path(self._project_path)

    def _on_save_as(self) -> None:
        start = str(self._project_path) if self._project_path else str(self.project_root() / "storyboard.json")
        path, _ = QFileDialog.getSaveFileName(self, "다른 이름으로 저장", start, _JSON_FILTER)
        if not path:
            return
        self._save_to_path(Path(path))

    def _save_to_path(self, path: Path) -> None:
        snap = self._nav_snapshot()
        self._project = self._collect_from_ui()
        self._project.renumber_scene_ids()
        try:
            self._project.save_json(path)
        except OSError as e:
            QMessageBox.critical(self, "저장 실패", str(e))
            return
        self._project_path = path
        self._remember_last_project_path(path)
        self._sync_ui_from_project(mark_clean=True, tree_focus=snap)

    def _any_worker_running(self) -> bool:
        v = self._validate_worker is not None and self._validate_worker.isRunning()
        o = self._llm_worker is not None and self._llm_worker.isRunning()
        t = self._tts_worker is not None and self._tts_worker.isRunning()
        s = self._subtitle_worker is not None and self._subtitle_worker.isRunning()
        r = self._render_worker is not None and self._render_worker.isRunning()
        g = self._scene_image_worker is not None and self._scene_image_worker.isRunning()
        w = self._wav_segments_worker is not None and self._wav_segments_worker.isRunning()
        wi = self._wav_segment_images_worker is not None and self._wav_segment_images_worker.isRunning()
        stt = self._stt_wav_segments_worker is not None and self._stt_wav_segments_worker.isRunning()
        ma = self._music_analysis_worker is not None and self._music_analysis_worker.isRunning()
        return v or o or t or s or r or g or w or wi or stt or ma

    def _read_llm_params(self) -> tuple[str, str, str, str, str]:
        s = self._settings
        prov = str(s.value("llm/provider", "ollama"))
        ollama_base = str(s.value("ollama/base_url", "http://127.0.0.1:11434")).strip() or "http://127.0.0.1:11434"
        ollama_model = str(s.value("ollama/model", "llama3.2")).strip()
        gemini_key = str(s.value("gemini/api_key", "")).strip()
        gemini_model = str(s.value("gemini/model", DEFAULT_GEMINI_MODEL)).strip() or DEFAULT_GEMINI_MODEL
        return prov, ollama_base, ollama_model, gemini_key, gemini_model

    def _on_validate(self) -> None:
        if self._any_worker_running():
            QMessageBox.information(self, "환경 검증", "다른 작업이 실행 중입니다.")
            return
        self._project = self._collect_from_ui()
        self._validate_worker = PipelineWorker(self._project, self._project_path)
        self._validate_worker.log_line.connect(self._append_job_log)
        self._validate_worker.finished_ok.connect(self._on_validate_finished)
        self._validate_worker.finished.connect(self._on_validate_thread_finished)
        self._start_job("환경 검증")
        self._set_busy(True)
        self._arm_job_cancel(self._validate_worker)
        self._validate_worker.start()

    def _on_validate_finished(self, ok: bool) -> None:
        self._finish_job_bar(success=ok)
        self.statusBar().showMessage("환경 검증 완료" if ok else "환경 검증에서 문제가 있습니다.", 5000)

    def _on_validate_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        if self._validate_worker is not None:
            self._validate_worker.deleteLater()
            self._validate_worker = None

    def _on_generate_scenes(self) -> None:
        if self._any_worker_running():
            QMessageBox.information(self, "씬 생성", "다른 작업이 실행 중입니다.")
            return
        if self._nav_snapshot()[0] != "prompt":
            QMessageBox.information(self, "씬 생성", "왼쪽에서 「프롬프트」를 선택한 뒤 실행하세요.")
            return
        self._project = self._collect_from_ui()
        if not self._project.prompt_ko.strip():
            QMessageBox.warning(self, "씬 생성", "프롬프트 내용을 먼저 입력하세요.")
            return
        provider, ollama_base, ollama_model, gemini_key, gemini_model = self._read_llm_params()
        if provider == "ollama":
            if not ollama_model:
                QMessageBox.warning(self, "씬 생성", "환경 설정에서 Ollama 모델 이름을 입력하세요.")
                return
        else:
            if not gemini_model:
                QMessageBox.warning(
                    self,
                    "씬 생성",
                    f"환경 설정에서 Gemini 모델을 지정하세요. (기본: {DEFAULT_GEMINI_MODEL})",
                )
                return
            if not gemini_key and not (os.environ.get("GEMINI_API_KEY") or "").strip():
                QMessageBox.warning(
                    self,
                    "씬 생성",
                    "환경 설정에 Gemini API 키를 입력하거나 환경 변수 GEMINI_API_KEY를 설정하세요.",
                )
                return
        ret = QMessageBox.question(
            self,
            "씬 생성",
            "생성된 씬으로 기존 씬 표를 모두 바꿉니다. 계속할까요?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if ret != QMessageBox.StandardButton.Ok:
            return
        self._settings.sync()
        self._llm_worker = LlmScenesWorker(
            provider=provider,  # type: ignore[arg-type]
            prompt_ko=self._project.prompt_ko,
            target_minutes=self._project.target_minutes,
            resolution=self._project.resolution,
            fps=self._project.fps,
            ollama_base_url=ollama_base,
            ollama_model=ollama_model,
            gemini_api_key=gemini_key,
            gemini_model=gemini_model,
        )
        self._llm_worker.log_line.connect(self._append_job_log)
        self._llm_worker.succeeded.connect(self._on_llm_scenes_succeeded)
        self._llm_worker.failed.connect(self._on_llm_scenes_failed)
        self._llm_worker.finished.connect(self._on_llm_thread_finished)
        self._start_job("씬 생성 (LLM)")
        self._set_busy(True)
        self._arm_job_cancel(self._llm_worker)
        self._llm_worker.start()

    def _on_llm_scenes_succeeded(self, scenes: object) -> None:
        if not isinstance(scenes, list):
            return
        out: list[Scene] = [s for s in scenes if isinstance(s, Scene)]
        if not out:
            QMessageBox.warning(self, "씬 생성", "유효한 씬이 없습니다.")
            return
        self._project = self._collect_from_ui()
        self._project.scenes = out
        self._project.renumber_scene_ids()
        self._sync_ui_from_project(mark_clean=False, tree_focus=("scenes", 0))
        self._mark_dirty()
        self._finish_job_bar(success=True)
        self.statusBar().showMessage("LLM 씬 생성 완료. 저장하여 프로젝트에 반영하세요.", 8000)

    def _on_llm_scenes_failed(self, msg: str) -> None:
        self._append_job_log(msg)
        self._finish_job_bar(success=False)
        if not self._is_job_cancel_message(msg):
            QMessageBox.critical(self, "씬 생성 실패", msg)

    def _on_llm_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        if self._llm_worker is not None:
            self._llm_worker.deleteLater()
            self._llm_worker = None

    def _on_generate_scene_images(self) -> None:
        if self._any_worker_running():
            QMessageBox.information(self, "씬 배경 이미지", "다른 작업이 실행 중입니다.")
            return
        self._project = self._collect_from_ui()
        if self._project_path is None:
            QMessageBox.warning(
                self,
                "씬 배경 이미지",
                "이미지를 프로젝트 JSON 옆 images/ 에 저장하려면\n"
                "먼저 프로젝트를 저장해 주세요.",
            )
            return
        if self._dirty:
            QMessageBox.warning(
                self,
                "씬 배경 이미지",
                "저장되지 않은 변경이 있습니다. 저장(Ctrl+S)한 뒤 다시 시도하세요.",
            )
            return
        self._persist_scene_image_settings()
        gemini_key = str(self._settings.value("gemini/api_key", "")).strip()
        if not gemini_key and not (os.environ.get("GEMINI_API_KEY") or "").strip():
            QMessageBox.warning(
                self,
                "씬 배경 이미지",
                "환경 설정에서 Gemini API 키를 입력하거나 GEMINI_API_KEY 환경 변수를 설정하세요.",
            )
            return
        image_model = self._combo_gemini_image_model.currentText().strip()
        if not image_model:
            QMessageBox.warning(
                self,
                "씬 배경 이미지",
                f"배경 이미지 모델을 선택하세요. (기본: {DEFAULT_GEMINI_IMAGE_MODEL})",
            )
            return
        parent = self._project_path.parent
        scenes_copy = list(self._project.scenes)
        self._scene_image_worker = GeminiSceneImagesWorker(
            scenes=scenes_copy,
            project_parent=parent,
            api_key=gemini_key,
            image_model=image_model,
            resolution=self._project.resolution,
        )
        self._scene_image_worker.log_line.connect(self._append_job_log)
        self._scene_image_worker.progress.connect(self._on_scene_images_progress)
        self._scene_image_worker.succeeded.connect(self._on_scene_images_succeeded)
        self._scene_image_worker.failed.connect(self._on_scene_images_failed)
        self._scene_image_worker.finished.connect(self._on_scene_images_thread_finished)
        self._start_job("씬 배경 이미지 생성")
        self._set_busy(True)
        self._arm_job_cancel(self._scene_image_worker)
        self._scene_image_worker.start()

    def _on_scene_images_progress(self, cur: int, total: int) -> None:
        self.statusBar().showMessage(f"씬 배경 이미지: {cur}/{total}", 3000)

    def _on_scene_images_succeeded(self, pairs: object) -> None:
        if not isinstance(pairs, list):
            return
        self._project = self._collect_from_ui()
        by_id: dict[int, str] = {}
        for item in pairs:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                sid, rel = item[0], item[1]
                try:
                    by_id[int(sid)] = str(rel)
                except (TypeError, ValueError):
                    continue
        for s in self._project.scenes:
            if s.scene_id in by_id:
                s.image_relpath = by_id[s.scene_id]
        snap = self._nav_snapshot()
        self._sync_ui_from_project(mark_clean=False, tree_focus=snap)
        self._mark_dirty()
        self._append_job_log("씬 배경 이미지 생성 완료 (저장 시 경로가 JSON에 반영됩니다)")
        self._finish_job_bar(success=True)
        self.statusBar().showMessage("씬 배경 이미지 완료. 저장(Ctrl+S)을 권장합니다.", 8000)

    def _on_scene_images_failed(self, msg: str) -> None:
        self._append_job_log(msg)
        self._finish_job_bar(success=False)
        if not self._is_job_cancel_message(msg):
            QMessageBox.critical(self, "씬 배경 이미지 실패", msg)

    def _on_scene_images_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        if self._scene_image_worker is not None:
            self._scene_image_worker.deleteLater()
            self._scene_image_worker = None

    def _piper_paths(self) -> tuple[Path | None, Path | None]:
        exe = str(self._settings.value("tts/piper_executable", "")).strip()
        model = str(self._settings.value("tts/piper_model_onnx", "")).strip()
        if not exe or not model:
            return None, None
        return Path(exe), Path(model)

    def _start_tts_worker(self, *, only_scene_ids: frozenset[int] | None) -> None:
        piper_path, model_path = self._piper_paths()
        if piper_path is None or model_path is None:
            QMessageBox.warning(self, "TTS", "환경 설정에서 Piper 실행 파일과 .onnx 모델 경로를 지정하세요.")
            return
        if not piper_path.is_file():
            QMessageBox.warning(self, "TTS", f"Piper 실행 파일을 찾을 수 없습니다:\n{piper_path}")
            return
        if not model_path.is_file():
            QMessageBox.warning(self, "TTS", f"모델 파일을 찾을 수 없습니다:\n{model_path}")
            return
        need_json = model_path.parent / (model_path.name + ".json")
        if piper_config_path_for_model(model_path) is None:
            extra = wrong_stem_json_hint(model_path)
            QMessageBox.critical(
                self,
                "Piper: 설정 JSON 없음",
                "WAV가 잡음·알아듣기 어려운 소리로 나오는 가장 흔한 원인은, "
                "ONNX와 짝이 되는 「.onnx.json」 파일이 없기 때문입니다.\n\n"
                f"아래 파일이 ONNX와 같은 폴더에 있어야 합니다.\n\n"
                f"{need_json}"
                f"{extra}\n\n"
                "neurlang 문서의 wget도 보통 onnx와 onnx.json 두 개를 받습니다. "
                "한쪽만 받았거나 중간에 끊기면 이런 증상이 납니다.\n\n"
                "• Hugging Face `neurlang/piper-onnx-kss-korean` — piper-kss-korean.onnx + "
                "piper-kss-korean.onnx.json 둘 다\n"
                "• 또는 `rhasspy/piper-voices` 의 ko_KR-kss-medium.onnx + "
                "ko_KR-kss-medium.onnx.json\n\n"
                "참고: piper-rs 저장소의 `cargo run …` 예제는 Rust 개발용입니다. "
                "이 앱에서는 일반 piper.exe + 위 두 파일만 있으면 됩니다.",
            )
            self.append_log(f"Piper 중단: 필수 설정 파일 없음 → {need_json}")
            return
        bad_ph = rhasspy_piper_phoneme_incompatible_reason(model_path)
        if bad_ph is not None:
            QMessageBox.critical(self, "Piper: 모델과 실행 파일이 맞지 않음", bad_ph)
            self.append_log("Piper 중단: phoneme_type이 espeak/espeak-ng가 아님 (표준 piper.exe와 비호환 가능)")
            return
        self._settings.sync()
        parent = self._project_path.parent
        scenes_copy = list(self._project.scenes)
        self._tts_worker = PiperTtsWorker(
            scenes=scenes_copy,
            project_parent=parent,
            piper_executable=piper_path,
            model_path=model_path,
            only_scene_ids=only_scene_ids,
        )
        self._tts_worker.log_line.connect(self._append_job_log)
        self._tts_worker.progress.connect(self._on_tts_progress)
        self._tts_worker.succeeded.connect(self._on_tts_succeeded)
        self._tts_worker.failed.connect(self._on_tts_failed)
        self._tts_worker.finished.connect(self._on_tts_thread_finished)
        title = "씬별 TTS (전체)" if only_scene_ids is None else "씬별 TTS (선택)"
        self._start_job(title)
        self._set_busy(True)
        self._arm_job_cancel(self._tts_worker)
        self._tts_worker.start()

    def _tts_precheck(self) -> bool:
        if self._any_worker_running():
            QMessageBox.information(self, "TTS", "다른 작업이 실행 중입니다.")
            return False
        self._project = self._collect_from_ui()
        if self._project_path is None:
            QMessageBox.warning(
                self,
                "TTS",
                "WAV 출력 위치를 프로젝트 파일 옆 audio/ 로 고정하려면\n"
                "먼저 프로젝트를 저장해 주세요.",
            )
            return False
        if self._dirty:
            QMessageBox.warning(
                self,
                "TTS",
                "저장되지 않은 변경이 있습니다. 저장(Ctrl+S)한 뒤 다시 시도하세요.",
            )
            return False
        return True

    def _on_tts_generate_batch(self) -> None:
        if not self._tts_precheck():
            return
        self._start_tts_worker(only_scene_ids=None)

    def _on_tts_generate_single(self) -> None:
        if not self._tts_precheck():
            return
        snap = self._nav_snapshot()
        if snap[0] != "scene":
            QMessageBox.information(self, "TTS", "왼쪽에서 편집할 씬 하나를 선택하세요.")
            return
        row = snap[1]
        if row < 0 or row >= len(self._project.scenes):
            return
        sid = self._project.scenes[row].scene_id
        self._start_tts_worker(only_scene_ids=frozenset({sid}))

    def _on_tts_generate_from_shortcut(self) -> None:
        if self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            QMessageBox.information(self, "TTS", "WAV 목록 모드에서는 F6을 사용하지 않습니다.")
            return
        snap = self._nav_snapshot()
        if snap[0] == "scenes":
            self._on_tts_generate_batch()
        elif snap[0] == "scene":
            self._on_tts_generate_single()
        else:
            QMessageBox.information(self, "TTS", "F6: 「씬」목록(전체 일괄) 또는 개별 씬을 선택하세요.")

    def _on_subtitle_generate_from_shortcut(self) -> None:
        if self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            QMessageBox.information(self, "자막", "WAV 목록 모드에서는 F7을 사용하지 않습니다.")
            return
        if self._nav_snapshot()[0] != "scenes":
            QMessageBox.information(self, "자막", "F7: 왼쪽에서 「씬」목록(전체)을 선택한 뒤 실행하세요.")
            return
        self._on_subtitle_generate()

    def _on_export_render_from_shortcut(self) -> None:
        if self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            self._on_wav_sequence_mp4()
            return
        if self._nav_snapshot()[0] != "scenes":
            QMessageBox.information(self, "렌더", "F8: 왼쪽에서 「씬」목록(전체)을 선택한 뒤 실행하세요.")
            return
        self._on_export_render()

    def _on_tts_progress(self, cur: int, total: int) -> None:
        self.statusBar().showMessage(f"TTS 진행: {cur}/{total}", 3000)

    def _on_tts_succeeded(self, pairs: object) -> None:
        if not isinstance(pairs, list):
            return
        self._project = self._collect_from_ui()
        by_id: dict[int, str] = {}
        for item in pairs:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                sid, rel = item[0], item[1]
                try:
                    by_id[int(sid)] = str(rel)
                except (TypeError, ValueError):
                    continue
        for s in self._project.scenes:
            if s.scene_id in by_id:
                s.audio_relpath = by_id[s.scene_id]
        snap = self._nav_snapshot()
        self._sync_ui_from_project(mark_clean=False, tree_focus=snap)
        self._mark_dirty()
        self._append_job_log("씬별 WAV 생성 완료 (저장 시 audio 경로가 JSON에 반영됩니다)")
        self._finish_job_bar(success=True)
        self.statusBar().showMessage("TTS 완료. 저장(Ctrl+S)을 권장합니다.", 8000)

    def _on_tts_failed(self, msg: str) -> None:
        self._append_job_log(msg)
        self._finish_job_bar(success=False)
        if not self._is_job_cancel_message(msg):
            QMessageBox.critical(self, "TTS 실패", msg)

    def _on_tts_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        if self._tts_worker is not None:
            self._tts_worker.deleteLater()
            self._tts_worker = None

    def _on_subtitle_generate(self) -> None:
        if self._any_worker_running():
            QMessageBox.information(self, "자막", "다른 작업이 실행 중입니다.")
            return
        if self._nav_snapshot()[0] != "scenes":
            QMessageBox.information(self, "자막", "왼쪽에서 「씬」목록을 선택한 뒤 실행하세요.")
            return
        self._project = self._collect_from_ui()
        if self._project_path is None:
            QMessageBox.warning(self, "자막", "프로젝트를 먼저 저장한 뒤 다시 시도하세요.")
            return
        if self._dirty:
            QMessageBox.warning(
                self,
                "자막",
                "저장되지 않은 변경이 있습니다. 저장(Ctrl+S)한 뒤 다시 시도하세요.",
            )
            return
        self._settings.sync()
        parent = self._project_path.parent
        scenes_copy = list(self._project.scenes)
        self._subtitle_worker = SubtitleWorker(
            scenes=scenes_copy,
            project_parent=parent,
            max_line_chars=self._subtitle_max_chars(),
        )
        self._subtitle_worker.log_line.connect(self._append_job_log)
        self._subtitle_worker.succeeded.connect(self._on_subtitle_succeeded)
        self._subtitle_worker.failed.connect(self._on_subtitle_failed)
        self._subtitle_worker.finished.connect(self._on_subtitle_thread_finished)
        self._start_job("병합 SRT 생성")
        self._set_busy(True)
        self._arm_job_cancel(self._subtitle_worker)
        self._subtitle_worker.start()

    def _on_subtitle_succeeded(self, rel: str) -> None:
        self._project = self._collect_from_ui()
        self._project.merged_srt_relpath = str(rel).strip()
        snap = self._nav_snapshot()
        self._sync_ui_from_project(mark_clean=False, tree_focus=snap)
        self._mark_dirty()
        self._append_job_log("병합 SRT 생성 완료 (저장 시 settings에 경로가 기록됩니다)")
        self._finish_job_bar(success=True)
        self.statusBar().showMessage("SRT 생성 완료. 저장(Ctrl+S)을 권장합니다.", 8000)

    def _on_subtitle_failed(self, msg: str) -> None:
        self._append_job_log(msg)
        self._finish_job_bar(success=False)
        if not self._is_job_cancel_message(msg):
            QMessageBox.critical(self, "자막 실패", msg)

    def _on_subtitle_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        if self._subtitle_worker is not None:
            self._subtitle_worker.deleteLater()
            self._subtitle_worker = None

    def _on_wav_sequence_mp4(self) -> None:
        if self._any_worker_running():
            QMessageBox.information(self, "WAV 목록 영상", "다른 작업이 실행 중입니다.")
            return
        if self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            # 저장·UI 동기화·메시지박스 포커스 전환 후에도 선택을 복원할 수 있도록 먼저 고정.
            render_pick = self._resolve_wav_sequence_render_pick()
            render_mode, render_row_indices = render_pick
            parent: Path
            try:
                if self._project_path is not None:
                    if self._dirty:
                        ret_save = QMessageBox.question(
                            self,
                            "WAV 목록 영상",
                            "저장되지 않은 변경이 있습니다.\n"
                            "저장 후 진행(예), 저장 없이 진행(아니오), 취소 중 선택하세요.",
                            QMessageBox.StandardButton.Yes
                            | QMessageBox.StandardButton.No
                            | QMessageBox.StandardButton.Cancel,
                            QMessageBox.StandardButton.No,
                        )
                        if ret_save == QMessageBox.StandardButton.Cancel:
                            return
                        if ret_save == QMessageBox.StandardButton.Yes:
                            self._save_to_path(self._project_path)
                            if self._dirty:
                                return
                            self._restore_wav_sequence_render_pick(render_pick)
                    parent = self._project_path.parent
                else:
                    picked = QFileDialog.getExistingDirectory(
                        self,
                        "WAV·자막·영상을 둘 폴더 선택 (프로젝트를 저장하지 않았을 때)",
                        str(self.project_root()),
                    )
                    if not picked:
                        return
                    parent = Path(picked)
                self._restore_wav_sequence_render_pick(render_pick)
                self._project = self._collect_from_ui()
                self._restore_wav_sequence_render_pick(render_pick)
                rows = self._collect_wav_sequence_rows(
                    allow_empty=False,
                    selected_rows=render_row_indices,
                )
                if rows is None:
                    return
                if not rows:
                    QMessageBox.warning(
                        self, "WAV 목록 영상", "최소 1개의 유효한 WAV 경로가 필요합니다."
                    )
                    return
                if render_row_indices is not None:
                    self.append_log(
                        f"[WAV 목록 영상] 선택 렌더: {len(render_row_indices)}행 중 유효 {len(rows)}개"
                    )
                else:
                    self.append_log(f"[WAV 목록 영상] 전체 렌더: {len(rows)}개")
                out_rel = (
                    self._edit_wav_sequence_out.text().strip().replace("\\", "/")
                    or "export/wav_sequence.mp4"
                )
                self._start_wav_sequence_render(parent=parent, rows=rows, out_rel=out_rel)
            finally:
                if render_mode != "all":
                    self._restore_wav_sequence_render_pick(render_pick)
            return
        parent: Path
        if self._project_path is not None:
            if self._dirty:
                QMessageBox.warning(
                    self,
                    "WAV 목록 영상",
                    "저장되지 않은 변경이 있습니다. 저장(Ctrl+S)한 뒤 다시 시도하세요.",
                )
                return
            parent = self._project_path.parent
        else:
            picked = QFileDialog.getExistingDirectory(
                self,
                "WAV·자막·영상을 둘 폴더 선택 (프로젝트를 저장하지 않았을 때)",
                str(self.project_root()),
            )
            if not picked:
                return
            parent = Path(picked)

        dlg = WavSequenceDialog(self, parent)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rows, out_rel = dlg.accepted_data()
        out_rel = out_rel.strip().replace("\\", "/") or "export/wav_sequence.mp4"
        self._start_wav_sequence_render(parent=parent, rows=rows, out_rel=out_rel)

    def _start_wav_sequence_render(self, *, parent: Path, rows: list[WavSeqRow], out_rel: str) -> None:
        ret = QMessageBox.question(
            self,
            "WAV 목록 영상",
            "원본 WAV는 자르지 않고 유지합니다.\n"
            "구간별 배경 이미지로 영상을 만들고, SRT가 있으면 병합해 입힙니다.\n"
            "자막·구간 분석이 없으면 WAV 전체 길이 1구간으로 렌더합니다.\n"
            "_render/work 폴더를 지웁니다. 계속할까요?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if ret != QMessageBox.StandardButton.Ok:
            return
        scenes: list[Scene] = []
        render_rows: list[WavSeqRow] = []
        scene_idx = 1
        for i, row in enumerate(rows, start=1):
            wav_abs = row.wav_source.resolve()
            if not wav_abs.is_file():
                self.append_log(f"[WAV 목록 영상] {i}행 WAV 없음: {wav_abs}")
                continue
            src_dur = 0.0
            try:
                src_dur = max(0.0, float(ffprobe_duration_seconds(wav_abs)))
            except Exception:
                src_dur = 0.0
            if src_dur <= 0:
                self.append_log(f"[WAV 목록 영상] {i}행 WAV 길이 확인 실패: {wav_abs}")
                continue

            raw_segments: list[dict[str, object]] = []
            for s in row.segments or []:
                if not isinstance(s, dict):
                    continue
                try:
                    st = float(s.get("start_sec", 0.0))
                    en = float(s.get("end_sec", 0.0))
                except (TypeError, ValueError):
                    continue
                if en > st:
                    raw_segments.append(s)
            if not raw_segments:
                raw_segments = [
                    {
                        "start_sec": 0.0,
                        "end_sec": src_dur,
                        "image_prompt": "",
                        "transition": row.transition or "fade",
                        "image_relpath": row.image_relpath,
                    }
                ]

            norm_segments: list[dict[str, object]] = []
            for j, seg in enumerate(raw_segments, start=1):
                try:
                    st = float(seg.get("start_sec", 0.0))
                    en = float(seg.get("end_sec", 0.0))
                except (TypeError, ValueError):
                    self.append_log(f"[WAV 목록 영상] {i}행 구간 {j} 시간 값이 올바르지 않습니다.")
                    continue
                st0, en0 = st, en
                if st >= src_dur - 0.04:
                    self.append_log(
                        f"[WAV 목록 영상] {i}행 구간 {j} 시작이 WAV 끝을 넘어 스킵합니다. "
                        f"(원본 {st0:.3f}~{en0:.3f}s, 원본 WAV 길이 {src_dur:.3f}s)"
                    )
                    continue
                st = max(0.0, min(st, src_dur - 0.04))
                en = max(st + 0.04, min(en, src_dur))
                dur = en - st
                if dur < 0.04:
                    self.append_log(
                        f"[WAV 목록 영상] {i}행 구간 {j} 길이가 0에 가까워 스킵합니다. "
                        f"(원본 {st0:.3f}~{en0:.3f}s, 보정 {st:.3f}~{en:.3f}s, 원본 WAV 길이 {src_dur:.3f}s)"
                    )
                    continue
                trans = str(seg.get("transition", row.transition)).strip() or "fade"
                img = str(seg.get("image_relpath", row.image_relpath)).strip()
                norm_segments.append(
                    {
                        "start_sec": st,
                        "end_sec": en,
                        "image_prompt": str(
                            seg.get("image_prompt", seg.get("narration", ""))
                        ).strip(),
                        "transition": trans,
                        "image_relpath": img,
                    }
                )
                scenes.append(
                    Scene(
                        scene_id=scene_idx,
                        narration_ko=" ",
                        visual_prompt_ko="",
                        transition=trans,
                        notes=f"duration_sec:{dur:.6f}",
                        audio_relpath=wav_abs.as_posix(),
                        image_relpath=img,
                    )
                )
                scene_idx += 1
            if norm_segments:
                render_rows.append(
                    WavSeqRow(
                        wav_source=wav_abs,
                        narration=" ",
                        transition=row.transition,
                        image_relpath=row.image_relpath,
                        subtitle_relpath=(row.subtitle_relpath or "").strip(),
                        start_sec=row.start_sec,
                        end_sec=row.end_sec,
                        segments=norm_segments,
                    )
                )
        if not scenes:
            self.append_log("[WAV 목록 영상] 유효한 구간이 없어 렌더를 중단합니다.")
            self.statusBar().showMessage("유효 구간 없음(로그 확인)", 8000)
            return
        srt_rel = "subs/wavseq_merged.srt"
        merged_srt_rel = ""
        try:
            body = merge_wav_subtitle_srts(
                project_parent=parent,
                wav_sources=[r.wav_source for r in render_rows],
                subtitle_relpaths=[(r.subtitle_relpath or "").strip() or None for r in render_rows],
                max_line_chars=self._subtitle_max_chars(),
                allow_empty=True,
            )
        except ValueError as e:
            self.append_log(f"[WAV 목록 영상] SRT 병합 실패: {e}")
            self.statusBar().showMessage("SRT 생성 실패(로그 확인)", 8000)
            return
        if body.strip():
            srt_abs = parent / srt_rel
            srt_abs.parent.mkdir(parents=True, exist_ok=True)
            srt_abs.write_text(body, encoding="utf-8")
            merged_srt_rel = srt_rel
        else:
            self.append_log("[WAV 목록 영상] 병합할 자막 없음 — 자막 없이 렌더합니다.")

        base = self._collect_from_ui()
        render_project = StoryProject(
            prompt_ko=base.prompt_ko,
            project_kind=base.project_kind,
            target_minutes=base.target_minutes,
            resolution=base.resolution,
            fps=base.fps,
            merged_srt_relpath=merged_srt_rel,
            export_final_relpath=out_rel,
            background_image_relpath=base.background_image_relpath,
            bgm_relpath=base.bgm_relpath,
            bgm_volume_percent=base.bgm_volume_percent,
            scenes=scenes,
        )
        self._render_from_wav_list_only = True
        self._render_worker = WavSequenceRenderWorker(
            rows=render_rows,
            project_parent=parent,
            resolution=render_project.resolution,
            fps=render_project.fps,
            global_background_relpath=render_project.background_image_relpath,
            bgm_relpath=render_project.bgm_relpath,
            bgm_volume_percent=render_project.bgm_volume_percent,
            merged_srt_relpath=merged_srt_rel,
            output_relpath=out_rel,
        )
        self._render_worker.log_line.connect(self._append_job_log)
        self._render_worker.progress.connect(self._on_export_progress)
        self._render_worker.succeeded.connect(self._on_export_succeeded)
        self._render_worker.failed.connect(self._on_export_failed)
        self._render_worker.finished.connect(self._on_export_thread_finished)
        self._start_job("영상 만들기")
        self._set_busy(True)
        self._arm_job_cancel(self._render_worker)
        self._render_worker.start()

    def _on_export_render(self) -> None:
        if self._project.project_kind == PROJECT_KIND_WAV_SEQUENCE:
            QMessageBox.information(self, "렌더", "WAV 목록 모드에서는 「영상 만들기」를 사용하세요.")
            return
        if self._any_worker_running():
            QMessageBox.information(self, "렌더", "다른 작업이 실행 중입니다.")
            return
        if self._nav_snapshot()[0] != "scenes":
            QMessageBox.information(self, "렌더", "왼쪽에서 「씬」목록을 선택한 뒤 실행하세요.")
            return
        self._project = self._collect_from_ui()
        if self._project_path is None:
            QMessageBox.warning(self, "렌더", "프로젝트를 먼저 저장하세요.")
            return
        if self._dirty:
            QMessageBox.warning(self, "렌더", "저장(Ctrl+S)한 뒤 다시 시도하세요.")
            return
        ret = QMessageBox.question(
            self,
            "렌더",
            "ffmpeg로 인코딩하며 시간이 걸릴 수 있습니다. _render/work 폴더를 지우고 다시 만듭니다. 계속할까요?",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if ret != QMessageBox.StandardButton.Ok:
            return
        parent = self._project_path.parent
        self._render_from_wav_list_only = False
        self._render_worker = FinalRenderWorker(self._project, parent)
        self._render_worker.log_line.connect(self._append_job_log)
        self._render_worker.progress.connect(self._on_export_progress)
        self._render_worker.succeeded.connect(self._on_export_succeeded)
        self._render_worker.failed.connect(self._on_export_failed)
        self._render_worker.finished.connect(self._on_export_thread_finished)
        self._start_job("최종 MP4 렌더")
        self._set_busy(True)
        self._arm_job_cancel(self._render_worker)
        self._render_worker.start()

    def _on_export_progress(self, cur: int, total: int) -> None:
        if total <= 0:
            self._update_job_progress(0, 0, message="렌더 진행 중…")
            return
        c = max(0, min(cur, total))
        self._update_job_progress(c, total, message=f"렌더 {c}/{total}")

    def _on_export_succeeded(self, rel: str) -> None:
        if self._render_from_wav_list_only:
            self._append_job_log(f"영상 만들기 완료: {rel}")
            self._finish_job_bar(success=True)
            self.statusBar().showMessage(f"생성됨: {rel}", 8000)
            return
        self._project = self._collect_from_ui()
        self._project.export_final_relpath = str(rel).strip() or EXPORT_FINAL_REL
        snap = self._nav_snapshot()
        self._sync_ui_from_project(mark_clean=False, tree_focus=snap)
        self._mark_dirty()
        self._append_job_log("최종 MP4 렌더 완료 (저장 시 export 경로가 JSON에 반영됩니다)")
        self._finish_job_bar(success=True)
        self.statusBar().showMessage("렌더 완료. 저장(Ctrl+S)을 권장합니다.", 8000)

    def _on_export_failed(self, msg: str) -> None:
        self._append_job_log(msg)
        self._finish_job_bar(success=False)
        if self._is_job_cancel_message(msg):
            self.statusBar().showMessage("렌더가 중지되었습니다.", 8000)
        else:
            self.statusBar().showMessage("렌더 실패(작업 내역 확인)", 10000)

    def _on_export_thread_finished(self) -> None:
        self._clear_job_cancel()
        self._set_busy(False)
        self._render_from_wav_list_only = False
        if self._render_worker is not None:
            self._render_worker.deleteLater()
            self._render_worker = None

    def _set_busy(self, busy: bool) -> None:
        prev_syncing = self._syncing_ui
        self._syncing_ui = True
        try:
            for a in (
                self._act_new,
                self._act_open,
                self._act_save,
                self._act_save_as,
                self._act_settings,
                self._act_generate_scenes,
                self._act_tts_wav,
                self._act_subtitle_srt,
                self._act_export_mp4,
                self._act_validate,
            ):
                a.setEnabled(not busy)
            self._nav_tree.setEnabled(not busy)
            self._btn_ctx_llm.setEnabled(not busy)
            self._btn_ctx_wav_add.setEnabled(not busy)
            self._btn_ctx_wav_all.setEnabled(not busy)
            self._btn_ctx_srt.setEnabled(not busy)
            self._btn_ctx_mp4.setEnabled(not busy)
            self._btn_ctx_wav_one.setEnabled(not busy)
            self._btn_ctx_wav_sequence_render.setEnabled(not busy)
            self._btn_add_scene.setEnabled(not busy)
            self._btn_remove_scene.setEnabled(not busy)
            self._combo_gemini_image_model.setEnabled(not busy)
            self._btn_generate_scene_images.setEnabled(not busy)
            self._wav_sequence_table.setEnabled(not busy)
            self._btn_wav_seq_remove.setEnabled(not busy)
            self._btn_wav_seq_up.setEnabled(not busy)
            self._btn_wav_seq_down.setEnabled(not busy)
            self._edit_wav_sequence_out.setEnabled(not busy)
            self._edit_single_wav_path.setEnabled(not busy)
            self._btn_browse_single_wav.setEnabled(not busy)
            self._combo_single_wav_transition.setEnabled(not busy)
            self._edit_single_wav_image.setEnabled(not busy)
            self._btn_browse_single_wav_image.setEnabled(not busy)
            self._btn_single_wav_play.setEnabled(not busy)
            self._btn_single_wav_add_marker.setEnabled(not busy)
            self._btn_single_wav_auto_segment.setEnabled(not busy)
            self._btn_music_analysis.setEnabled(not busy)
            self._btn_single_wav_stt_segments.setEnabled(not busy)
            self._edit_single_wav_subtitle_path.setEnabled(not busy)
            self._btn_browse_single_wav_subtitle.setEnabled(not busy)
            self._btn_default_single_wav_subtitle.setEnabled(not busy)
            self._btn_view_single_wav_subtitle.setEnabled(not busy)
            self._edit_single_wav_reference_lyrics.setEnabled(not busy)
            self._spin_single_wav_subtitle_intro.setEnabled(not busy)
            self._spin_single_wav_subtitle_offset.setEnabled(not busy)
            self._edit_single_wav_intro_title.setEnabled(not busy)
            self._btn_intro_title_from_wav.setEnabled(not busy)
            self._spin_single_wav_intro_title_duration.setEnabled(not busy)
            self._btn_single_wav_auto_images.setEnabled(not busy)
            self._slider_single_wav_pos.setEnabled(not busy)
            self._table_single_wav_segments.setEnabled(not busy)
            self._wav_boundary_bar.setEnabled(not busy)
            self._btn_delete_selected_segment.setEnabled(not busy)
            self._edit_single_wav_seg_prompt.setEnabled(not busy)
            self._edit_bg_image.setEnabled(not busy)
            self._btn_browse_bg.setEnabled(not busy)
            self._edit_bgm.setEnabled(not busy)
            self._btn_browse_bgm.setEnabled(not busy)
            self._spin_bgm_volume.setEnabled(not busy)
        finally:
            self._syncing_ui = prev_syncing

    def _confirm_discard_unsaved(self) -> bool:
        if not self._dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("저장")
        box.setText("저장하지 않은 변경이 있습니다. 어떻게 할까요?")
        box.setIcon(QMessageBox.Icon.Question)
        save_btn = box.addButton("저장", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("저장 안 함", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == cancel_btn:
            return False
        if clicked == save_btn:
            if self._project_path is None:
                start = str(self.project_root() / "storyboard.json")
                path, _ = QFileDialog.getSaveFileName(self, "저장", start, _JSON_FILTER)
                if not path:
                    return False
                self._save_to_path(Path(path))
            else:
                self._save_to_path(self._project_path)
            return not self._dirty
        if clicked == discard_btn:
            return True
        return False

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._any_worker_running():
            QMessageBox.warning(self, "종료", "실행 중인 작업이 끝난 뒤 종료하세요.")
            event.ignore()
            return
        if self._confirm_discard_unsaved():
            event.accept()
        else:
            event.ignore()
