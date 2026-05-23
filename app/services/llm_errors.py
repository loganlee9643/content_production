"""LLM 제공자(Ollama·Gemini 등) 공통 예외 베이스."""


class LlmProviderError(RuntimeError):
    """HTTP·연결·API 응답 처리 실패 등."""
