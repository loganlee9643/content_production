from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.services.gemini_image_model_catalog import (
    DEFAULT_GEMINI_IMAGE_MODEL,
    GEMINI_IMAGE_MODEL_PRESET_IDS,
)
from app.services.gemini_model_catalog import DEFAULT_GEMINI_MODEL, GEMINI_MODEL_PRESET_IDS
from app.stt_settings_defaults import (
    STT_COMPUTE_PRESETS,
    STT_MODEL_PRESETS,
    STT_SETTINGS_DEFAULTS,
)


class SettingsDialog(QDialog):
    """LLM·Piper·자막·STT·Gemini 이미지 모델 등 앱 전역 설정(QSettings)."""

    def __init__(self, parent: QWidget | None, settings: QSettings) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("환경 설정")
        self.resize(580, 620)

        tabs = QTabWidget()
        tabs.addTab(self._build_llm_tab(), "LLM")
        tabs.addTab(self._build_tts_tab(), "음성 (Piper)")
        tabs.addTab(self._build_stt_tab(), "STT (자막 추출)")
        tabs.addTab(self._build_subtitle_tab(), "자막 (SRT)")
        tabs.addTab(self._build_image_tab(), "씬 배경 이미지")
        tabs.addTab(self._build_video_production_tab(), "영상 제작")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(tabs)
        root.addWidget(buttons)

        self._load_from_settings()

    def _build_llm_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self._combo_llm_provider = QComboBox()
        self._combo_llm_provider.addItem("Ollama (로컬)", "ollama")
        self._combo_llm_provider.addItem("Google Gemini API", "gemini")
        self._combo_llm_provider.currentIndexChanged.connect(self._on_llm_provider_changed)
        form.addRow("LLM 백엔드", self._combo_llm_provider)

        self._stack_llm = QStackedWidget()
        page_o = QWidget()
        fo = QFormLayout(page_o)
        self._edit_ollama_url = QLineEdit()
        self._edit_ollama_url.setPlaceholderText("http://127.0.0.1:11434")
        fo.addRow("Ollama URL", self._edit_ollama_url)

        self._edit_ollama_model = QLineEdit()
        self._edit_ollama_model.setPlaceholderText("예: llama3.2, qwen2.5:7b")
        fo.addRow("모델", self._edit_ollama_model)

        page_g = QWidget()
        fg = QFormLayout(page_g)
        self._edit_gemini_key = QLineEdit()
        self._edit_gemini_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_gemini_key.setPlaceholderText("API 키 (비우면 환경 변수 GEMINI_API_KEY 사용)")
        fg.addRow("API 키", self._edit_gemini_key)

        self._combo_gemini_model = QComboBox()
        self._combo_gemini_model.setEditable(True)
        self._combo_gemini_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_gemini_model.addItems(GEMINI_MODEL_PRESET_IDS)
        gem_le = self._combo_gemini_model.lineEdit()
        if gem_le is not None:
            gem_le.setPlaceholderText("목록에서 선택하거나 모델 ID를 직접 입력")
        fg.addRow("모델", self._combo_gemini_model)

        self._stack_llm.addWidget(page_o)
        self._stack_llm.addWidget(page_g)
        form.addRow(self._stack_llm)

        hint = QLabel(
            "「LLM으로 씬 생성」은 왼쪽에서 「프롬프트」를 선택한 뒤 "
            "상단 작업 막대에서 실행합니다. Gemini 이미지 생성에도 위 API 키가 사용됩니다."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        return w

    def _build_tts_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        hint = QLabel(
            "필수: Piper는 ONNX와 같은 폴더에 「파일이름.onnx.json」이 반드시 있어야 합니다.\n"
            "예: ko_KR-kss-medium.onnx 옆에 ko_KR-kss-medium.onnx.json\n\n"
            "ONNX만 두고 .onnx.json이 없으면 알아듣기 어려운 잡음·외계어 같은 소리만 납니다.\n"
            "Hugging Face의 rhasspy/piper-voices 등에서 onnx와 onnx.json을 함께 받으세요.\n"
            "또한 ONNX JSON의 phoneme_type은 보통 espeak 입니다. "
            "neurlang의 piper-kss-korean(pygoruut)은 표준 piper.exe와 짝이 맞지 않을 수 있습니다.\n\n"
            "출력 WAV는 프로젝트 JSON과 같은 폴더의 audio/ 입니다."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)

        form = QFormLayout()
        row_exe = QHBoxLayout()
        self._edit_piper_exe = QLineEdit()
        self._edit_piper_exe.setPlaceholderText("예: C:\\\\piper\\\\piper.exe")
        btn_exe = QPushButton("찾기…")

        def browse_exe() -> None:
            start = self._edit_piper_exe.text().strip() or str(Path.home())
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Piper 실행 파일",
                start,
                "실행 파일 (*.exe);;모든 파일 (*.*)",
            )
            if path:
                self._edit_piper_exe.setText(path)

        btn_exe.clicked.connect(browse_exe)
        row_exe.addWidget(self._edit_piper_exe, stretch=1)
        row_exe.addWidget(btn_exe)
        form.addRow("Piper 실행 파일", row_exe)

        row_model = QHBoxLayout()
        self._edit_piper_model = QLineEdit()
        self._edit_piper_model.setPlaceholderText("예: …\\\\ko_KR-kss-medium.onnx")
        btn_model = QPushButton("찾기…")

        def browse_model() -> None:
            start = self._edit_piper_model.text().strip() or str(Path.home())
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Piper ONNX 모델",
                start,
                "ONNX 모델 (*.onnx);;모든 파일 (*.*)",
            )
            if path:
                self._edit_piper_model.setText(path)

        btn_model.clicked.connect(browse_model)
        row_model.addWidget(self._edit_piper_model, stretch=1)
        row_model.addWidget(btn_model)
        form.addRow("Piper 모델 (.onnx)", row_model)
        outer.addLayout(form)
        outer.addStretch(1)
        return w

    def _build_stt_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        hint = QLabel(
            "WAV에서 faster-whisper로 자막을 뽑을 때 쓰는 옵션입니다. "
            "저장 후 다음 STT 실행부터 적용되며, 터미널 로그에 「STT 설정 적용」으로 확인할 수 있습니다.\n\n"
            "· 기본값은 간주·인트로 환각 자막을 줄이면서, 간주 뒤 보컬 구간을 다시 잡기 쉽게 맞춰 두었습니다.\n"
            "· VAD 음성 임계값을 낮출수록 간주 후 보컬을 더 일찍 잡지만, 간주 중 자막이 늘 수 있습니다.\n"
            "· 모델·beam을 올리면 정확도는 좋아지나 처리 시간이 길어집니다."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)

        preset_row = QHBoxLayout()
        btn_defaults = QPushButton("STT 기본값으로")
        btn_defaults.setToolTip("아래 STT·VAD 옵션을 앱 기본값으로 되돌립니다. 확인을 눌러야 저장됩니다.")
        btn_defaults.clicked.connect(self._apply_stt_defaults_to_form)
        preset_row.addWidget(btn_defaults)
        preset_row.addStretch(1)
        outer.addLayout(preset_row)

        form = QFormLayout()
        self._combo_stt_model = QComboBox()
        self._combo_stt_model.setEditable(True)
        self._combo_stt_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_stt_model.addItems(STT_MODEL_PRESETS)
        stt_le = self._combo_stt_model.lineEdit()
        if stt_le is not None:
            stt_le.setPlaceholderText("tiny, small, medium, large-v3 …")
        form.addRow("Whisper 모델", self._combo_stt_model)

        self._combo_stt_compute = QComboBox()
        self._combo_stt_compute.setEditable(True)
        self._combo_stt_compute.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_stt_compute.addItems(STT_COMPUTE_PRESETS)
        form.addRow("연산 타입", self._combo_stt_compute)

        self._check_stt_vad = QCheckBox("VAD로 음성 구간만 인식 (간주·반주 필터)")
        self._check_stt_vad.toggled.connect(self._sync_stt_vad_controls_enabled)
        form.addRow("", self._check_stt_vad)

        self._spin_stt_vad_threshold = QDoubleSpinBox()
        self._spin_stt_vad_threshold.setRange(0.05, 0.95)
        self._spin_stt_vad_threshold.setDecimals(2)
        self._spin_stt_vad_threshold.setSingleStep(0.05)
        self._spin_stt_vad_threshold.setToolTip(
            "Silero 음성 확률 임계값. 높일수록 확실한 보컬만 인식(간주 자막↓·누락↑), "
            "낮출수록 작은 소리도 인식(누락↓·간주 자막↑). 기본 0.5."
        )
        form.addRow("VAD 음성 임계값 (threshold)", self._spin_stt_vad_threshold)

        self._spin_stt_vad_min_silence = QSpinBox()
        self._spin_stt_vad_min_silence.setRange(0, 15000)
        self._spin_stt_vad_min_silence.setSingleStep(100)
        self._spin_stt_vad_min_silence.setSuffix(" ms")
        self._spin_stt_vad_min_silence.setToolTip(
            "이만큼 이상 조용해야 ‘말 끝’으로 구간을 자릅니다. 클수록 긴 간주를 침묵으로 보기 쉬움."
        )
        form.addRow("VAD 최소 무음 길이", self._spin_stt_vad_min_silence)

        self._spin_stt_vad_min_speech = QSpinBox()
        self._spin_stt_vad_min_speech.setRange(0, 5000)
        self._spin_stt_vad_min_speech.setSingleStep(50)
        self._spin_stt_vad_min_speech.setSuffix(" ms")
        self._spin_stt_vad_min_speech.setToolTip("이보다 짧은 음성 덩어리는 버립니다. 잡음·짧은 반주 제거에 유리.")
        form.addRow("VAD 최소 음성 길이", self._spin_stt_vad_min_speech)

        self._spin_stt_vad_speech_pad = QSpinBox()
        self._spin_stt_vad_speech_pad.setRange(0, 3000)
        self._spin_stt_vad_speech_pad.setSingleStep(50)
        self._spin_stt_vad_speech_pad.setSuffix(" ms")
        self._spin_stt_vad_speech_pad.setToolTip("인식된 음성 구간 앞뒤로 붙이는 여유(ms). 너무 크면 간주 일부가 포함될 수 있음.")
        form.addRow("VAD 음성 패딩", self._spin_stt_vad_speech_pad)

        self._spin_stt_beam = QSpinBox()
        self._spin_stt_beam.setRange(1, 20)
        self._spin_stt_beam.setToolTip("클수록 정확하지만 느려집니다.")
        form.addRow("beam_size", self._spin_stt_beam)

        self._spin_stt_no_speech = QDoubleSpinBox()
        self._spin_stt_no_speech.setRange(0.0, 1.0)
        self._spin_stt_no_speech.setDecimals(2)
        self._spin_stt_no_speech.setSingleStep(0.05)
        self._spin_stt_no_speech.setToolTip(
            "Whisper 내부 no_speech_threshold. 낮을수록 무음·간주 구간을 더 많이 건너뜁니다."
        )
        form.addRow("no_speech_threshold", self._spin_stt_no_speech)

        self._spin_stt_max_no_speech = QDoubleSpinBox()
        self._spin_stt_max_no_speech.setRange(0.0, 1.0)
        self._spin_stt_max_no_speech.setDecimals(2)
        self._spin_stt_max_no_speech.setSingleStep(0.05)
        self._spin_stt_max_no_speech.setToolTip(
            "구간 no_speech_prob가 이 값보다 크면 결과에서 제외합니다."
        )
        form.addRow("max_no_speech_prob", self._spin_stt_max_no_speech)

        self._spin_stt_log_prob = QDoubleSpinBox()
        self._spin_stt_log_prob.setRange(-5.0, 0.0)
        self._spin_stt_log_prob.setDecimals(1)
        self._spin_stt_log_prob.setSingleStep(0.1)
        self._spin_stt_log_prob.setToolTip(
            "avg_logprob가 이보다 낮으면(더 음수) 해당 구간을 버립니다. -2.0은 음악에 관대한 편입니다."
        )
        form.addRow("log_prob_threshold", self._spin_stt_log_prob)

        outer.addLayout(form)
        outer.addStretch(1)
        return w

    def _stt_vad_detail_widgets(self) -> list[QWidget]:
        return [
            self._spin_stt_vad_threshold,
            self._spin_stt_vad_min_silence,
            self._spin_stt_vad_min_speech,
            self._spin_stt_vad_speech_pad,
        ]

    def _sync_stt_vad_controls_enabled(self, _checked: bool | None = None) -> None:
        on = self._check_stt_vad.isChecked()
        for w in self._stt_vad_detail_widgets():
            w.setEnabled(on)

    def _apply_stt_defaults_to_form(self) -> None:
        self._apply_stt_preset(STT_SETTINGS_DEFAULTS)

    def _apply_stt_preset(self, preset: dict[str, object]) -> None:
        self._combo_stt_model.setCurrentText(str(preset.get("stt/model", "medium")))
        self._combo_stt_compute.setCurrentText(str(preset.get("stt/compute_type", "int8")))
        vad = preset.get("stt/vad_filter", True)
        self._check_stt_vad.setChecked(bool(vad) if isinstance(vad, bool) else str(vad).lower() in ("1", "true", "yes"))
        self._spin_stt_vad_threshold.setValue(float(preset.get("stt/vad_threshold", 0.5)))
        self._spin_stt_vad_min_silence.setValue(int(preset.get("stt/vad_min_silence_duration_ms", 2000)))
        self._spin_stt_vad_min_speech.setValue(int(preset.get("stt/vad_min_speech_duration_ms", 0)))
        self._spin_stt_vad_speech_pad.setValue(int(preset.get("stt/vad_speech_pad_ms", 400)))
        try:
            self._spin_stt_beam.setValue(max(1, int(preset.get("stt/beam_size", 6))))
        except (TypeError, ValueError):
            self._spin_stt_beam.setValue(6)
        self._spin_stt_no_speech.setValue(float(preset.get("stt/no_speech_threshold", 0.8)))
        self._spin_stt_max_no_speech.setValue(float(preset.get("stt/max_no_speech_prob", 0.8)))
        self._spin_stt_log_prob.setValue(float(preset.get("stt/log_prob_threshold", -2.0)))
        self._sync_stt_vad_controls_enabled()

    def _stt_default(self, key: str) -> object:
        return STT_SETTINGS_DEFAULTS.get(key)

    def _load_stt_from_settings(self) -> None:
        model = str(self._settings.value("stt/model", self._stt_default("stt/model")) or "medium")
        compute = str(self._settings.value("stt/compute_type", self._stt_default("stt/compute_type")) or "int8")
        self._combo_stt_model.setCurrentText(model)
        self._combo_stt_compute.setCurrentText(compute)

        vad_raw = self._settings.value("stt/vad_filter", self._stt_default("stt/vad_filter"))
        if isinstance(vad_raw, bool):
            vad_on = vad_raw
        else:
            vad_on = str(vad_raw).strip().lower() in ("1", "true", "yes", "on")
        self._check_stt_vad.setChecked(vad_on)

        for spin, key, fallback in (
            (self._spin_stt_vad_threshold, "stt/vad_threshold", 0.5),
            (self._spin_stt_vad_min_silence, "stt/vad_min_silence_duration_ms", 2000),
            (self._spin_stt_vad_min_speech, "stt/vad_min_speech_duration_ms", 0),
            (self._spin_stt_vad_speech_pad, "stt/vad_speech_pad_ms", 400),
        ):
            raw = self._settings.value(key, self._stt_default(key))
            try:
                if isinstance(spin, QDoubleSpinBox):
                    spin.setValue(float(raw))
                else:
                    spin.setValue(int(raw))
            except (TypeError, ValueError):
                if isinstance(spin, QDoubleSpinBox):
                    spin.setValue(float(self._stt_default(key) or fallback))
                else:
                    spin.setValue(int(self._stt_default(key) or fallback))

        try:
            beam = max(1, int(self._settings.value("stt/beam_size", self._stt_default("stt/beam_size"))))
        except (TypeError, ValueError):
            beam = 6
        self._spin_stt_beam.setValue(beam)

        for spin, key in (
            (self._spin_stt_no_speech, "stt/no_speech_threshold"),
            (self._spin_stt_max_no_speech, "stt/max_no_speech_prob"),
            (self._spin_stt_log_prob, "stt/log_prob_threshold"),
        ):
            raw = self._settings.value(key, self._stt_default(key))
            try:
                spin.setValue(float(raw))
            except (TypeError, ValueError):
                spin.setValue(float(self._stt_default(key)))

        self._sync_stt_vad_controls_enabled()

    def _save_stt_to_settings(self) -> None:
        self._settings.setValue("stt/model", self._combo_stt_model.currentText().strip() or "medium")
        self._settings.setValue(
            "stt/compute_type",
            self._combo_stt_compute.currentText().strip() or "int8",
        )
        self._settings.setValue("stt/vad_filter", self._check_stt_vad.isChecked())
        self._settings.setValue("stt/vad_threshold", float(self._spin_stt_vad_threshold.value()))
        self._settings.setValue("stt/vad_min_silence_duration_ms", int(self._spin_stt_vad_min_silence.value()))
        self._settings.setValue("stt/vad_min_speech_duration_ms", int(self._spin_stt_vad_min_speech.value()))
        self._settings.setValue("stt/vad_speech_pad_ms", int(self._spin_stt_vad_speech_pad.value()))
        self._settings.setValue("stt/beam_size", int(self._spin_stt_beam.value()))
        self._settings.setValue("stt/no_speech_threshold", float(self._spin_stt_no_speech.value()))
        self._settings.setValue("stt/max_no_speech_prob", float(self._spin_stt_max_no_speech.value()))
        self._settings.setValue("stt/log_prob_threshold", float(self._spin_stt_log_prob.value()))

    def _build_subtitle_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        hint = QLabel(
            "병합 SRT 생성 시 한 줄에 넣을 최대 글자 수입니다. "
            "프로젝트를 저장한 뒤 「씬」을 선택하고 자막 작업을 실행하세요."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)
        form = QFormLayout()
        self._spin_subtitle_chars = QSpinBox()
        self._spin_subtitle_chars.setRange(8, 80)
        self._spin_subtitle_chars.setValue(34)
        form.addRow("한 자막 줄 최대 글자 수", self._spin_subtitle_chars)
        self._spin_subtitle_intro_default = QDoubleSpinBox()
        self._spin_subtitle_intro_default.setRange(0.0, 600.0)
        self._spin_subtitle_intro_default.setDecimals(1)
        self._spin_subtitle_intro_default.setSingleStep(0.5)
        self._spin_subtitle_intro_default.setSuffix(" 초")
        self._spin_subtitle_intro_default.setToolTip(
            "WAV별 값이 없을 때 적용. 인트로·간주 앞부분 무자막 시간."
        )
        form.addRow("기본 인트로 무자막", self._spin_subtitle_intro_default)
        self._spin_subtitle_offset_default = QDoubleSpinBox()
        self._spin_subtitle_offset_default.setRange(-120.0, 120.0)
        self._spin_subtitle_offset_default.setDecimals(1)
        self._spin_subtitle_offset_default.setSingleStep(0.5)
        self._spin_subtitle_offset_default.setSuffix(" 초")
        form.addRow("기본 자막 지연", self._spin_subtitle_offset_default)
        self._check_vocal_retime = QCheckBox(
            "원곡 가사가 있을 때 Gemini로 보컬 구간에만 자막 배치(인트로·간주 제외)"
        )
        self._check_vocal_retime.setChecked(True)
        form.addRow("", self._check_vocal_retime)
        outer.addLayout(form)
        outer.addStretch(1)
        return w

    def _build_image_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        hint = QLabel(
            "「씬」목록을 선택한 뒤 씬 편집 영역에서 Gemini로 배경 이미지를 생성할 때 사용할 모델입니다."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)
        form = QFormLayout()
        self._combo_gemini_image_model = QComboBox()
        self._combo_gemini_image_model.setEditable(True)
        self._combo_gemini_image_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo_gemini_image_model.addItems(GEMINI_IMAGE_MODEL_PRESET_IDS)
        img_le = self._combo_gemini_image_model.lineEdit()
        if img_le is not None:
            img_le.setPlaceholderText("예: gemini-2.5-flash-image")
        form.addRow("배경 이미지 모델", self._combo_gemini_image_model)
        outer.addLayout(form)
        outer.addStretch(1)
        return w

    def _build_video_production_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        hint = QLabel(
            "영상 제작 프로젝트에서 사용하는 Veo 및 ElevenLabs 설정입니다. "
            "Gemini API 키와 대본 모델은 LLM 탭, 이미지 모델은 씬 배경 이미지 탭 설정을 사용합니다."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)

        form = QFormLayout()
        self._edit_veo_model = QLineEdit()
        self._edit_veo_model.setPlaceholderText("veo-3.1-generate-preview")
        form.addRow("Veo 모델", self._edit_veo_model)

        self._edit_veo_resolution = QLineEdit()
        self._edit_veo_resolution.setPlaceholderText("720p")
        form.addRow("Veo 해상도", self._edit_veo_resolution)

        self._combo_voice_provider = QComboBox()
        self._combo_voice_provider.addItem("ElevenLabs", "elevenlabs")
        self._combo_voice_provider.addItem("Gemini TTS", "gemini_tts")
        form.addRow("\uC74C\uC131 \uC0DD\uC131 \uBC29\uC2DD", self._combo_voice_provider)

        self._edit_elevenlabs_key = QLineEdit()
        self._edit_elevenlabs_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit_elevenlabs_key.setPlaceholderText("비워두면 ELEVENLABS_API_KEY 환경변수 사용")
        form.addRow("ElevenLabs API 키", self._edit_elevenlabs_key)

        self._edit_elevenlabs_voice = QLineEdit()
        self._edit_elevenlabs_voice.setPlaceholderText("비워두면 ELEVENLABS_VOICE_ID 환경변수 사용")
        form.addRow("ElevenLabs voice ID", self._edit_elevenlabs_voice)

        self._edit_elevenlabs_model = QLineEdit()
        self._edit_elevenlabs_model.setPlaceholderText("eleven_multilingual_v2")
        form.addRow("ElevenLabs 모델", self._edit_elevenlabs_model)

        self._edit_gemini_tts_model = QLineEdit()
        self._edit_gemini_tts_model.setPlaceholderText("gemini-2.5-flash-preview-tts")
        form.addRow("Gemini TTS \uBAA8\uB378", self._edit_gemini_tts_model)

        self._edit_gemini_tts_voice = QLineEdit()
        self._edit_gemini_tts_voice.setPlaceholderText("Kore")
        form.addRow("Gemini TTS voice", self._edit_gemini_tts_voice)

        self._edit_gemini_tts_style = QLineEdit()
        self._edit_gemini_tts_style.setPlaceholderText("\uD55C\uAD6D\uC5B4\uB85C \uC790\uC5F0\uC2A4\uB7FD\uACE0 \uB610\uB837\uD55C \uB0B4\uB808\uC774\uC158\uC73C\uB85C \uC77D\uC5B4\uC918.")
        form.addRow("Gemini TTS \uC9C0\uC2DC\uBB38", self._edit_gemini_tts_style)

        outer.addLayout(form)
        outer.addStretch(1)
        return w

    def _on_llm_provider_changed(self, _index: int) -> None:
        self._stack_llm.setCurrentIndex(self._combo_llm_provider.currentIndex())

    def _load_from_settings(self) -> None:
        prov = str(self._settings.value("llm/provider", "ollama"))
        idx = 1 if prov == "gemini" else 0
        self._combo_llm_provider.blockSignals(True)
        self._combo_llm_provider.setCurrentIndex(idx)
        self._combo_llm_provider.blockSignals(False)
        self._stack_llm.setCurrentIndex(idx)

        u = self._settings.value("ollama/base_url", "http://127.0.0.1:11434")
        m = self._settings.value("ollama/model", "llama3.2")
        self._edit_ollama_url.setText(str(u) if u else "http://127.0.0.1:11434")
        self._edit_ollama_model.setText(str(m) if m else "llama3.2")

        gk = self._settings.value("gemini/api_key", "")
        gm = self._settings.value("gemini/model", DEFAULT_GEMINI_MODEL)
        gm_str = str(gm) if gm else DEFAULT_GEMINI_MODEL
        if gm_str in ("gemini-2.0-flash", "models/gemini-2.0-flash"):
            gm_str = DEFAULT_GEMINI_MODEL
        self._edit_gemini_key.setText(str(gk) if gk else "")
        self._combo_gemini_model.setCurrentText(gm_str)

        exe = self._settings.value("tts/piper_executable", "")
        model = self._settings.value("tts/piper_model_onnx", "")
        self._edit_piper_exe.setText(str(exe) if exe else "")
        self._edit_piper_model.setText(str(model) if model else "")

        mc = self._settings.value("subtitle/max_line_chars", 34)
        try:
            v = int(mc)
        except (TypeError, ValueError):
            v = 34
        v = max(8, min(80, v))
        self._spin_subtitle_chars.setValue(v)

        intro = self._settings.value("subtitle/default_intro_skip_sec", 0.0)
        try:
            intro_v = max(0.0, float(intro))
        except (TypeError, ValueError):
            intro_v = 0.0
        self._spin_subtitle_intro_default.setValue(intro_v)

        off = self._settings.value("subtitle/default_offset_sec", 0.0)
        try:
            off_v = float(off)
        except (TypeError, ValueError):
            off_v = 0.0
        self._spin_subtitle_offset_default.setValue(off_v)

        vr = self._settings.value("subtitle/vocal_retime_with_lyrics", True)
        if isinstance(vr, bool):
            vocal_on = vr
        else:
            vocal_on = str(vr).strip().lower() not in ("0", "false", "no", "off")
        self._check_vocal_retime.setChecked(vocal_on)

        im = self._settings.value("gemini/image_model", DEFAULT_GEMINI_IMAGE_MODEL)
        im_str = str(im) if im else DEFAULT_GEMINI_IMAGE_MODEL
        self._combo_gemini_image_model.setCurrentText(im_str)

        self._edit_veo_model.setText(
            str(self._settings.value("video/veo_model", "veo-3.1-generate-preview") or "veo-3.1-generate-preview")
        )
        self._edit_veo_resolution.setText(str(self._settings.value("video/veo_resolution", "720p") or "720p"))
        voice_provider = str(self._settings.value("voice/provider", "elevenlabs") or "elevenlabs")
        provider_idx = self._combo_voice_provider.findData(voice_provider)
        self._combo_voice_provider.setCurrentIndex(provider_idx if provider_idx >= 0 else 0)
        self._edit_elevenlabs_key.setText(str(self._settings.value("elevenlabs/api_key", "") or ""))
        self._edit_elevenlabs_voice.setText(str(self._settings.value("elevenlabs/voice_id", "") or ""))
        self._edit_elevenlabs_model.setText(
            str(self._settings.value("elevenlabs/model", "eleven_multilingual_v2") or "eleven_multilingual_v2")
        )
        self._edit_gemini_tts_model.setText(
            str(self._settings.value("gemini_tts/model", "gemini-2.5-flash-preview-tts") or "gemini-2.5-flash-preview-tts")
        )
        self._edit_gemini_tts_voice.setText(str(self._settings.value("gemini_tts/voice_name", "Kore") or "Kore"))
        self._edit_gemini_tts_style.setText(
            str(
                self._settings.value(
                    "gemini_tts/style_prompt",
                    "한국어로 자연스럽고 또렷한 내레이션으로 읽어줘.",
                )
                or "한국어로 자연스럽고 또렷한 내레이션으로 읽어줘."
            )
        )

        self._load_stt_from_settings()

    def _on_accept(self) -> None:
        data = self._combo_llm_provider.currentData()
        prov = str(data) if data is not None else "ollama"
        self._settings.setValue("llm/provider", prov)
        self._settings.setValue("ollama/base_url", self._edit_ollama_url.text().strip())
        self._settings.setValue("ollama/model", self._edit_ollama_model.text().strip())
        self._settings.setValue("gemini/api_key", self._edit_gemini_key.text().strip())
        self._settings.setValue("gemini/model", self._combo_gemini_model.currentText().strip())
        self._settings.setValue("tts/piper_executable", self._edit_piper_exe.text().strip())
        self._settings.setValue("tts/piper_model_onnx", self._edit_piper_model.text().strip())
        self._settings.setValue("subtitle/max_line_chars", int(self._spin_subtitle_chars.value()))
        self._settings.setValue(
            "subtitle/default_intro_skip_sec",
            float(self._spin_subtitle_intro_default.value()),
        )
        self._settings.setValue(
            "subtitle/default_offset_sec",
            float(self._spin_subtitle_offset_default.value()),
        )
        self._settings.setValue(
            "subtitle/vocal_retime_with_lyrics",
            self._check_vocal_retime.isChecked(),
        )
        self._settings.setValue(
            "gemini/image_model",
            self._combo_gemini_image_model.currentText().strip(),
        )
        self._settings.setValue("video/veo_model", self._edit_veo_model.text().strip())
        self._settings.setValue("video/veo_resolution", self._edit_veo_resolution.text().strip())
        voice_provider = self._combo_voice_provider.currentData()
        self._settings.setValue("voice/provider", str(voice_provider or "elevenlabs"))
        self._settings.setValue("elevenlabs/api_key", self._edit_elevenlabs_key.text().strip())
        self._settings.setValue("elevenlabs/voice_id", self._edit_elevenlabs_voice.text().strip())
        self._settings.setValue("elevenlabs/model", self._edit_elevenlabs_model.text().strip())
        self._settings.setValue("gemini_tts/model", self._edit_gemini_tts_model.text().strip())
        self._settings.setValue("gemini_tts/voice_name", self._edit_gemini_tts_voice.text().strip())
        self._settings.setValue("gemini_tts/style_prompt", self._edit_gemini_tts_style.text().strip())
        self._save_stt_to_settings()
        self._settings.sync()
        self.accept()
