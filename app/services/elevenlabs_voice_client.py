from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.services.elevenlabs_client import ElevenLabsApiError


@dataclass(frozen=True)
class ElevenLabsVoiceCandidate:
    voice_id: str
    name: str
    description: str
    category: str
    gender: str
    age: str
    accent: str
    preview_url: str
    score: int
    reason: str


def _api_key(api_key: str = "") -> str:
    key = (api_key or os.environ.get("ELEVENLABS_API_KEY", "") or "").strip()
    if not key:
        raise ElevenLabsApiError("ElevenLabs API key is empty.")
    return key


def _get_json(url: str, *, api_key: str, timeout_sec: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "xi-api-key": _api_key(api_key)},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise ElevenLabsApiError(f"ElevenLabs voices HTTP {e.code}: {err[:800] or e.reason}") from e
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        raise ElevenLabsApiError(f"ElevenLabs voices request failed: {e}") from e
    if not isinstance(payload, dict):
        raise ElevenLabsApiError("ElevenLabs voices response root is not an object.")
    return payload


def recommend_voices(
    *,
    api_key: str = "",
    topic_text: str,
    limit: int = 5,
    timeout_sec: float = 30.0,
) -> list[ElevenLabsVoiceCandidate]:
    voices = _list_available_voices(api_key=api_key, timeout_sec=timeout_sec, topic_text=topic_text)
    scored = [_score_voice(v, topic_text) for v in voices]
    scored = [v for v in scored if v.voice_id]
    scored.sort(key=lambda v: (-v.score, v.name.lower()))
    return scored[: max(1, int(limit))]


def _list_available_voices(*, api_key: str, timeout_sec: float, topic_text: str) -> list[dict[str, Any]]:
    search = _voice_search_query(topic_text)
    url = "https://api.elevenlabs.io/v2/voices?" + urllib.parse.urlencode(
        {
            "page_size": 100,
            "voice_type": "default",
            "search": search,
        }
    )
    try:
        payload = _get_json(url, api_key=api_key, timeout_sec=timeout_sec)
        raw = payload.get("voices")
        if isinstance(raw, list) and raw:
            return [v for v in raw if isinstance(v, dict)]
    except ElevenLabsApiError:
        # Older accounts/endpoints may still expose the v1 shape.
        pass

    payload = _get_json("https://api.elevenlabs.io/v1/voices", api_key=api_key, timeout_sec=timeout_sec)
    raw = payload.get("voices")
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, dict)]
    return []


def _voice_search_query(topic_text: str) -> str:
    t = (topic_text or "").lower()
    if any("\uac00" <= ch <= "\ud7a3" for ch in topic_text) or "korean" in t or "한국" in topic_text:
        return "korean"
    return "narration"


def _voice_text(v: dict[str, Any]) -> str:
    labels = v.get("labels")
    label_text = ""
    if isinstance(labels, dict):
        label_text = " ".join(str(x) for x in labels.values())
    parts = [
        v.get("name", ""),
        v.get("description", ""),
        v.get("category", ""),
        v.get("gender", ""),
        v.get("age", ""),
        v.get("accent", ""),
        label_text,
    ]
    return " ".join(str(p) for p in parts if p).lower()


def _topic_profile(topic_text: str) -> tuple[list[str], list[str]]:
    t = (topic_text or "").lower()
    wants = ["korean", "ko", "multilingual", "narration", "clear", "warm", "friendly", "calm", "natural"]
    reasons = ["한국어/다국어 음성을 우선하고, 명확하고 자연스러운 내레이션 톤"]

    if any(k in t for k in ("초등", "아이", "어린이", "학생", "쉬운", "교육", "과학", "설명")):
        wants += ["educational", "young", "friendly", "bright", "soft", "warm"]
        reasons.append("교육/설명형 콘텐츠에 맞는 친근한 톤")
    if any(k in t for k in ("다큐", "역사", "우주", "경제", "뉴스", "분석")):
        wants += ["documentary", "authoritative", "deep", "calm", "mature", "serious"]
        reasons.append("다큐/정보형 콘텐츠에 맞는 차분한 신뢰감")
    if any(k in t for k in ("공포", "미스터리", "스릴러", "무서", "범죄")):
        wants += ["dramatic", "deep", "serious", "low", "mysterious"]
        reasons.append("긴장감 있는 장르에 맞는 낮고 진지한 톤")
    if any(k in t for k in ("광고", "홍보", "제품", "브랜드", "세일즈")):
        wants += ["energetic", "confident", "upbeat", "commercial", "bright"]
        reasons.append("홍보성 콘텐츠에 맞는 선명하고 활기 있는 톤")
    if any(k in t for k in ("명상", "수면", "힐링", "감성", "편안")):
        wants += ["calm", "soft", "soothing", "gentle", "relaxed"]
        reasons.append("편안한 콘텐츠에 맞는 부드러운 톤")
    if any(k in t for k in ("한국", "한국어", "한글", "korean")):
        wants += ["korean", "ko", "multilingual"]
        reasons.append("한국어 콘텐츠에 적합한 후보")
    return wants, reasons


def _score_voice(v: dict[str, Any], topic_text: str) -> ElevenLabsVoiceCandidate:
    text = _voice_text(v)
    wants, reasons = _topic_profile(topic_text)
    score = 0
    hits: list[str] = []
    for word in wants:
        if word in text:
            score += 8
            hits.append(word)

    category = str(v.get("category", "") or "")
    if category.lower() in ("default", "professional", "premade"):
        score += 5
    if "korean" in text or "ko" in text or "multilingual" in text:
        score += 30
    if "narrat" in text:
        score += 10
    if "clear" in text:
        score += 8
    if "warm" in text or "friendly" in text:
        score += 6

    labels = v.get("labels")
    if isinstance(labels, dict):
        gender = str(labels.get("gender", v.get("gender", "")) or "")
        age = str(labels.get("age", v.get("age", "")) or "")
        accent = str(labels.get("accent", v.get("accent", "")) or "")
    else:
        gender = str(v.get("gender", "") or "")
        age = str(v.get("age", "") or "")
        accent = str(v.get("accent", "") or "")

    reason = ", ".join(reasons)
    if hits:
        reason += f" / 매칭 키워드: {', '.join(sorted(set(hits))[:6])}"

    return ElevenLabsVoiceCandidate(
        voice_id=str(v.get("voice_id", "") or ""),
        name=str(v.get("name", "") or "(이름 없음)"),
        description=str(v.get("description", "") or ""),
        category=category,
        gender=gender,
        age=age,
        accent=accent,
        preview_url=str(v.get("preview_url", "") or ""),
        score=score,
        reason=reason,
    )
