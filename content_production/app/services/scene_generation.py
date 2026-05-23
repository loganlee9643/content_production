from __future__ import annotations

import json
import re
from typing import Any

from app.models.storyboard import Scene


SYSTEM_SCENE_JSON_KO = """당신은 한국어 YouTube 영상용 스토리보드 작성 보조입니다.
사용자 요구에 맞춰 씬 목록을 JSON으로만 출력하세요. 다른 설명·서문·맺음말은 쓰지 마세요.

반드시 아래 형태의 JSON 객체 하나만 출력합니다(코드펜스 금지):
{
  "scenes": [
    {
      "scene_id": 1,
      "narration_ko": "이 씬에서 말할 나레이션 전문(한국어)",
      "visual_prompt_ko": "배경·자료화면에 쓸 비주얼 설명(한국어)",
      "transition": "fade"
    }
  ]
}

규칙:
- scenes 배열에 순서대로 씬을 넣습니다. scene_id는 1부터 연속 번호입니다.
- narration_ko는 한 씬당 1~4문장, 말로 읽었을 때 과하지 않게(너무 길지 않게) 작성합니다.
- visual_prompt_ko는 이미지·영상 소스를 고를 때 참고할 짧은 묘사입니다.
- transition은 "fade" 또는 "cut" 중 하나만 사용합니다.
- 목표 영상 길이(분)에 맞게 씬 개수를 조절합니다(대략 분당 8~20씬 범위에서 합리적으로).
- 전체 내용은 한국어로 작성합니다.
"""


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
