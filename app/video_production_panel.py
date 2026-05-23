from __future__ import annotations

import json
import mimetypes
import os
import math
import re
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.models.storyboard import Scene, StoryProject
from app.services.elevenlabs_client import synthesize_speech_with_timestamps
from app.services.elevenlabs_voice_client import (
    ElevenLabsVoiceCandidate,
    recommend_voices,
)
from app.services.ffmpeg_render import concat_segments_copy, run_ffmpeg, which_ffmpeg
from app.services.ffprobe_audio import FfprobeError, ffprobe_duration_seconds
from app.services.gemini_client import gemini_generate_content
from app.services.gemini_image_client import (
    GeminiImageApiError,
    api_aspect_ratio_from_resolution,
    gemini_generate_image,
)
from app.services.gemini_image_model_catalog import (
    DEFAULT_GEMINI_IMAGE_MODEL,
    GEMINI_IMAGE_MODEL_PRESET_IDS,
)
from app.services.gemini_tts_client import synthesize_gemini_speech
from app.services.scene_generation import SYSTEM_SCENE_JSON_KO, build_user_message, parse_storyboard_json_payload
from app.services.srt_build import parse_srt_file, seconds_to_srt_timestamp, split_narration_lines
from app.services.veo_video_client import generate_video_from_image


VOICE_PROVIDER_ELEVENLABS = "elevenlabs"
VOICE_PROVIDER_GEMINI_TTS = "gemini_tts"
DEFAULT_GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_GEMINI_TTS_VOICE = "Kore"
DEFAULT_GEMINI_TTS_STYLE = "한국어로 자연스럽고 또렷한 내레이션으로 읽어줘."


def _suffix_for_mime(mime: str) -> str:
    return mimetypes.guess_extension(mime or "") or ".png"


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _compose_final_video(
    *,
    ffmpeg: str,
    input_video: Path,
    narration_audio: Path | None,
    srt_path: Path | None,
    output_video: Path,
    cwd: Path,
    video_pad_sec: float = 0.0,
) -> None:
    output_video.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-i", str(input_video.resolve())]
    if narration_audio is not None:
        cmd.extend(["-i", str(narration_audio.resolve())])
    cmd.extend(["-map", "0:v:0"])
    if narration_audio is not None:
        cmd.extend(["-map", "1:a:0"])
    vf_parts: list[str] = []
    if video_pad_sec > 0.01:
        vf_parts.append(f"tpad=stop_mode=clone:stop_duration={video_pad_sec:.3f}")
    if srt_path is not None:
        rel_srt = srt_path.resolve().relative_to(cwd.resolve()).as_posix()
        vf_parts.append(f"subtitles='{rel_srt}':charenc=UTF-8")
    if vf_parts:
        cmd.extend(["-vf", ",".join(vf_parts)])
        cmd.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "22"])
    else:
        cmd.extend(["-c:v", "copy"])
    if narration_audio is not None:
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.extend(["-an"])
    cmd.append(str(output_video.resolve()))
    run_ffmpeg(cmd, cwd=cwd)


def _duration_srt(text: str, duration_sec: float, *, max_line_chars: int) -> str:
    lines = split_narration_lines(text, max_line_chars)
    if not lines:
        raise RuntimeError("자막으로 만들 대본이 없습니다.")
    step = max(0.04, float(duration_sec)) / len(lines)
    blocks: list[str] = []
    t = 0.0
    for idx, line in enumerate(lines, start=1):
        end = t + step
        blocks.extend(
            [
                str(idx),
                f"{seconds_to_srt_timestamp(t)} --> {seconds_to_srt_timestamp(end)}",
                line,
                "",
            ]
        )
        t = end
    return "\n".join(blocks).rstrip() + "\n"


def _media_duration(path: Path | None) -> float:
    if path is None or not path.is_file():
        return 0.0
    try:
        return max(0.0, float(ffprobe_duration_seconds(path.resolve())))
    except FfprobeError:
        return 0.0


def _auto_scene_count(target_minutes: int, default_clip_seconds: int) -> int:
    total_seconds = max(1, int(target_minutes)) * 60
    clip_seconds = max(4, min(8, int(default_clip_seconds)))
    return max(1, min(120, math.ceil(total_seconds / clip_seconds)))


def _target_seconds(target_minutes: int) -> int:
    return max(1, int(target_minutes)) * 60


def _estimate_narration_seconds(narration: str) -> float:
    count = _narration_char_count(narration)
    if count <= 0:
        return 0.0
    sentence_count = max(1, len([s for s in re.split(r"[.!?。！？\n]+", narration) if s.strip()]))
    pause_seconds = max(0, sentence_count - 1) * 0.5
    return max(1.2, count / 3.5 + pause_seconds)


def _narration_char_count(text: str) -> int:
    return len("".join(ch for ch in text.strip() if not ch.isspace()))


def _timeline_stats(scenes: list[Scene], *, target_minutes: int) -> dict[str, float]:
    target = float(_target_seconds(target_minutes))
    clip_total = float(sum(_normalize_clip_seconds(s.clip_seconds, 8) for s in scenes))
    voice_estimate = float(sum(_estimate_narration_seconds(s.narration_ko) for s in scenes))
    return {
        "target": target,
        "clip_total": clip_total,
        "voice_estimate": voice_estimate,
        "clip_delta": clip_total - target,
        "voice_delta": voice_estimate - target,
    }


def _timeline_validation_error(stats: dict[str, float]) -> str:
    target = float(stats["target"])
    clip_total = float(stats["clip_total"])
    voice_estimate = float(stats["voice_estimate"])
    clip_min = target - 4.0
    clip_max = target + 4.0
    voice_max = target + 4.0
    problems: list[str] = []
    if clip_total < clip_min or clip_total > clip_max:
        problems.append(f"클립 합계 {clip_total:.0f}s(허용 {clip_min:.0f}~{clip_max:.0f}s)")
    if voice_estimate > voice_max:
        problems.append(f"예상 음성 {voice_estimate:.1f}s(최대 {voice_max:.0f}s)")
    return ", ".join(problems)


def _write_text_debug_file(project_parent: Path, filename: str, text: str) -> Path:
    debug_dir = project_parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


def _normalize_clip_seconds(raw: object, default_clip_seconds: int = 8) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default_clip_seconds)
    if value <= 0:
        value = int(default_clip_seconds)
    if value <= 4:
        return 4
    if value <= 6:
        return 6
    return 8


def _srt_duration(path: Path | None) -> float:
    if path is None or not path.is_file():
        return 0.0
    cues = parse_srt_file(path.resolve())
    return max((end for _start, end, _text in cues), default=0.0)


