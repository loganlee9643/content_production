"""Gemini 이미지 생성 전용 모델 ID (텍스트 채팅용 모델과 다름)."""

from __future__ import annotations

GEMINI_IMAGE_MODEL_PRESET_IDS: list[str] = [
    "gemini-3.1-flash-image-preview",
    "gemini-2.5-flash-image",
    "gemini-2.0-flash-preview-image-generation",
]

DEFAULT_GEMINI_IMAGE_MODEL: str = "gemini-2.5-flash-image"
