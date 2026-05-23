"""Google AI Studio / Generative Language API 에서 자주 쓰이는 모델 ID 프리셋.

공식 목록은 계속 바뀌므로, 필요 시 이 배열만 갱신하면 됩니다.
(목록에 없는 ID는 콤보에서 직접 입력 가능)
"""

from __future__ import annotations

# 신규 → 구형 순 (UI 콤보 표시 순서)
GEMINI_MODEL_PRESET_IDS: list[str] = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]

DEFAULT_GEMINI_MODEL: str = "gemini-2.5-flash"