class VideoProductionWorker(QThread):
    log_line = Signal(str)
    progress = Signal(int, int)
    succeeded = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, step: int, args: dict[str, Any]) -> None:
        super().__init__()
        self._step = step
        self._args = dict(args)

    def run(self) -> None:
        try:
            if self._step == 1:
                result = self._script()
            elif self._step == 2:
                result = self._images()
            elif self._step == 3:
                result = self._videos()
            elif self._step == 4:
                result = self._concat()
            elif self._step == 5:
                result = self._voice()
            elif self._step == 6:
                result = self._supplement_clips()
            elif self._step == 7:
                result = self._subtitles()
            elif self._step == 8:
                result = self._final()
            else:
                raise RuntimeError(f"Unknown step: {self._step}")
            self.succeeded.emit(self._step, result)
        except Exception as e:
            self.failed.emit(self._step, str(e))

    def _script(self) -> dict[str, object]:
        target_seconds = _target_seconds(int(self._args["target_minutes"]))
        voice_max_chars = int(target_seconds * 3.5)
        msg = build_user_message(
            prompt_ko=str(self._args["prompt"]),
            target_minutes=int(self._args["target_minutes"]),
            resolution=str(self._args["resolution"]),
            fps=24,
        )
        msg += (
            f"\n[타임라인 배분]\n"
            f"- 목표 전체 길이: 약 {target_seconds}초\n"
            f"- 허용 클립 길이: {target_seconds - 4}~{target_seconds + 4}초\n"
            f"- 허용 예상 음성 길이: 최대 {target_seconds + 4}초\n"
            f"- 전체 narration_ko 총 글자 수(공백 제외): 최대 약 {voice_max_chars}자\n"
            f"- 목표 씬 수: 약 {int(self._args['max_scenes'])}개\n"
            f"- 한 씬의 최대 클립 길이: {int(self._args['clip_seconds'])}초\n"
            "- 위 길이 조건을 만족하도록 씬 수와 씬별 대본 길이를 조절하세요.\n"
        )
        project_parent = Path(str(self._args["project_parent"])).resolve()
        system_instruction = self._script_system_instruction()
        try:
            system_path = _write_text_debug_file(project_parent, "last_script_system_prompt.txt", system_instruction)
            user_path = _write_text_debug_file(project_parent, "last_script_user_prompt.txt", msg)
            request_path = _write_text_debug_file(
                project_parent,
                "last_script_request.json",
                json.dumps(
                    {
                        "model": str(self._args["gemini_text_model"]),
                        "system_instruction": system_instruction,
                        "user_text": msg,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            self.log_line.emit(f"LLM 요청 프롬프트 저장: {system_path}, {user_path}, {request_path}")
        except OSError as e:
            self.log_line.emit(f"LLM 요청 프롬프트 저장 실패: {e}")
        content = ""
        last_error = ""
        first_model = str(self._args["gemini_text_model"])
        chosen_model = ""
        for model in self._script_models_to_try():
            try:
                content = gemini_generate_content(
                    str(self._args["gemini_api_key"]),
                    model,
                    system_instruction=system_instruction,
                    user_text=msg,
                    max_retries=3,
                )
                if model != first_model:
                    self.log_line.emit(f"대본 생성 모델 fallback 사용: {model}")
                chosen_model = model
                break
            except Exception as e:
                last_error = str(e)
                retryable = any(token in last_error for token in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"))
                if not retryable:
                    raise
                self.log_line.emit(f"대본 생성 모델 {model} 일시 실패: {last_error}")
        if not content:
            raise RuntimeError(last_error or "대본 생성에 실패했습니다.")
        default_clip_seconds = int(self._args["clip_seconds"])

        def prepare(raw: str) -> tuple[list[Scene], str, dict[str, float]]:
            prepared_scenes, style = parse_storyboard_json_payload(raw)
            for i, scene in enumerate(prepared_scenes, start=1):
                scene.scene_id = i
                scene.clip_seconds = _normalize_clip_seconds(scene.clip_seconds, default_clip_seconds)
            prepared_stats = _timeline_stats(prepared_scenes, target_minutes=int(self._args["target_minutes"]))
            return prepared_scenes, style, prepared_stats

        scenes, visual_style_prompt, stats = prepare(content)
        validation_error = _timeline_validation_error(stats)
        self.log_line.emit(
            "대본 타임라인 예상: "
            f"목표 {stats['target']:.0f}s / 클립 합계 {stats['clip_total']:.0f}s / "
            f"예상 음성 {stats['voice_estimate']:.1f}s"
        )
        if validation_error:
            self.log_line.emit(f"대본 타임라인 경고: {validation_error}")
        self.progress.emit(len(scenes), len(scenes))
        return {
            "scenes": scenes,
            "visual_style_prompt": visual_style_prompt,
            "timeline_warning": validation_error,
            "timeline_stats": stats,
        }

    def _script_system_instruction(self) -> str:
        return (
            SYSTEM_SCENE_JSON_KO
            + "\n\n추가 요구사항:\n"
            + "- JSON 루트에 visual_style_prompt 문자열 필드를 반드시 포함하세요.\n"
            + "- visual_style_prompt에는 모든 이미지와 영상 클립에 공통 적용할 스타일을 한국어로 작성하세요.\n"
            + "- 스타일에는 매체(예: 3D 애니메이션, 수채화, 실사풍), 색감, 조명, 캐릭터 비율, 질감, 카메라 톤을 포함하세요.\n"
            + "- 각 visual_prompt_ko는 장면 내용 중심으로 쓰고, 전체 스타일 일관성은 visual_style_prompt에 모으세요.\n"
            + "- 각 video_prompt_ko는 해당 이미지가 영상으로 변할 때의 움직임, 카메라, 연출만 작성하세요.\n"
            + "- video_prompt_ko에는 반드시 무음 영상, 대사 없음, 내레이션 없음, 배경음악 없음, 효과음 없음 조건을 포함하세요.\n"
            + "예: {\"visual_style_prompt\":\"밝고 따뜻한 3D 애니메이션, 둥근 캐릭터, 파스텔 색감, 부드러운 확산광, 동일한 동화책 같은 카메라 톤\", \"total_clip_seconds\":60, \"estimated_voice_seconds\":58, \"total_narration_chars\":203, \"within_target\":true, \"scenes\":[{\"scene_id\":1,\"narration_ko\":\"...\",\"visual_prompt_ko\":\"...\",\"video_prompt_ko\":\"천천히 앞으로 다가가는 카메라, 구름이 부드럽게 움직이는 무음 영상. 대사 없음, 내레이션 없음, 배경음악 없음, 효과음 없음.\",\"clip_seconds\":8,\"transition\":\"fade\"}]}\n"
        )

    def _script_models_to_try(self) -> list[str]:
        first = str(self._args.get("gemini_text_model", "")).strip()
        models: list[str] = []
        for model in (first, "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash"):
            if model and model not in models:
                models.append(model)
        return models

    def _images(self) -> list[tuple[int, str]]:
        parent = Path(self._args["project_parent"]).resolve()
        scenes: list[Scene] = self._args["scenes"]
        aspect = api_aspect_ratio_from_resolution(str(self._args["resolution"]))
        model_candidates = self._image_models_to_try()
        out: list[tuple[int, str]] = []
        for i, scene in enumerate(scenes, start=1):
            self.log_line.emit(f"씬 {scene.scene_id}: 이미지 생성")
            self.progress.emit(i, len(scenes))
            raw: bytes | None = None
            mime = "image/png"
            last_error = ""
            prompt = self._image_prompt(scene)
            for model in model_candidates:
                try:
                    raw, mime = gemini_generate_image(
                        str(self._args["gemini_api_key"]),
                        model,
                        prompt=prompt,
                        aspect_ratio=aspect,
                    )
                    if model != str(self._args["gemini_image_model"]):
                        self.log_line.emit(f"씬 {scene.scene_id}: 이미지 모델 fallback 사용 - {model}")
                    break
                except GeminiImageApiError as e:
                    last_error = str(e)
                    self.log_line.emit(f"씬 {scene.scene_id}: {model} 실패, 다음 이미지 모델 시도")
            if raw is None:
                raise GeminiImageApiError(last_error or "이미지 생성에 실패했습니다.")
            path = parent / "images" / "video_production" / f"scene_{scene.scene_id:03d}{_suffix_for_mime(mime)}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            out.append((scene.scene_id, _relative(path, parent)))
        return out

    def _image_models_to_try(self) -> list[str]:
        first = str(self._args.get("gemini_image_model", "")).strip()
        models: list[str] = []
        if first:
            models.append(first)
        for model in (DEFAULT_GEMINI_IMAGE_MODEL, *GEMINI_IMAGE_MODEL_PRESET_IDS):
            if model and model not in models:
                models.append(model)
        return models

    def _image_prompt(self, scene: Scene) -> str:
        style = str(self._args.get("visual_style_prompt", "") or "").strip()
        style_block = ""
        if style:
            style_block = (
                f"\n\uacf5\ud1b5 \uc2a4\ud0c0\uc77c \uac00\uc774\ub4dc:\n{style}\n"
                "\ubaa8\ub4e0 \uc52c\uc5d0\uc11c \uc774 \uc2a4\ud0c0\uc77c, \uc0c9\uac10, \uc870\uba85, \uce90\ub9ad\ud130 \ube44\uc728, \uc9c8\uac10, \uce74\uba54\ub77c \ud1a4\uc744 \uc77c\uad00\ub418\uac8c \uc720\uc9c0\ud558\uc138\uc694.\n"
            )
        return (
            "\uc544\ub798 \uc124\uba85\uc5d0 \ub9de\ub294 \ub2e8\uc77c \ubc30\uacbd \uc774\ubbf8\uc9c0\ub97c \ubc18\ub4dc\uc2dc \uc0dd\uc131\ud558\uc138\uc694.\n"
            "\ud14d\uc2a4\ud2b8 \uc124\uba85\ub9cc \ud558\uc9c0 \ub9d0\uace0 \uc774\ubbf8\uc9c0 1\uc7a5\uc744 \ubc18\ud658\ud558\uc138\uc694.\n"
            "\uc774\ubbf8\uc9c0 \uc548\uc5d0\ub294 \uae00\uc790, \uc790\ub9c9, \ub85c\uace0, \uc6cc\ud130\ub9c8\ud06c\ub97c \ub123\uc9c0 \ub9c8\uc138\uc694.\n"
            f"{style_block}\n"
            f"\uc7a5\uba74 \uc124\uba85:\n{scene.visual_prompt_ko}"
        )

    def _videos(self) -> list[tuple[int, str]]:
        parent = Path(self._args["project_parent"]).resolve()
        scenes: list[Scene] = self._args["scenes"]
        aspect = api_aspect_ratio_from_resolution(str(self._args["resolution"]))
        out: list[tuple[int, str]] = []
        for i, scene in enumerate(scenes, start=1):
            if not scene.image_relpath.strip():
                raise RuntimeError(f"씬 {scene.scene_id} 이미지가 없습니다.")
            self.log_line.emit(f"씬 {scene.scene_id}: Veo 영상 생성")
            self.progress.emit(i, len(scenes))
            clip = parent / "video_clips" / f"scene_{scene.scene_id:03d}.mp4"
            video_prompt = scene.video_prompt_ko.strip() or scene.visual_prompt_ko.strip()
            clip_seconds = _normalize_clip_seconds(scene.clip_seconds, int(self._args["clip_seconds"]))
            prompt = (
                f"{self._style_prefix_for_video()}{video_prompt}\n\n"
                "Create a cinematic video from this image. Use smooth camera motion. "
                "Do not add readable on-screen text. Create a silent visual-only video. "
                "No dialogue, no narration, no voice, no background music, no sound effects."
            )
            generate_video_from_image(
                api_key=str(self._args["gemini_api_key"]),
                model=str(self._args["veo_model"]),
                prompt=prompt,
                image_path=parent / scene.image_relpath,
                out_video_path=clip,
                resolution=str(self._args["veo_resolution"]),
                aspect_ratio=aspect,
                duration_seconds=clip_seconds,
            )
            out.append((scene.scene_id, _relative(clip, parent)))
        return out

    def _concat(self) -> str:
        parent = Path(self._args["project_parent"]).resolve()
        clips = self._concat_clip_paths(parent)
        if not clips:
            raise RuntimeError("이어 붙일 영상 클립이 없습니다.")
        out = parent / "export" / "video_production_merged.mp4"
        self.progress.emit(1, 1)
        concat_segments_copy(ffmpeg=which_ffmpeg(), segment_paths=clips, out_mp4=out, cwd=parent)
        return _relative(out, parent)

    def _concat_clip_paths(self, parent: Path) -> list[Path]:
        scenes: list[Scene] = self._args["scenes"]
        clips = [
            parent / scene.notes[len("video_relpath:") :]
            for scene in scenes
            if scene.notes.startswith("video_relpath:")
        ]
        for rel in self._args.get("supplement_clip_relpaths", []):
            rel_s = str(rel).strip()
            if rel_s:
                clips.append(parent / rel_s)
        return clips

    def _supplement_clips(self) -> dict[str, object]:
        parent = Path(self._args["project_parent"]).resolve()
        merged_rel = str(self._args.get("merged_video_relpath", "") or "").strip()
        audio_rel = str(self._args.get("audio_relpath", "") or "").strip()
        if not merged_rel:
            raise RuntimeError("먼저 4단계 영상 이어 붙이기를 완료하세요.")
        if not audio_rel:
            raise RuntimeError("부족한 영상 길이를 계산하려면 먼저 5단계 음성 생성을 완료하세요.")
        merged_path = parent / merged_rel
        audio_path = parent / audio_rel
        merged_duration = _media_duration(merged_path)
        audio_duration = _media_duration(audio_path)
        deficit = audio_duration - merged_duration
        if deficit <= 3.0:
            self.progress.emit(1, 1)
            self.log_line.emit(
                f"부족 영상 채우기 생략: 영상 {merged_duration:.2f}s / 음성 {audio_duration:.2f}s"
            )
            return {
                "supplement_clips": list(self._args.get("supplement_clip_relpaths", [])),
                "merged": merged_rel,
                "deficit": deficit,
            }

        scenes: list[Scene] = self._args["scenes"]
        last_scene = next((s for s in reversed(scenes) if s.image_relpath.strip()), None)
        if last_scene is None:
            raise RuntimeError("보충 클립을 만들 기준 이미지가 없습니다. 먼저 2단계 이미지 생성을 완료하세요.")

        existing = [str(x) for x in self._args.get("supplement_clip_relpaths", []) if str(x).strip()]
        supplement_paths = list(existing)
        clip_seconds = max(4, min(8, int(self._args["clip_seconds"])))
        clips_to_make = max(1, math.ceil(max(0.0, deficit - 3.0) / clip_seconds))
        total_units = clips_to_make + 1
        aspect = api_aspect_ratio_from_resolution(str(self._args["resolution"]))
        start_index = len(existing) + 1
        for i in range(clips_to_make):
            clip = parent / "video_clips" / f"supplement_{start_index + i:03d}.mp4"
            prompt = self._supplement_prompt(last_scene, i + 1)
            self.log_line.emit(f"보충 클립 생성 {i + 1}/{clips_to_make}: {clip_seconds}s")
            self.progress.emit(i, total_units)
            generate_video_from_image(
                api_key=str(self._args["gemini_api_key"]),
                model=str(self._args["veo_model"]),
                prompt=prompt,
                image_path=parent / last_scene.image_relpath,
                out_video_path=clip,
                resolution=str(self._args["veo_resolution"]),
                aspect_ratio=aspect,
                duration_seconds=clip_seconds,
            )
            supplement_paths.append(_relative(clip, parent))
            self.progress.emit(i + 1, total_units)

        out = parent / "export" / "video_production_merged.mp4"
        scene_clips = [
            parent / scene.notes[len("video_relpath:") :]
            for scene in scenes
            if scene.notes.startswith("video_relpath:")
        ]
        self.log_line.emit("보충 클립을 포함해 영상을 다시 이어 붙이는 중")
        self.progress.emit(clips_to_make, total_units)
        concat_segments_copy(
            ffmpeg=which_ffmpeg(),
            segment_paths=scene_clips + [parent / rel for rel in supplement_paths],
            out_mp4=out,
            cwd=parent,
        )
        self.progress.emit(total_units, total_units)
        return {
            "supplement_clips": supplement_paths,
            "merged": _relative(out, parent),
            "deficit": deficit,
        }

    def _supplement_prompt(self, scene: Scene, index: int) -> str:
        video_prompt = scene.video_prompt_ko.strip() or scene.visual_prompt_ko.strip()
        return (
            f"{self._style_prefix_for_video()}{video_prompt}\n\n"
            "Create a natural continuation shot from this final scene image. "
            "Keep the same characters, background, colors, and mood. "
            "Make it feel like a calm ending or reflective continuation, without introducing a new event. "
            "Use smooth cinematic motion and do not add readable on-screen text. "
            "Create a silent visual-only video. No dialogue, no narration, no voice, no background music, no sound effects. "
            f"This is supplemental continuation clip {index}."
        )

    def _style_prefix_for_video(self) -> str:
        style = str(self._args.get("visual_style_prompt", "") or "").strip()
        if not style:
            return ""
        return (
            f"Shared visual style for the whole project: {style}\n"
            "Keep this style consistent with previous scenes.\n\n"
        )

    def _supplement_base_scene(self) -> Scene | None:
        return next((s for s in reversed(self._scenes) if s.image_relpath.strip()), None)

    def _narration_text(self) -> str:
        scenes: list[Scene] = self._args["scenes"]
        return "\n\n".join(s.narration_ko for s in scenes if s.narration_ko.strip()).strip()

    def _voice(self) -> str:
        parent = Path(self._args["project_parent"]).resolve()
        text = self._narration_text()
        if not text.strip():
            raise RuntimeError("음성으로 만들 대본이 없습니다.")
        self.progress.emit(1, 1)
        provider = str(self._args.get("voice_provider", VOICE_PROVIDER_ELEVENLABS) or VOICE_PROVIDER_ELEVENLABS)
        if provider == VOICE_PROVIDER_GEMINI_TTS:
            result = synthesize_gemini_speech(
                text=text,
                out_audio_path=parent / "audio" / "video_production_narration.wav",
                out_srt_path=parent / "subs" / "video_production_voice_alignment.srt",
                api_key=str(self._args["gemini_api_key"]),
                model_id=str(self._args["gemini_tts_model"]),
                voice_name=str(self._args["gemini_tts_voice_name"]),
                style_prompt=str(self._args["gemini_tts_style_prompt"]),
                max_line_chars=int(self._args["max_subtitle_chars"]),
            )
            return _relative(result.audio_path, parent)

        result = synthesize_speech_with_timestamps(
            text=text,
            voice_id=str(self._args["elevenlabs_voice_id"]),
            out_audio_path=parent / "audio" / "video_production_narration.mp3",
            out_srt_path=parent / "subs" / "video_production_voice_alignment.srt",
            api_key=str(self._args["elevenlabs_api_key"]),
            model_id=str(self._args["elevenlabs_model"]),
            language_code="ko",
            max_line_chars=int(self._args["max_subtitle_chars"]),
        )
        return _relative(result.audio_path, parent)

    def _subtitles(self) -> str:
        parent = Path(self._args["project_parent"]).resolve()
        text = self._narration_text()
        if not text.strip():
            raise RuntimeError("자막으로 만들 대본이 없습니다.")
        audio_rel = str(self._args.get("audio_relpath", "") or "").strip()
        merged_rel = str(self._args.get("merged_video_relpath", "") or "").strip()
        alignment = parent / "subs" / "video_production_voice_alignment.srt"
        out = parent / "subs" / "video_production_narration.srt"
        if audio_rel and alignment.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(alignment.read_text(encoding="utf-8"), encoding="utf-8")
            self.progress.emit(1, 1)
            return _relative(out, parent)
        duration = 0.0
        if audio_rel:
            try:
                duration = ffprobe_duration_seconds((parent / audio_rel).resolve())
            except FfprobeError:
                duration = 0.0
        if duration <= 0 and merged_rel:
            try:
                duration = ffprobe_duration_seconds((parent / merged_rel).resolve())
            except FfprobeError:
                duration = 0.0
        if duration <= 0:
            scenes: list[Scene] = self._args["scenes"]
            duration = max(
                0.04,
                sum(_normalize_clip_seconds(s.clip_seconds, int(self._args["clip_seconds"])) for s in scenes),
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            _duration_srt(text, duration, max_line_chars=int(self._args["max_subtitle_chars"])),
            encoding="utf-8",
        )
        self.progress.emit(1, 1)
        return _relative(out, parent)

    def _final(self) -> str:
        parent = Path(self._args["project_parent"]).resolve()
        out = parent / "export" / "video_production_final.mp4"
        self.progress.emit(1, 1)
        use_voice = bool(self._args.get("use_voice_in_final", True))
        use_subtitles = bool(self._args.get("use_subtitles_in_final", True))
        audio_path = parent / str(self._args["audio_relpath"]) if use_voice else None
        srt_path = parent / str(self._args["srt_relpath"]) if use_subtitles else None
        if audio_path is not None and not audio_path.is_file():
            raise RuntimeError("최종 영상에 입힐 음성 파일이 없습니다. 음성을 생략하거나 5단계를 실행하세요.")
        if srt_path is not None and not srt_path.is_file():
            raise RuntimeError("최종 영상에 입힐 자막 파일이 없습니다. 자막을 생략하거나 6단계를 실행하세요.")
        input_video = parent / str(self._args["merged_video_relpath"])
        video_duration = _media_duration(input_video)
        audio_duration = _media_duration(audio_path)
        subtitle_duration = _srt_duration(srt_path)
        target_duration = max(video_duration, audio_duration, subtitle_duration)
        video_pad_sec = max(0.0, target_duration - video_duration)
        if video_pad_sec > 0.01:
            self.log_line.emit(
                f"최종 영상 길이 보정: 영상 {video_duration:.2f}s → {target_duration:.2f}s "
                f"(마지막 프레임 {video_pad_sec:.2f}s 연장)"
            )
        _compose_final_video(
            ffmpeg=which_ffmpeg(),
            input_video=input_video,
            narration_audio=audio_path,
            srt_path=srt_path,
            output_video=out,
            cwd=parent,
            video_pad_sec=video_pad_sec,
        )
        return _relative(out, parent)


class VoiceSearchWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, *, api_key: str, topic_text: str) -> None:
        super().__init__()
        self._api_key = api_key
        self._topic_text = topic_text

    def run(self) -> None:
        try:
            self.succeeded.emit(
                recommend_voices(
                    api_key=self._api_key,
                    topic_text=self._topic_text,
                    limit=6,
                )
            )
        except Exception as e:
            self.failed.emit(str(e))


class VoiceCandidateDialog(QDialog):
    def __init__(self, parent: QWidget | None, candidates: list[ElevenLabsVoiceCandidate]) -> None:
        super().__init__(parent)
        self.setWindowTitle("ElevenLabs 목소리 선택")
        self.resize(860, 420)
        self._candidates = candidates
        self._selected: ElevenLabsVoiceCandidate | None = candidates[0] if candidates else None
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(0.8)
        self._player.setAudioOutput(self._audio_output)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)

        root = QVBoxLayout(self)
        hint = QLabel("영상 주제와 voice 메타데이터를 기준으로 정렬한 후보입니다. 사용할 목소리를 선택하세요.")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels(["점수", "이름", "Voice ID", "성별", "연령", "억양", "미리듣기 URL"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        root.addWidget(self._table, stretch=1)

        self._reason = QPlainTextEdit()
        self._reason.setReadOnly(True)
        self._reason.setMaximumHeight(100)
        root.addWidget(self._reason)

        preview_row = QHBoxLayout()
        self._btn_preview = QPushButton("샘플 듣기")
        self._btn_preview.clicked.connect(self._play_selected_preview)
        self._label_preview = QLabel("")
        preview_row.addWidget(self._btn_preview)
        preview_row.addWidget(self._label_preview, stretch=1)
        root.addLayout(preview_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._fill()

    def _fill(self) -> None:
        self._table.setRowCount(0)
        for cand in self._candidates:
            row = self._table.rowCount()
            self._table.insertRow(row)
            values = [
                str(cand.score),
                cand.name,
                cand.voice_id,
                cand.gender,
                cand.age,
                cand.accent,
                cand.preview_url,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                self._table.setItem(row, col, item)
        self._table.resizeColumnsToContents()
        if self._candidates:
            self._table.selectRow(0)

    def _on_selection_changed(self) -> None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._candidates):
            self._selected = None
            self._reason.clear()
            return
        self._selected = self._candidates[row]
        self._reason.setPlainText(
            f"선택 이유:\n{self._selected.reason}\n\n설명:\n{self._selected.description or '(설명 없음)'}"
        )
        self._label_preview.setText(self._selected.preview_url or "샘플 URL 없음")
        self._btn_preview.setEnabled(bool(self._selected.preview_url))

    def _play_selected_preview(self) -> None:
        if self._selected is None or not self._selected.preview_url:
            QMessageBox.information(self, "샘플 듣기", "선택한 목소리에 미리듣기 URL이 없습니다.")
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.stop()
            return
        self._player.setSource(QUrl(self._selected.preview_url))
        self._player.play()
        self._btn_preview.setText("정지")

    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if state != QMediaPlayer.PlaybackState.PlayingState:
            self._btn_preview.setText("샘플 듣기")

    def selected_candidate(self) -> ElevenLabsVoiceCandidate | None:
        return self._selected


class VideoProductionPanel(QWidget):
    stateChanged = Signal()

    def __init__(self, project_parent_getter: Callable[[], Path | None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project_parent_getter = project_parent_getter
        self._settings = QSettings("ContentProduction", "ContentProductionApp")
        self._worker: VideoProductionWorker | None = None
        self._voice_worker: VoiceSearchWorker | None = None
        self._syncing_scene_editor = False
        self._scenes: list[Scene] = []
        self._scene_table_rows: list[dict[str, object]] = []
        self._result_table_rows: list[dict[str, str]] = []
        self._merged_video_relpath = ""
        self._supplement_clip_relpaths: list[str] = []
        self._audio_relpath = ""
        self._srt_relpath = ""
        self._final_video_relpath = ""
        self._build_ui()
        self._refresh_buttons()
        self._refresh_voice_label()
        self._refresh_length_status()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        steps = QGroupBox("단계별 진행")
        step_lay = QGridLayout(steps)
        self._buttons: list[QPushButton] = []
        for col, (label, step) in enumerate((
            ("1. 대본 생성", 1),
            ("2. 이미지 생성", 2),
            ("3. 영상 클립 생성", 3),
            ("4. 영상 이어 붙이기", 4),
            ("5. 음성 생성", 5),
            ("6. 부족 영상 채우기", 6),
            ("7. 자막 생성", 7),
            ("8. 음성/자막 입히기", 8),
        )):
            btn = QPushButton(label)
            btn.setMinimumHeight(34)
            btn.clicked.connect(lambda _checked=False, s=step: self._run_step(s))
            step_lay.addWidget(btn, 0, col)
            self._buttons.append(btn)
        root.addWidget(steps)

        top = QGroupBox("영상 제작 프로젝트")
        top_lay = QVBoxLayout(top)
        self._prompt = QTextEdit()
        self._prompt.setPlaceholderText("만들고 싶은 영상 주제, 톤, 대상 시청자를 입력하세요.")
        self._prompt.textChanged.connect(self.stateChanged.emit)
        top_lay.addWidget(self._prompt)

        self._style_prompt = QPlainTextEdit()
        self._style_prompt.setPlaceholderText(
            "공통 이미지 스타일 예: 밝고 따뜻한 3D 애니메이션, 둥근 캐릭터, 파스텔 색감, 부드러운 조명, 동일한 카메라 톤"
        )
        self._style_prompt.setMaximumHeight(72)
        self._style_prompt.textChanged.connect(self.stateChanged.emit)
        top_lay.addWidget(QLabel("공통 이미지 스타일"))
        top_lay.addWidget(self._style_prompt)

        form = QGridLayout()
        self._spin_minutes = QSpinBox()
        self._spin_minutes.setRange(1, 120)
        self._spin_minutes.setValue(1)
        self._spin_scenes = QSpinBox()
        self._spin_scenes.setRange(1, 120)
        self._spin_scenes.setValue(_auto_scene_count(self._spin_minutes.value(), 8))
        self._spin_clip = QSpinBox()
        self._spin_clip.setRange(4, 8)
        self._spin_clip.setSingleStep(2)
        self._spin_clip.setValue(8)
        self._edit_resolution = QLineEdit("1920x1080")
        self._spin_minutes.valueChanged.connect(lambda _v: self._sync_auto_scene_count())
        self._spin_clip.valueChanged.connect(lambda _v: self._sync_auto_scene_count())
        self._spin_scenes.valueChanged.connect(lambda _v: self.stateChanged.emit())
        self._edit_resolution.editingFinished.connect(self.stateChanged.emit)
        form.addWidget(QLabel("목표 분"), 0, 0)
        form.addWidget(self._spin_minutes, 0, 1)
        form.addWidget(QLabel("장면 수"), 0, 2)
        form.addWidget(self._spin_scenes, 0, 3)
        form.addWidget(QLabel("클립 초"), 0, 4)
        form.addWidget(self._spin_clip, 0, 5)
        form.addWidget(QLabel("해상도"), 0, 6)
        form.addWidget(self._edit_resolution, 0, 7)
        top_lay.addLayout(form)

        voice_row = QHBoxLayout()
        self._label_voice = QLabel("")
        self._btn_find_voice = QPushButton("어울리는 목소리 찾기")
        self._btn_find_voice.clicked.connect(self._find_matching_voice)
        self._check_use_voice = QCheckBox("최종 영상에 음성 입히기")
        self._check_use_voice.setChecked(True)
        self._check_use_voice.toggled.connect(lambda _checked: self._refresh_buttons())
        self._check_use_subtitles = QCheckBox("최종 영상에 자막 입히기")
        self._check_use_subtitles.setChecked(True)
        self._check_use_subtitles.toggled.connect(lambda _checked: self._refresh_buttons())
        voice_row.addWidget(self._label_voice, stretch=1)
        voice_row.addWidget(self._check_use_voice)
        voice_row.addWidget(self._check_use_subtitles)
        voice_row.addWidget(self._btn_find_voice)
        top_lay.addLayout(voice_row)
        self._label_length_status = QLabel("영상/음성 길이: 아직 계산되지 않았습니다.")
        self._label_length_status.setWordWrap(True)
        top_lay.addWidget(self._label_length_status)
        root.addWidget(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._scene_table = QTableWidget(0, 7)
        self._scene_table.setHorizontalHeaderLabels(["씬", "클립 초", "대본", "이미지 프롬프트", "영상 프롬프트", "이미지", "영상"])
        self._scene_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._scene_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._scene_table.itemSelectionChanged.connect(self._on_scene_selection_changed)

        table_panel = QWidget()
        table_lay = QVBoxLayout(table_panel)
        table_lay.setContentsMargins(0, 0, 0, 0)
        table_lay.addWidget(self._scene_table, stretch=3)
        scene_table_buttons = QHBoxLayout()
        self._btn_scene_add = QPushButton("씬 추가")
        self._btn_scene_add.clicked.connect(self._add_scene_after_selection)
        self._btn_scene_delete = QPushButton("씬 삭제")
        self._btn_scene_delete.clicked.connect(self._delete_selected_scene)
        scene_table_buttons.addStretch(1)
        scene_table_buttons.addWidget(self._btn_scene_add)
        scene_table_buttons.addWidget(self._btn_scene_delete)
        table_lay.addLayout(scene_table_buttons)
        table_lay.addWidget(QLabel("결과 영상"))
        self._result_table = QTableWidget(0, 3)
        self._result_table.setHorizontalHeaderLabels(["구분", "파일", "길이"])
        self._result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._result_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._result_table.itemSelectionChanged.connect(self._update_result_preview_from_selection)
        table_lay.addWidget(self._result_table, stretch=1)
        splitter.addWidget(table_panel)

        right_tabs = QTabWidget()

        edit_tab = QWidget()
        edit_lay = QVBoxLayout(edit_tab)
        self._edit_scene_title = QLabel("씬을 선택하세요.")
        edit_lay.addWidget(self._edit_scene_title)
        self._edit_scene_narration = QPlainTextEdit()
        self._edit_scene_narration.setPlaceholderText("선택한 씬의 대본")
        self._edit_scene_image_prompt = QPlainTextEdit()
        self._edit_scene_image_prompt.setPlaceholderText("선택한 씬의 이미지 프롬프트")
        self._edit_scene_video_prompt = QPlainTextEdit()
        self._edit_scene_video_prompt.setPlaceholderText("선택한 씬의 영상 프롬프트")
        for editor in (self._edit_scene_narration, self._edit_scene_image_prompt, self._edit_scene_video_prompt):
            editor.setMinimumHeight(90)
            editor.textChanged.connect(self._on_scene_editor_changed)
        self._edit_scene_clip_seconds = QSpinBox()
        self._edit_scene_clip_seconds.setRange(4, 8)
        self._edit_scene_clip_seconds.setSingleStep(2)
        self._edit_scene_clip_seconds.valueChanged.connect(lambda _v: self._on_scene_editor_changed())
        form_edit = QFormLayout()
        form_edit.addRow("클립 초", self._edit_scene_clip_seconds)
        form_edit.addRow("대본", self._edit_scene_narration)
        form_edit.addRow("이미지 프롬프트", self._edit_scene_image_prompt)
        form_edit.addRow("영상 프롬프트", self._edit_scene_video_prompt)
        edit_lay.addLayout(form_edit)
        self._label_scene_assets = QLabel("")
        self._label_scene_assets.setWordWrap(True)
        edit_lay.addWidget(self._label_scene_assets)
        edit_btn_row = QHBoxLayout()
        self._btn_scene_save = QPushButton("저장")
        self._btn_scene_save.clicked.connect(self._save_selected_scene_edits)
        self._btn_scene_image = QPushButton("씬 이미지 생성")
        self._btn_scene_image.clicked.connect(lambda: self._run_selected_scene_step(2))
        self._btn_scene_clip = QPushButton("씬 클립 영상 생성")
        self._btn_scene_clip.clicked.connect(lambda: self._run_selected_scene_step(3))
        for btn in (
            self._btn_scene_save,
            self._btn_scene_image,
            self._btn_scene_clip,
        ):
            edit_btn_row.addWidget(btn)
        edit_lay.addLayout(edit_btn_row)
        edit_lay.addStretch(1)
        right_tabs.addTab(edit_tab, "씬별 편집")

        preview = QWidget()
        preview_lay = QVBoxLayout(preview)
        self._image_preview = QLabel("생성된 이미지를 선택하면 여기에 표시됩니다.")
        self._image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_preview.setMinimumSize(320, 220)
        self._image_preview.setStyleSheet("border: 1px solid #c8c8c8; background: #fafafa;")
        self._clip_player = QMediaPlayer(self)
        self._clip_audio_output = QAudioOutput(self)
        self._clip_audio_output.setVolume(0.8)
        self._clip_player.setAudioOutput(self._clip_audio_output)
        self._clip_video = QVideoWidget()
        self._clip_video.setMinimumSize(320, 180)
        self._clip_video.setStyleSheet("background: #111;")
        self._clip_player.setVideoOutput(self._clip_video)
        self._clip_player.playbackStateChanged.connect(self._on_clip_playback_state_changed)
        self._btn_clip_play = QPushButton("재생")
        self._btn_clip_play.setEnabled(False)
        self._btn_clip_play.clicked.connect(self._toggle_clip_playback)
        self._label_clip_path = QLabel("생성된 클립을 선택하면 바로 확인할 수 있습니다.")
        self._label_clip_path.setWordWrap(True)
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setMaximumHeight(150)
        media_splitter = QSplitter(Qt.Orientation.Horizontal)

        image_pane = QWidget()
        image_lay = QVBoxLayout(image_pane)
        image_lay.setContentsMargins(0, 0, 0, 0)
        image_lay.addWidget(self._image_preview)

        video_pane = QWidget()
        video_lay = QVBoxLayout(video_pane)
        video_lay.setContentsMargins(0, 0, 0, 0)
        video_lay.addWidget(self._clip_video, stretch=1)
        clip_row = QHBoxLayout()
        clip_row.addWidget(self._btn_clip_play)
        clip_row.addWidget(self._label_clip_path, stretch=1)
        video_lay.addLayout(clip_row)

        media_splitter.addWidget(image_pane)
        media_splitter.addWidget(video_pane)
        media_splitter.setStretchFactor(0, 1)
        media_splitter.setStretchFactor(1, 1)
        preview_lay.addWidget(media_splitter, stretch=1)
        preview_lay.addWidget(self._detail)
        right_tabs.addTab(preview, "미리 보기")
        splitter.addWidget(right_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, stretch=1)

        self._progress = QProgressBar()
        root.addWidget(self._progress)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(110)
        root.addWidget(self._log)

    def set_project(self, project: StoryProject, project_parent: Path | None) -> None:
        if project.prompt_ko and not self._prompt.toPlainText().strip():
            self._prompt.setPlainText(project.prompt_ko)
        self._spin_minutes.setValue(max(1, int(project.target_minutes)))
        self._edit_resolution.setText(project.resolution or "1920x1080")
        if project.export_final_relpath:
            self._final_video_relpath = project.export_final_relpath
        self._scenes = list(project.scenes)
        self._load_state(project_parent)
        self._refresh_scene_table()
        self._refresh_result_table()
        self._refresh_buttons()
        self._refresh_voice_label()
        self._refresh_length_status()

    def apply_to_project(self, project: StoryProject) -> None:
        project.prompt_ko = self._prompt.toPlainText().strip()
        project.target_minutes = int(self._spin_minutes.value())
        project.resolution = self._edit_resolution.text().strip() or "1920x1080"
        project.fps = 24
        project.scenes = list(self._scenes)
        project.merged_srt_relpath = self._srt_relpath
        project.export_final_relpath = self._final_video_relpath

    def _project_parent(self) -> Path | None:
        parent = self._project_parent_getter()
        return parent.resolve() if parent is not None else None

    def _state_path(self, parent: Path) -> Path:
        return parent / "video_production_state.json"

    def _load_state(self, parent: Path | None) -> None:
        if parent is None:
            return
        path = self._state_path(parent)
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self._merged_video_relpath = str(data.get("merged_video_relpath", ""))
        self._style_prompt.setPlainText(str(data.get("visual_style_prompt", self._style_prompt.toPlainText()) or ""))
        raw_supplements = data.get("supplement_clip_relpaths", [])
        self._supplement_clip_relpaths = [str(x) for x in raw_supplements] if isinstance(raw_supplements, list) else []
        self._audio_relpath = str(data.get("audio_relpath", ""))
        self._srt_relpath = str(data.get("srt_relpath", ""))
        self._final_video_relpath = str(data.get("final_video_relpath", self._final_video_relpath))
        if "use_voice_in_final" in data:
            self._check_use_voice.setChecked(bool(data.get("use_voice_in_final")))
        if "use_subtitles_in_final" in data:
            self._check_use_subtitles.setChecked(bool(data.get("use_subtitles_in_final")))

    def _save_state(self) -> None:
        parent = self._project_parent()
        if parent is None:
            return
        parent.mkdir(parents=True, exist_ok=True)
        self._state_path(parent).write_text(
            json.dumps(
                {
                    "merged_video_relpath": self._merged_video_relpath,
                    "visual_style_prompt": self._style_prompt.toPlainText().strip(),
                    "supplement_clip_relpaths": self._supplement_clip_relpaths,
                    "audio_relpath": self._audio_relpath,
                    "srt_relpath": self._srt_relpath,
                    "final_video_relpath": self._final_video_relpath,
                    "use_voice_in_final": self._check_use_voice.isChecked(),
                    "use_subtitles_in_final": self._check_use_subtitles.isChecked(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _args(self) -> dict[str, Any]:
        parent = self._project_parent()
        if parent is None:
            raise RuntimeError("프로젝트를 먼저 저장하세요.")
        return {
            "project_parent": str(parent),
            "prompt": self._prompt.toPlainText().strip(),
            "visual_style_prompt": self._style_prompt.toPlainText().strip(),
            "target_minutes": self._spin_minutes.value(),
            "max_scenes": self._spin_scenes.value(),
            "clip_seconds": self._spin_clip.value(),
            "resolution": self._edit_resolution.text().strip() or "1920x1080",
            "gemini_api_key": str(self._settings.value("gemini/api_key", "") or os.environ.get("GEMINI_API_KEY", "")),
            "gemini_text_model": str(self._settings.value("gemini/model", "gemini-2.5-flash") or "gemini-2.5-flash"),
            "gemini_image_model": str(
                self._settings.value("gemini/image_model", DEFAULT_GEMINI_IMAGE_MODEL)
                or DEFAULT_GEMINI_IMAGE_MODEL
            ),
            "veo_model": str(self._settings.value("video/veo_model", "veo-3.1-generate-preview") or "veo-3.1-generate-preview"),
            "veo_resolution": str(self._settings.value("video/veo_resolution", "720p") or "720p"),
            "elevenlabs_api_key": str(
                self._settings.value("elevenlabs/api_key", "") or os.environ.get("ELEVENLABS_API_KEY", "")
            ),
            "elevenlabs_voice_id": str(
                self._settings.value("elevenlabs/voice_id", "") or os.environ.get("ELEVENLABS_VOICE_ID", "")
            ),
            "elevenlabs_model": str(self._settings.value("elevenlabs/model", "eleven_multilingual_v2") or "eleven_multilingual_v2"),
            "voice_provider": str(self._settings.value("voice/provider", VOICE_PROVIDER_ELEVENLABS) or VOICE_PROVIDER_ELEVENLABS),
            "gemini_tts_model": str(
                self._settings.value("gemini_tts/model", DEFAULT_GEMINI_TTS_MODEL) or DEFAULT_GEMINI_TTS_MODEL
            ),
            "gemini_tts_voice_name": str(
                self._settings.value("gemini_tts/voice_name", DEFAULT_GEMINI_TTS_VOICE) or DEFAULT_GEMINI_TTS_VOICE
            ),
            "gemini_tts_style_prompt": str(
                self._settings.value("gemini_tts/style_prompt", DEFAULT_GEMINI_TTS_STYLE) or DEFAULT_GEMINI_TTS_STYLE
            ),
            "max_subtitle_chars": int(self._settings.value("subtitle/max_line_chars", 24) or 24),
            "scenes": list(self._scenes),
            "merged_video_relpath": self._merged_video_relpath,
            "supplement_clip_relpaths": list(self._supplement_clip_relpaths),
            "audio_relpath": self._audio_relpath,
            "srt_relpath": self._srt_relpath,
            "use_voice_in_final": self._check_use_voice.isChecked(),
            "use_subtitles_in_final": self._check_use_subtitles.isChecked(),
        }

    def _voice_topic_text(self) -> str:
        scene_text = "\n".join(
            f"{scene.narration_ko}\n{scene.visual_prompt_ko}" for scene in self._scenes[:5]
        )
        return f"{self._prompt.toPlainText().strip()}\n{scene_text}".strip()

    def _find_matching_voice(self) -> None:
        provider = str(self._settings.value("voice/provider", VOICE_PROVIDER_ELEVENLABS) or VOICE_PROVIDER_ELEVENLABS)
        if provider != VOICE_PROVIDER_ELEVENLABS:
            QMessageBox.information(self, "목소리 검색", "자동 목소리 검색과 샘플 재생은 ElevenLabs 선택 시에만 사용할 수 있습니다.")
            return
        if self._voice_worker is not None and self._voice_worker.isRunning():
            QMessageBox.information(self, "목소리 검색", "이미 목소리를 검색 중입니다.")
            return
        topic = self._voice_topic_text()
        if not topic:
            QMessageBox.warning(self, "목소리 검색", "먼저 영상 주제나 대본을 입력하세요.")
            return
        api_key = str(self._settings.value("elevenlabs/api_key", "") or os.environ.get("ELEVENLABS_API_KEY", ""))
        if not api_key.strip():
            QMessageBox.warning(self, "목소리 검색", "환경 설정에 ElevenLabs API 키를 입력하세요.")
            return
        self._btn_find_voice.setEnabled(False)
        self._btn_find_voice.setText("검색 중...")
        self._voice_worker = VoiceSearchWorker(api_key=api_key, topic_text=topic)
        self._voice_worker.succeeded.connect(self._on_voice_search_succeeded)
        self._voice_worker.failed.connect(self._on_voice_search_failed)
        self._voice_worker.finished.connect(self._on_voice_search_finished)
        self._voice_worker.start()

    def _on_voice_search_succeeded(self, raw: object) -> None:
        candidates = list(raw) if isinstance(raw, list) else []
        if not candidates:
            QMessageBox.warning(self, "목소리 검색", "사용 가능한 ElevenLabs voice 후보를 찾지 못했습니다.")
            return
        dlg = VoiceCandidateDialog(self, candidates)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.selected_candidate()
        if selected is None:
            return
        self._settings.setValue("elevenlabs/voice_id", selected.voice_id)
        self._settings.sync()
        self._refresh_voice_label()
        self._append_log(f"ElevenLabs voice 선택: {selected.name} ({selected.voice_id})")

    def _on_voice_search_failed(self, msg: str) -> None:
        QMessageBox.warning(self, "목소리 검색 실패", msg)

    def _on_voice_search_finished(self) -> None:
        self._btn_find_voice.setText("어울리는 목소리 찾기")
        provider = str(self._settings.value("voice/provider", VOICE_PROVIDER_ELEVENLABS) or VOICE_PROVIDER_ELEVENLABS)
        self._btn_find_voice.setEnabled(provider == VOICE_PROVIDER_ELEVENLABS)
        if self._voice_worker is not None:
            self._voice_worker.deleteLater()
            self._voice_worker = None

    def _refresh_voice_label(self) -> None:
        provider = str(self._settings.value("voice/provider", VOICE_PROVIDER_ELEVENLABS) or VOICE_PROVIDER_ELEVENLABS)
        if provider == VOICE_PROVIDER_GEMINI_TTS:
            voice = str(
                self._settings.value("gemini_tts/voice_name", DEFAULT_GEMINI_TTS_VOICE)
                or DEFAULT_GEMINI_TTS_VOICE
            ).strip()
            model = str(
                self._settings.value("gemini_tts/model", DEFAULT_GEMINI_TTS_MODEL)
                or DEFAULT_GEMINI_TTS_MODEL
            ).strip()
            self._label_voice.setText(
                f"\uD604\uC7AC \uC74C\uC131 \uC81C\uACF5\uC790: Gemini TTS / voice: {voice or DEFAULT_GEMINI_TTS_VOICE} / model: {model or DEFAULT_GEMINI_TTS_MODEL}"
            )
            self._btn_find_voice.setEnabled(False)
            self._btn_find_voice.setToolTip("\uC790\uB3D9 \uBAA9\uC18C\uB9AC \uAC80\uC0C9\uACFC \uC0D8\uD50C \uC7AC\uC0DD\uC740 ElevenLabs \uC120\uD0DD \uC2DC\uC5D0\uB9CC \uC0AC\uC6A9\uD560 \uC218 \uC788\uC2B5\uB2C8\uB2E4.")
            return
        vid = str(self._settings.value("elevenlabs/voice_id", "") or "").strip()
        fallback_voice_id = "\uBBF8\uC124\uC815"
        self._label_voice.setText(
            f"\uD604\uC7AC \uC74C\uC131 \uC81C\uACF5\uC790: ElevenLabs / voice ID: {vid or f'({fallback_voice_id})'}"
        )
        self._btn_find_voice.setEnabled(True)
        self._btn_find_voice.setToolTip("")

    def _sync_auto_scene_count(self) -> None:
        auto_count = _auto_scene_count(self._spin_minutes.value(), self._spin_clip.value())
        if self._spin_scenes.value() != auto_count:
            self._spin_scenes.blockSignals(True)
            self._spin_scenes.setValue(auto_count)
            self._spin_scenes.blockSignals(False)
        self.stateChanged.emit()

    def _selected_scene_row_info(self) -> dict[str, object] | None:
        row = self._scene_table.currentRow()
        if row < 0 or row >= len(self._scene_table_rows):
            return None
        row_info = self._scene_table_rows[row]
        if str(row_info.get("type", "")) != "scene":
            return None
        return row_info

    def _selected_scene(self) -> Scene | None:
        row_info = self._selected_scene_row_info()
        if row_info is None:
            return None
        scene = row_info.get("scene")
        return scene if isinstance(scene, Scene) else None

    def _on_scene_selection_changed(self) -> None:
        self._update_preview_from_selection()
        self._sync_scene_editor_from_selection()

    def _sync_scene_editor_from_selection(self) -> None:
        scene = self._selected_scene()
        self._syncing_scene_editor = True
        try:
            if scene is None:
                self._edit_scene_title.setText("편집할 씬을 선택하세요.")
                self._edit_scene_narration.clear()
                self._edit_scene_image_prompt.clear()
                self._edit_scene_video_prompt.clear()
                self._edit_scene_clip_seconds.setValue(self._spin_clip.value())
                self._label_scene_assets.setText("")
            else:
                video_rel = scene.notes[len("video_relpath:") :] if scene.notes.startswith("video_relpath:") else ""
                self._edit_scene_title.setText(f"씬 {scene.scene_id}")
                self._edit_scene_narration.setPlainText(scene.narration_ko)
                self._edit_scene_image_prompt.setPlainText(scene.visual_prompt_ko)
                self._edit_scene_video_prompt.setPlainText(scene.video_prompt_ko)
                self._edit_scene_clip_seconds.setValue(_normalize_clip_seconds(scene.clip_seconds, self._spin_clip.value()))
                self._label_scene_assets.setText(
                    f"이미지: {scene.image_relpath or '(없음)'}\n영상: {video_rel or '(없음)'}"
                )
        finally:
            self._syncing_scene_editor = False
        editable = scene is not None
        for editor in (self._edit_scene_narration, self._edit_scene_image_prompt, self._edit_scene_video_prompt):
            editor.setEnabled(editable)
        self._edit_scene_clip_seconds.setEnabled(editable)
        self._btn_scene_save.setEnabled(editable)
        self._btn_scene_add.setEnabled(True)
        self._btn_scene_delete.setEnabled(editable and len(self._scenes) > 1)
        self._btn_scene_image.setEnabled(editable)
        self._btn_scene_clip.setEnabled(editable and bool(scene and scene.image_relpath.strip()))

    def _on_scene_editor_changed(self) -> None:
        if self._syncing_scene_editor:
            return
        if self._selected_scene() is not None:
            self._btn_scene_save.setEnabled(True)
            self.stateChanged.emit()

    def _save_selected_scene_edits(self) -> bool:
        scene = self._selected_scene()
        if scene is None:
            QMessageBox.information(self, "씬 편집", "편집할 씬을 선택하세요.")
            return False
        scene.narration_ko = self._edit_scene_narration.toPlainText().strip()
        scene.visual_prompt_ko = self._edit_scene_image_prompt.toPlainText().strip()
        scene.video_prompt_ko = self._edit_scene_video_prompt.toPlainText().strip()
        scene.clip_seconds = _normalize_clip_seconds(self._edit_scene_clip_seconds.value(), self._spin_clip.value())
        self._refresh_scene_table()
        self._refresh_buttons()
        self.stateChanged.emit()
        return True

    def _add_scene_after_selection(self) -> None:
        insert_at = len(self._scenes)
        current = self._selected_scene()
        if current is not None:
            for i, scene in enumerate(self._scenes):
                if scene is current:
                    insert_at = i + 1
                    break
        new_scene = Scene(
            scene_id=0,
            narration_ko="",
            visual_prompt_ko="",
            video_prompt_ko="무음 영상. 대사 없음, 내레이션 없음, 배경음악 없음, 효과음 없음.",
            clip_seconds=self._spin_clip.value(),
        )
        self._scenes.insert(insert_at, new_scene)
        self._renumber_scenes()
        self._merged_video_relpath = ""
        self._supplement_clip_relpaths = []
        self._audio_relpath = ""
        self._srt_relpath = ""
        self._final_video_relpath = ""
        self._refresh_scene_table()
        self._scene_table.selectRow(insert_at)
        self._refresh_result_table()
        self._refresh_buttons()
        self._refresh_length_status()
        self.stateChanged.emit()

    def _delete_selected_scene(self) -> None:
        scene = self._selected_scene()
        if scene is None:
            QMessageBox.information(self, "씬 삭제", "삭제할 씬을 선택하세요.")
            return
        if len(self._scenes) <= 1:
            QMessageBox.information(self, "씬 삭제", "최소 1개 씬은 필요합니다.")
            return
        if QMessageBox.question(self, "씬 삭제", f"씬 {scene.scene_id}을 삭제할까요?") != QMessageBox.StandardButton.Yes:
            return
        delete_at = self._scenes.index(scene)
        self._scenes.pop(delete_at)
        self._renumber_scenes()
        self._merged_video_relpath = ""
        self._supplement_clip_relpaths = []
        self._audio_relpath = ""
        self._srt_relpath = ""
        self._final_video_relpath = ""
        self._refresh_scene_table()
        if self._scene_table.rowCount():
            self._scene_table.selectRow(min(delete_at, self._scene_table.rowCount() - 1))
        self._refresh_result_table()
        self._refresh_buttons()
        self._refresh_length_status()
        self.stateChanged.emit()

    def _renumber_scenes(self) -> None:
        for index, scene in enumerate(self._scenes, start=1):
            scene.scene_id = index

    def _run_selected_scene_step(self, step: int) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "작업 중", "이미 실행 중인 작업이 있습니다.")
            return
        scene = self._selected_scene()
        if scene is None:
            QMessageBox.information(self, "씬 작업", "작업할 씬을 선택하세요.")
            return
        if not self._save_selected_scene_edits():
            return
        if step == 3 and not scene.image_relpath.strip():
            QMessageBox.warning(self, "씬 클립 영상 생성", "먼저 이 씬의 이미지를 생성하세요.")
            return
        try:
            args = self._args()
        except RuntimeError as e:
            QMessageBox.warning(self, "영상 제작", str(e))
            return
        args["scenes"] = [scene]
        self._start_worker(step, args)

    def _run_step(self, step: int) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "작업 중", "이미 실행 중인 작업이 있습니다.")
            return
        if step == 1 and not self._prompt.toPlainText().strip():
            QMessageBox.warning(self, "대본 생성", "프롬프트를 입력하세요.")
            return
        if step in (2, 3, 4, 5, 6, 7) and not self._scenes:
            QMessageBox.warning(self, "영상 제작", "먼저 1단계 대본 생성을 완료하세요.")
            return
        if step == 3 and not all(s.image_relpath.strip() for s in self._scenes):
            QMessageBox.warning(self, "이미지 생성", "먼저 2단계 이미지 생성을 완료하세요.")
            return
        if step == 4 and not all(s.notes.startswith("video_relpath:") for s in self._scenes):
            QMessageBox.warning(self, "영상 병합", "먼저 3단계 영상 클립 생성을 완료하세요.")
            return
        if step == 6:
            if not self._merged_video_relpath:
                QMessageBox.warning(self, "부족 영상 채우기", "먼저 4단계 영상 이어 붙이기를 완료하세요.")
                return
            if not self._audio_relpath:
                QMessageBox.warning(self, "부족 영상 채우기", "먼저 5단계 음성 생성을 완료하세요.")
                return
        if step == 8:
            if not self._merged_video_relpath:
                QMessageBox.warning(self, "최종 영상", "먼저 4단계 영상 이어 붙이기를 완료하세요.")
                return
            if self._check_use_voice.isChecked() and not self._audio_relpath:
                QMessageBox.warning(self, "최종 영상", "음성을 입히려면 5단계 음성 생성을 완료하거나 음성 입히기를 해제하세요.")
                return
            if self._check_use_subtitles.isChecked() and not self._srt_relpath:
                QMessageBox.warning(self, "최종 영상", "자막을 입히려면 7단계 자막 생성을 완료하거나 자막 입히기를 해제하세요.")
                return
        try:
            args = self._args()
        except RuntimeError as e:
            QMessageBox.warning(self, "영상 제작", str(e))
            return
        self._start_worker(step, args)

    def _start_worker(self, step: int, args: dict[str, Any]) -> None:
        self._progress.setValue(0)
        self._worker = VideoProductionWorker(step, args)
        self._worker.log_line.connect(self._append_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._set_busy(True)
        self._worker.start()

    def _on_progress(self, cur: int, total: int) -> None:
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(max(0, min(cur, max(1, total))))

    def _on_succeeded(self, step: int, result: object) -> None:
        if step == 1:
            if isinstance(result, dict):
                self._scenes = list(result.get("scenes", []))
                generated_style = str(result.get("visual_style_prompt", "") or "").strip()
                if generated_style:
                    self._style_prompt.setPlainText(generated_style)
                    self._append_log(f"공통 이미지 스타일 생성: {generated_style}")
                timeline_warning = str(result.get("timeline_warning", "") or "").strip()
                if timeline_warning:
                    self._append_log(f"대본 타임라인 경고: {timeline_warning}")
                    QMessageBox.warning(
                        self,
                        "대본 타임라인 경고",
                        "생성된 대본이 목표 길이와 다를 수 있습니다.\n\n"
                        f"{timeline_warning}\n\n"
                        "씬별 편집에서 대본이나 클립 초를 조정할 수 있습니다.",
                    )
            else:
                self._scenes = list(result) if isinstance(result, list) else []
            self._merged_video_relpath = ""
            self._supplement_clip_relpaths = []
            self._audio_relpath = ""
            self._srt_relpath = ""
            self._final_video_relpath = ""
            self._append_log(f"대본 생성 완료: {len(self._scenes)}개 씬")
        elif step == 2:
            for sid, rel in result:  # type: ignore[union-attr]
                for scene in self._scenes:
                    if scene.scene_id == int(sid):
                        scene.image_relpath = str(rel)
                        scene.notes = ""
            self._merged_video_relpath = ""
            self._supplement_clip_relpaths = []
            self._final_video_relpath = ""
            self._append_log("이미지 생성 완료")
        elif step == 3:
            self._supplement_clip_relpaths = []
            self._merged_video_relpath = ""
            self._final_video_relpath = ""
            for sid, rel in result:  # type: ignore[union-attr]
                for scene in self._scenes:
                    if scene.scene_id == int(sid):
                        scene.notes = f"video_relpath:{rel}"
            self._append_log("영상 클립 생성 완료")
        elif step == 4:
            self._merged_video_relpath = str(result)
            self._append_log(f"이어 붙인 영상: {self._merged_video_relpath}")
        elif step == 5:
            self._audio_relpath = str(result)
            self._append_log(f"음성: {self._audio_relpath}")
        elif step == 6:
            data = result if isinstance(result, dict) else {}
            self._supplement_clip_relpaths = [str(x) for x in data.get("supplement_clips", [])]
            self._merged_video_relpath = str(data.get("merged", self._merged_video_relpath))
            deficit = data.get("deficit", 0.0)
            if self._supplement_clip_relpaths:
                self._append_log("보충 클립: " + ", ".join(self._supplement_clip_relpaths))
            self._append_log(
                f"부족 영상 채우기 완료: 보충 클립 {len(self._supplement_clip_relpaths)}개, 부족 길이 {float(deficit):.2f}s"
            )
        elif step == 7:
            self._srt_relpath = str(result)
            self._append_log(f"자막: {self._srt_relpath}")
        elif step == 8:
            self._final_video_relpath = str(result)
            self._append_log(f"최종 영상: {self._final_video_relpath}")
        self._save_state()
        self._refresh_scene_table()
        self._refresh_result_table()
        self._refresh_buttons()
        self._refresh_length_status()
        if step in (4, 6):
            self._select_result_row("merged")
        elif step == 8:
            self._select_result_row("final")
        self.stateChanged.emit()

    def _on_failed(self, step: int, msg: str) -> None:
        self._append_log(f"[실패] {step}단계: {msg}")
        QMessageBox.warning(self, "영상 제작 실패", f"{step}단계 실패\n\n{msg}")

    def _on_finished(self) -> None:
        self._set_busy(False)
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _set_busy(self, busy: bool) -> None:
        for btn in self._buttons:
            btn.setEnabled(not busy)
        for btn in (
            self._btn_scene_save,
            self._btn_scene_add,
            self._btn_scene_delete,
            self._btn_scene_image,
            self._btn_scene_clip,
        ):
            btn.setEnabled(not busy)
        if not busy:
            self._refresh_buttons()
            self._sync_scene_editor_from_selection()

    def _refresh_buttons(self) -> None:
        has_script = bool(self._scenes)
        has_images = has_script and all(s.image_relpath.strip() for s in self._scenes)
        has_clips = has_script and all(s.notes.startswith("video_relpath:") for s in self._scenes)
        has_merged = bool(self._merged_video_relpath)
        has_audio = bool(self._audio_relpath)
        needs_voice = self._check_use_voice.isChecked()
        needs_subtitles = self._check_use_subtitles.isChecked()
        has_required_voice = (not needs_voice) or has_audio
        has_required_subtitles = (not needs_subtitles) or bool(self._srt_relpath)
        can_final = has_merged and has_required_voice and has_required_subtitles
        enabled = [True, has_script, has_images, has_clips, has_script, has_merged and has_audio, has_script, can_final]
        for btn, on in zip(self._buttons, enabled):
            btn.setEnabled(on)

    def _supplement_base_scene(self) -> Scene | None:
        return next((s for s in reversed(self._scenes) if s.image_relpath.strip()), None)

    def _supplement_prompt_for_table(self, scene: Scene, index: int) -> str:
        style = self._style_prompt.toPlainText().strip()
        style_prefix = f"공통 스타일: {style}\n\n" if style else ""
        video_prompt = scene.video_prompt_ko.strip() or scene.visual_prompt_ko.strip()
        return (
            f"{style_prefix}{video_prompt}\n\n"
            "Create a natural continuation shot from this final scene image. "
            "Keep the same characters, background, colors, and mood. "
            "Make it feel like a calm ending or reflective continuation, without introducing a new event. "
            "Use smooth cinematic motion and do not add readable on-screen text. "
            "Create a silent visual-only video. No dialogue, no narration, no voice, no background music, no sound effects. "
            f"This is supplemental continuation clip {index}."
        )

    def _refresh_length_status(self) -> None:
        parent = self._project_parent()
        if parent is None:
            self._label_length_status.setText("영상/음성 길이: 프로젝트를 저장하면 계산할 수 있습니다.")
            return
        merged = parent / self._merged_video_relpath if self._merged_video_relpath else None
        audio = parent / self._audio_relpath if self._audio_relpath else None
        video_sec = _media_duration(merged)
        audio_sec = _media_duration(audio)
        if video_sec <= 0 and audio_sec <= 0:
            if self._scenes:
                stats = _timeline_stats(self._scenes, target_minutes=self._spin_minutes.value())
                self._label_length_status.setText(
                    f"계획 길이: 목표 {stats['target']:.0f}s / 클립 합계 {stats['clip_total']:.0f}s / "
                    f"예상 음성 {stats['voice_estimate']:.1f}s"
                )
            else:
                self._label_length_status.setText("영상/음성 길이: 아직 계산되지 않았습니다.")
            return
        deficit = audio_sec - video_sec
        supplement_count = len(self._supplement_clip_relpaths)
        if deficit > 3.0:
            suffix = f"부족 {deficit:.2f}s"
        elif deficit > 0:
            suffix = f"짧은 부족 {deficit:.2f}s(최종 합성에서 보정)"
        else:
            suffix = "부족 없음"
        if supplement_count:
            supplement_tail = ", ".join(Path(p).name for p in self._supplement_clip_relpaths[-3:])
            if supplement_count > 3:
                supplement_tail = f"... {supplement_tail}"
            supplement_text = f" / 보충 클립 {supplement_count}개 포함: {supplement_tail}"
        else:
            supplement_text = " / 보충 클립 없음"
        self._label_length_status.setText(
            f"영상 {video_sec:.2f}s / 음성 {audio_sec:.2f}s / {suffix}{supplement_text}"
        )

    def _refresh_result_table(self) -> None:
        selected_kind = ""
        current_row = self._result_table.currentRow()
        if 0 <= current_row < len(self._result_table_rows):
            selected_kind = self._result_table_rows[current_row].get("kind", "")

        parent = self._project_parent()
        rows: list[dict[str, str]] = []
        if self._merged_video_relpath:
            rows.append({"kind": "merged", "label": "이어 붙인 영상", "relpath": self._merged_video_relpath})
        if self._final_video_relpath:
            rows.append({"kind": "final", "label": "최종 영상", "relpath": self._final_video_relpath})

        self._result_table_rows = rows
        self._result_table.setRowCount(0)
        for row_info in rows:
            row = self._result_table.rowCount()
            self._result_table.insertRow(row)
            relpath = row_info["relpath"]
            duration = _media_duration(parent / relpath) if parent is not None else 0.0
            values = [
                row_info["label"],
                relpath,
                f"{duration:.2f}s" if duration > 0 else "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                self._result_table.setItem(row, col, item)
        self._result_table.resizeColumnsToContents()

        if rows:
            row_to_select = 0
            if selected_kind:
                for i, row_info in enumerate(rows):
                    if row_info["kind"] == selected_kind:
                        row_to_select = i
                        break
            self._result_table.selectRow(row_to_select)

    def _select_result_row(self, kind: str) -> None:
        for row, row_info in enumerate(self._result_table_rows):
            if row_info.get("kind") == kind:
                self._result_table.selectRow(row)
                return

    def _update_result_preview_from_selection(self) -> None:
        row = self._result_table.currentRow()
        if row < 0 or row >= len(self._result_table_rows):
            return
        row_info = self._result_table_rows[row]
        parent = self._project_parent()
        relpath = row_info.get("relpath", "")
        if parent is None or not relpath:
            return
        video_path = parent / relpath
        if not video_path.is_file():
            self._clear_clip_preview()
            self._label_clip_path.setText(f"{row_info.get('label', '결과 영상')} 파일을 찾을 수 없습니다.")
            return
        self._load_preview_video(video_path, f"{row_info.get('label', '결과 영상')}: {relpath}")

    def _refresh_scene_table(self) -> None:
        selected_key: tuple[str, int] | None = None
        current_row = self._scene_table.currentRow()
        if 0 <= current_row < len(self._scene_table_rows):
            row_info = self._scene_table_rows[current_row]
            selected_key = (str(row_info.get("type", "")), int(row_info.get("index", -1)))
        self._scene_table_rows = []
        self._scene_table.setRowCount(0)
        for scene in self._scenes:
            video_rel = scene.notes[len("video_relpath:") :] if scene.notes.startswith("video_relpath:") else ""
            self._scene_table_rows.append(
                {
                    "type": "scene",
                    "index": scene.scene_id,
                    "scene": scene,
                    "image": scene.image_relpath,
                    "video": video_rel,
                    "prompt": scene.visual_prompt_ko,
                    "video_prompt": scene.video_prompt_ko,
                    "clip_seconds": str(_normalize_clip_seconds(scene.clip_seconds, self._spin_clip.value())),
                    "narration": scene.narration_ko,
                }
            )
            row = self._scene_table.rowCount()
            self._scene_table.insertRow(row)
            values = [
                str(scene.scene_id),
                str(_normalize_clip_seconds(scene.clip_seconds, self._spin_clip.value())),
                scene.narration_ko,
                scene.visual_prompt_ko,
                scene.video_prompt_ko,
                scene.image_relpath,
                video_rel,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                self._scene_table.setItem(row, col, item)
        base_scene = self._supplement_base_scene()
        if base_scene is not None:
            for i, video_rel in enumerate(self._supplement_clip_relpaths, start=1):
                prompt = self._supplement_prompt_for_table(base_scene, i)
                self._scene_table_rows.append(
                    {
                        "type": "supplement",
                        "index": i,
                        "scene": base_scene,
                        "image": base_scene.image_relpath,
                        "video": video_rel,
                        "prompt": base_scene.visual_prompt_ko,
                        "video_prompt": prompt,
                        "clip_seconds": str(self._spin_clip.value()),
                        "narration": "부족한 음성 길이를 채우기 위한 보충 클립입니다.",
                    }
                )
                row = self._scene_table.rowCount()
                self._scene_table.insertRow(row)
                values = [
                    f"보충 {i}",
                    str(self._spin_clip.value()),
                    "부족한 음성 길이를 채우기 위한 보충 클립입니다.",
                    base_scene.visual_prompt_ko,
                    prompt,
                    base_scene.image_relpath,
                    video_rel,
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if col == 0:
                        item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                    self._scene_table.setItem(row, col, item)
        self._scene_table.resizeColumnsToContents()
        if self._scene_table_rows:
            row_to_select = 0
            if selected_key is not None:
                for i, row_info in enumerate(self._scene_table_rows):
                    key = (str(row_info.get("type", "")), int(row_info.get("index", -1)))
                    if key == selected_key:
                        row_to_select = i
                        break
            self._scene_table.selectRow(row_to_select)
        self._on_scene_selection_changed()

    def _update_preview_from_selection(self) -> None:
        row = self._scene_table.currentRow()
        if row < 0 or row >= len(self._scene_table_rows):
            self._image_preview.setText("생성된 이미지를 선택하면 여기에 표시됩니다.")
            self._image_preview.setPixmap(QPixmap())
            self._clear_clip_preview()
            self._detail.clear()
            return
        row_info = self._scene_table_rows[row]
        row_type = str(row_info.get("type", "scene"))
        index = int(row_info.get("index", 0))
        narration = str(row_info.get("narration", ""))
        prompt = str(row_info.get("prompt", ""))
        video_prompt = str(row_info.get("video_prompt", ""))
        clip_seconds = str(row_info.get("clip_seconds", ""))
        image_rel = str(row_info.get("image", ""))
        video_rel = str(row_info.get("video", ""))
        title = f"씬 {index}" if row_type == "scene" else f"보충 클립 {index}"
        self._detail.setPlainText(
            f"{title}\n\n클립 초: {clip_seconds}\n\n대본\n{narration}\n\n이미지 프롬프트:\n{prompt}\n\n영상 프롬프트:\n{video_prompt}\n\n"
            f"이미지: {image_rel or '(없음)'}\n영상: {video_rel or '(없음)'}"
        )
        self._update_clip_preview_from_rel(video_rel)
        parent = self._project_parent()
        image_path = (parent / image_rel) if parent is not None and image_rel else None
        if image_path is None or not image_path.is_file():
            self._image_preview.setPixmap(QPixmap())
            self._image_preview.setText("이미지가 아직 생성되지 않았습니다.")
            return
        pix = QPixmap(str(image_path))
        if pix.isNull():
            self._image_preview.setPixmap(QPixmap())
            self._image_preview.setText("이미지를 표시할 수 없습니다.")
            return
        self._image_preview.setText("")
        self._image_preview.setPixmap(
            pix.scaled(
                self._image_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _clip_path_for_scene(self, scene: Scene) -> Path | None:
        if not scene.notes.startswith("video_relpath:"):
            return None
        parent = self._project_parent()
        if parent is None:
            return None
        rel = scene.notes[len("video_relpath:") :].strip()
        return parent / rel if rel else None

    def _update_clip_preview(self, scene: Scene) -> None:
        clip_path = self._clip_path_for_scene(scene)
        parent = self._project_parent()
        label = _relative(clip_path, parent) if clip_path is not None and parent is not None else ""
        self._update_clip_preview_path(clip_path, label)

    def _update_clip_preview_from_rel(self, video_rel: str) -> None:
        parent = self._project_parent()
        clip_path = (parent / video_rel) if parent is not None and video_rel else None
        self._update_clip_preview_path(clip_path, video_rel)

    def _update_clip_preview_path(self, clip_path: Path | None, label: str) -> None:
        if clip_path is None or not clip_path.is_file():
            self._clear_clip_preview()
            self._label_clip_path.setText("생성된 클립이 아직 없습니다.")
            return
        self._load_preview_video(clip_path, label or str(clip_path))

    def _load_preview_video(self, video_path: Path, label: str, *, autoplay: bool = False) -> None:
        if not video_path.is_file():
            return
        self._clip_player.stop()
        self._clip_player.setSource(QUrl.fromLocalFile(str(video_path.resolve())))
        self._btn_clip_play.setText("재생")
        self._btn_clip_play.setEnabled(True)
        self._label_clip_path.setText(label)
        if autoplay:
            self._clip_player.play()

    def _clear_clip_preview(self) -> None:
        self._clip_player.stop()
        self._clip_player.setSource(QUrl())
        self._btn_clip_play.setText("재생")
        self._btn_clip_play.setEnabled(False)
        self._label_clip_path.setText("생성된 클립을 선택하면 바로 확인할 수 있습니다.")

    def _toggle_clip_playback(self) -> None:
        if not self._btn_clip_play.isEnabled():
            return
        if self._clip_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._clip_player.pause()
            return
        self._clip_player.play()

    def _on_clip_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._btn_clip_play.setText("일시정지")
        else:
            self._btn_clip_play.setText("재생")

    def _append_log(self, text: str) -> None:
        self._log.appendPlainText(text)
        self._log.verticalScrollBar().setValue(self._log.verticalScrollBar().maximum())
