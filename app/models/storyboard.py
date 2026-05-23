from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PROJECT_KIND_STORYBOARD = "storyboard"
PROJECT_KIND_WAV_SEQUENCE = "wav_sequence"
PROJECT_KIND_VIDEO_PRODUCTION = "video_production"


def normalize_project_kind(raw: str) -> str:
    s = (raw or "").strip()
    if s in (PROJECT_KIND_STORYBOARD, PROJECT_KIND_WAV_SEQUENCE, PROJECT_KIND_VIDEO_PRODUCTION):
        return s
    return PROJECT_KIND_STORYBOARD


def normalize_clip_seconds(raw: object, default: int = 8) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    if value <= 0:
        return 0 if int(default) <= 0 else normalize_clip_seconds(default, 8)
    if value <= 4:
        return 4
    if value <= 6:
        return 6
    return 8


@dataclass
class Scene:
    scene_id: int
    narration_ko: str = ""
    visual_prompt_ko: str = ""
    video_prompt_ko: str = ""
    clip_seconds: int = 8
    transition: str = "fade"
    notes: str = ""
    audio_relpath: str = ""
    image_relpath: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Scene:
        return Scene(
            scene_id=int(d["scene_id"]),
            narration_ko=str(d.get("narration_ko", "")),
            visual_prompt_ko=str(d.get("visual_prompt_ko", "")),
            video_prompt_ko=str(d.get("video_prompt_ko", "")),
            clip_seconds=normalize_clip_seconds(d.get("clip_seconds", 0), 0),
            transition=str(d.get("transition", "fade")),
            notes=str(d.get("notes", "")),
            audio_relpath=str(d.get("audio_relpath", "")),
            image_relpath=str(d.get("image_relpath", "")),
        )

    @staticmethod
    def from_llm_dict(d: dict[str, Any], *, index: int) -> Scene:
        sid_raw = d.get("scene_id")
        try:
            scene_id = int(sid_raw) if sid_raw is not None else index + 1
        except (TypeError, ValueError):
            scene_id = index + 1
        return Scene(
            scene_id=scene_id,
            narration_ko=str(d.get("narration_ko", "")).strip(),
            visual_prompt_ko=str(d.get("visual_prompt_ko", "")).strip(),
            video_prompt_ko=str(d.get("video_prompt_ko", "")).strip(),
            clip_seconds=normalize_clip_seconds(d.get("clip_seconds", 8)),
            transition=str(d.get("transition", "fade") or "fade").strip() or "fade",
            notes=str(d.get("notes", "")).strip(),
            audio_relpath=str(d.get("audio_relpath", "")).strip(),
            image_relpath=str(d.get("image_relpath", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StoryProject:
    prompt_ko: str = ""
    project_kind: str = PROJECT_KIND_STORYBOARD
    target_minutes: int = 7
    resolution: str = "1920x1080"
    fps: int = 30
    merged_srt_relpath: str = ""
    export_final_relpath: str = ""
    background_image_relpath: str = ""
    bgm_relpath: str = ""
    bgm_volume_percent: int = 20
    scenes: list[Scene] = field(default_factory=list)

    FORMAT_ID = "content-production-storyboard"
    FORMAT_VERSION = 1

    @staticmethod
    def empty_default(*, project_kind: str = PROJECT_KIND_STORYBOARD) -> StoryProject:
        pk = normalize_project_kind(project_kind)
        return StoryProject(
            prompt_ko="",
            project_kind=pk,
            scenes=[
                Scene(scene_id=1, narration_ko="", visual_prompt_ko=""),
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.FORMAT_ID,
            "version": self.FORMAT_VERSION,
            "prompt_ko": self.prompt_ko,
            "settings": {
                "target_minutes": self.target_minutes,
                "resolution": self.resolution,
                "fps": self.fps,
                "merged_srt_relpath": self.merged_srt_relpath,
                "export_final_relpath": self.export_final_relpath,
                "background_image_relpath": self.background_image_relpath,
                "bgm_relpath": self.bgm_relpath,
                "bgm_volume_percent": self.bgm_volume_percent,
                "project_kind": self.project_kind,
            },
            "scenes": [s.to_dict() for s in self.scenes],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> StoryProject:
        if data.get("format") != StoryProject.FORMAT_ID:
            raise ValueError("지원하지 않는 프로젝트 형식입니다.")
        ver = int(data.get("version", 0))
        if ver != StoryProject.FORMAT_VERSION:
            raise ValueError(f"지원하지 않는 버전입니다: {ver}")
        settings = data.get("settings") or {}
        scenes_raw = data.get("scenes") or []
        scenes = [Scene.from_dict(s) for s in scenes_raw]
        scenes.sort(key=lambda s: s.scene_id)
        return StoryProject(
            prompt_ko=str(data.get("prompt_ko", "")),
            target_minutes=int(settings.get("target_minutes", 7)),
            resolution=str(settings.get("resolution", "1920x1080")),
            fps=int(settings.get("fps", 30)),
            merged_srt_relpath=str(settings.get("merged_srt_relpath", "")),
            export_final_relpath=str(settings.get("export_final_relpath", "")),
            background_image_relpath=str(settings.get("background_image_relpath", "")),
            bgm_relpath=str(settings.get("bgm_relpath", "")),
            bgm_volume_percent=max(
                1,
                min(50, int(float(settings.get("bgm_volume_percent", 20)))),
            ),
            project_kind=normalize_project_kind(str(settings.get("project_kind", PROJECT_KIND_STORYBOARD))),
            scenes=scenes,
        )

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def load_json(path: Path) -> StoryProject:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("JSON 루트는 객체여야 합니다.")
        return StoryProject.from_dict(raw)

    def renumber_scene_ids(self) -> None:
        for i, s in enumerate(self.scenes, start=1):
            s.scene_id = i
