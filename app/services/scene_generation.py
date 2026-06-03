from __future__ import annotations

import json
import re
from typing import Any

from app.models.storyboard import Scene


SYSTEM_SCENE_JSON_KO_TEMPLATE = """당신은 한국어 YouTube 영상용 스토리보드 작성 보조입니다.
사용자 요구에 맞춰 씬 목록을 JSON으로만 출력하세요. 다른 설명·서문·맺음말은 쓰지 마세요.

반드시 아래 형태의 JSON 객체 하나만 출력합니다(코드펜스 금지):
{
  "visual_style_prompt": "모든 이미지와 영상 클립에 공통 적용할 스타일 설명(한국어)",
  "reference_image_prompt": "모든 씬에서 유지할 대표 참조 이미지 생성 프롬프트(인물, 의상, 배경, 소품, 색감, 조명 포함)",
  "total_clip_seconds": 60,
  "estimated_voice_seconds": 58,
  "total_narration_chars": 203,
  "within_target": true,
  "scenes": [
    {
      "scene_id": 1,
      "narration_ko": "이 씬에서 말할 나레이션 전문(한국어)",
      "visual_prompt_ko": "배경·자료화면에 쓸 비주얼 설명(한국어)",
      "video_prompt_ko": "이미지를 영상으로 만들 때 쓸 움직임·카메라·무음 조건 설명(한국어)",
      "clip_seconds": {example_clip_seconds},
      "transition": "fade"
    }
  ]
}

규칙:
- scenes 배열에 순서대로 씬을 넣습니다. scene_id는 1부터 연속 번호입니다.
- visual_style_prompt, reference_image_prompt, narration_ko, visual_prompt_ko, video_prompt_ko는 모두 한국어로 작성합니다.
- visual_style_prompt에는 모든 이미지와 영상 클립에 공통 적용할 스타일을 작성합니다. 매체(예: 3D 애니메이션, 수채화, 실사풍), 색감, 조명, 캐릭터 비율, 질감, 카메라 톤을 포함합니다.
- reference_image_prompt에는 모든 씬에서 유지할 대표 참조 이미지 생성을 위한 설명을 작성합니다. 인물/캐릭터 정체성, 의상, 배경 구조, 핵심 소품, 색감, 조명, 질감을 포함합니다.
- visual_prompt_ko는 이미지 생성에 참고할 짧은 장면 묘사입니다. 장면 내용 중심으로 쓰고, 전체 스타일 일관성은 visual_style_prompt에 모읍니다.
- video_prompt_ko는 영상 클립 생성에 참고할 움직임, 카메라, 연출 묘사입니다. 반드시 무음 영상, 대사 없음, 내레이션 없음, 배경음악 없음, 효과음 없음 조건을 포함합니다.
- 각 씬은 clip_seconds 길이의 영상 클립 하나에 대응합니다.
- clip_seconds는 {allowed_clip_seconds_text} 중 하나입니다. narration_ko 길이에 맞춰 배정합니다.
- narration_ko는 사용자 메시지의 타임라인 배분 조건에 맞춰 작성합니다.
- 한 씬의 narration_ko가 해당 clip_seconds에 비해 너무 짧거나 길지 않게 조절합니다.
- 설명이 길어질 것 같으면 한 씬에 몰아넣지 말고 두 씬 이상으로 나눕니다.
- total_clip_seconds는 scenes의 clip_seconds 합계입니다.
- estimated_voice_seconds는 전체 narration_ko의 예상 TTS 발화 시간입니다.
- total_narration_chars는 전체 narration_ko의 공백 제외 글자 수입니다.
- within_target는 목표 길이 조건을 모두 만족할 때만 true입니다. false가 될 결과는 출력하지 말고 다시 조정합니다.
- transition은 "fade" 또는 "cut" 중 하나만 사용합니다.
- 목표 영상 길이(분), 허용 clip_seconds, 사용자 메시지의 권장 씬 수 범위에 맞게 씬 개수를 조절합니다.
- 전체 내용은 한국어로 작성합니다.
"""


def build_system_scene_json_prompt(*, allowed_clip_seconds: tuple[int, ...] = (4, 6, 8)) -> str:
    values = tuple(int(v) for v in allowed_clip_seconds) or (4, 6, 8)
    text = ", ".join(str(v) for v in values)
    example_clip_seconds = str(values[-1])
    return (
        SYSTEM_SCENE_JSON_KO_TEMPLATE
        .replace("{allowed_clip_seconds_text}", text)
        .replace("{example_clip_seconds}", example_clip_seconds)
    )


def narration_length_limits(seconds: int) -> tuple[int, int, int]:
    value = max(1, int(seconds))
    recommended_min = max(6, int(value * 3.0))
    recommended_max = max(recommended_min + 2, int(value * 4.2))
    hard_max = max(recommended_max, int(value * 4.8))
    return recommended_min, recommended_max, hard_max


def _narration_length_rule(seconds: int) -> str:
    value = max(1, int(seconds))
    recommended_min, recommended_max, hard_max = narration_length_limits(value)
    return f"{value}초는 권장 {recommended_min}~{recommended_max}자, 절대 최대 {hard_max}자"


def build_narration_length_rules(allowed_clip_seconds: tuple[int, ...]) -> str:
    values = tuple(int(v) for v in allowed_clip_seconds) or (4, 6, 8)
    return "; ".join(_narration_length_rule(v) for v in values)


SYSTEM_SCENE_JSON_KO = build_system_scene_json_prompt()


def strip_markdown_json_fence(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def build_user_message(
    *,
    prompt_ko: str,
    target_minutes: int,
    resolution: str,
    fps: int,
) -> str:
    return (
        f"다음 조건으로 씬 목록을 생성해 주세요.\n\n"
        f"[영상 설정]\n"
        f"- 목표 길이: 약 {target_minutes}분\n"
        f"- 해상도: {resolution}\n"
        f"- FPS: {fps}\n\n"
        f"[기획·프롬프트]\n{prompt_ko.strip()}\n"
    )


def parse_storyboard_json_payload(content: str) -> tuple[list[Scene], str, str]:
    raw = strip_markdown_json_fence(content)
    data = json.loads(raw)
    visual_style_prompt = ""
    reference_image_prompt = ""
    if isinstance(data, dict):
        visual_style_prompt = str(data.get("visual_style_prompt", "") or "").strip()
        reference_image_prompt = str(data.get("reference_image_prompt", "") or "").strip()
    return parse_scenes_json_payload(content), visual_style_prompt, reference_image_prompt


def parse_scenes_json_payload(content: str) -> list[Scene]:
    raw = strip_markdown_json_fence(content)
    data = json.loads(raw)
    if isinstance(data, list):
        scenes_raw = data
    elif isinstance(data, dict):
        scenes_raw = data.get("scenes")
        if not isinstance(scenes_raw, list):
            raise ValueError("JSON에 scenes 배열이 없습니다.")
    else:
        raise ValueError("JSON 루트는 객체 또는 배열이어야 합니다.")

    scenes: list[Scene] = []
    for i, item in enumerate(scenes_raw):
        if not isinstance(item, dict):
            raise ValueError(f"scenes[{i}]가 객체가 아닙니다.")
        scenes.append(Scene.from_llm_dict(item, index=i))

    if not scenes:
        raise ValueError("씬이 0개입니다. 최소 1개 필요합니다.")
    return scenes
