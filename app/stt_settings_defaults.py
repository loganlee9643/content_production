from __future__ import annotations

STT_RUNTIME_DEFAULTS_VERSION = 9

# 간주·인트로 억제와 간주 후 보컬 재개 인식의 균형
STT_SETTINGS_DEFAULTS: dict[str, object] = {
    "stt/model": "medium",
    "stt/compute_type": "int8",
    "stt/vad_filter": True,
    "stt/vad_threshold": 0.45,
    "stt/vad_min_silence_duration_ms": 2200,
    "stt/vad_min_speech_duration_ms": 150,
    "stt/vad_speech_pad_ms": 400,
    "stt/beam_size": 6,
    "stt/no_speech_threshold": 0.88,
    "stt/max_no_speech_prob": 0.90,
    "stt/log_prob_threshold": -2.0,
    "stt/condition_on_previous_text": False,
    "stt/temperature": 0.0,
    "stt/compression_ratio_threshold": 2.4,
    "stt/chunk_length": 30,
}

STT_MODEL_PRESETS = ("tiny", "base", "small", "medium", "large-v2", "large-v3")
STT_COMPUTE_PRESETS = ("int8", "int8_float16", "float16", "float32")
