from __future__ import annotations

import csv
import json
import logging
import mimetypes
import os
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
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
from app.models.storyboard import SCRIPT_INPUT_MODE_FULL_SCRIPT, SCRIPT_INPUT_MODE_TOPIC
from app.services.elevenlabs_client import synthesize_speech_with_timestamps
from app.services.elevenlabs_voice_client import (
    ElevenLabsVoiceCandidate,
    recommend_voices,
)
from app.services.ffmpeg_render import concat_segments_normalized, run_ffmpeg, which_ffmpeg, parse_resolution
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
from app.services.gemini_tts_client import synthesize_gemini_speech, synthesize_gemini_speech_segments
from app.services.scene_generation import (
    build_narration_length_rules,
    build_user_message,
    narration_length_limits,
    strip_markdown_json_fence,
)
from app.services.srt_build import build_srt_from_timed_lines, parse_srt_file, seconds_to_srt_timestamp, split_narration_lines
from app.services.stt_transcribe import refine_timed_lines_with_reference_script, transcribe_wav_sentences
from app.services.comfyui_wan_video_client import generate_video_from_image_comfyui_wan
from app.services.kling_video_client import generate_video_from_image_kling_api
from app.services.veo_video_client import generate_video_from_image


VIDEO_BACKEND_VEO = "veo"
VIDEO_BACKEND_COMFYUI_WAN = "comfyui_wan"
VIDEO_BACKEND_KLING_API = "kling_api"
VOICE_PROVIDER_ELEVENLABS = "elevenlabs"
VOICE_PROVIDER_GEMINI_TTS = "gemini_tts"
DEFAULT_GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_GEMINI_TTS_VOICE = "Kore"
DEFAULT_VIDEO_CLIP_SECONDS = 8
DEFAULT_GEMINI_TTS_STYLE = (
    "한국어로 자연스럽고 또렷한 내레이션으로 읽어줘. "
    "모든 문단에서 같은 화자처럼 일정한 톤, 속도, 음색을 유지해줘."
)
GEMINI_TTS_TARGET_SEGMENTS = 3
DEFAULT_GEMINI_TTS_SPLIT_AUDIO = True
VOICE_TEMPO_MIN = 0.85
VOICE_TEMPO_MAX = 1.25
CLIP_SECONDS_MODE_LLM = "llm"
CLIP_SECONDS_MODE_ADJUSTED = "adjusted"
ASSET_POLICY_REPLACE = "replace"
ASSET_POLICY_MISSING_ONLY = "missing_only"
FINAL_AUDIO_NARRATION_ONLY = "narration_only"
FINAL_AUDIO_CLIP_ONLY = "clip_only"
FINAL_AUDIO_MIX = "mix"

logger = logging.getLogger(__name__)


def _suffix_for_mime(mime: str) -> str:
    return mimetypes.guess_extension(mime or "") or ".png"


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _which_ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("PATH에서 ffprobe를 찾을 수 없습니다.")
    return exe


def _media_has_audio(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        proc = subprocess.run(
            [
                _which_ffprobe(),
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path.resolve()),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and "audio" in (proc.stdout or "").lower()


def _no_window_kwargs() -> dict[str, int]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _parse_ffmpeg_progress_seconds(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    try:
        if text.startswith("out_time_ms="):
            return max(0.0, float(text.split("=", 1)[1]) / 1_000_000.0)
        if text.startswith("out_time_us="):
            return max(0.0, float(text.split("=", 1)[1]) / 1_000_000.0)
        if text.startswith("out_time="):
            stamp = text.split("=", 1)[1].strip()
            hours, minutes, seconds = stamp.split(":", 2)
            return max(0.0, int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    except (TypeError, ValueError):
        return None
    return None


def _run_ffmpeg_with_progress(
    cmd: list[str],
    *,
    cwd: Path,
    duration_sec: float,
    progress_callback: Callable[[int], None],
    timeout_sec: float = 7200.0,
) -> None:
    progress_cmd = [cmd[0], "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", *cmd[2:]]
    proc = subprocess.Popen(
        progress_cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_no_window_kwargs(),
    )
    output_lines: list[str] = []
    last_percent = -1
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            output_lines.append(line)
            seconds = _parse_ffmpeg_progress_seconds(line)
            if seconds is None or duration_sec <= 0:
                continue
            percent = max(0, min(99, int(seconds / duration_sec * 100)))
            if percent > last_percent:
                last_percent = percent
                progress_callback(percent)
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        raise RuntimeError("ffmpeg 실행 시간이 초과되었습니다.") from e
    if proc.returncode != 0:
        err = "".join(output_lines).strip()
        raise RuntimeError(err or f"ffmpeg 종료 코드 {proc.returncode}")


def _normalize_final_audio_mode(raw: object) -> str:
    value = str(raw or "").strip()
    if value in (FINAL_AUDIO_CLIP_ONLY, FINAL_AUDIO_MIX):
        return value
    return FINAL_AUDIO_NARRATION_ONLY


def _bool_value(raw: object, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _compose_final_video(
    *,
    ffmpeg: str,
    input_video: Path,
    narration_audio: Path | None,
    srt_path: Path | None,
    output_video: Path,
    cwd: Path,
    video_pad_sec: float = 0.0,
    audio_mode: str = FINAL_AUDIO_NARRATION_ONLY,
    clip_audio_volume: float = 0.2,
    narration_audio_volume: float = 1.0,
    duration_sec: float = 0.0,
    progress_callback: Callable[[int], None] | None = None,
) -> None:
    output_video.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-i", str(input_video.resolve())]
    if narration_audio is not None:
        cmd.extend(["-i", str(narration_audio.resolve())])
    cmd.extend(["-map", "0:v:0"])
    mode = _normalize_final_audio_mode(audio_mode)
    clip_has_audio = _media_has_audio(input_video)
    use_narration = narration_audio is not None and mode in (FINAL_AUDIO_NARRATION_ONLY, FINAL_AUDIO_MIX)
    use_clip_audio = clip_has_audio and mode in (FINAL_AUDIO_CLIP_ONLY, FINAL_AUDIO_MIX)
    audio_filter = ""
    if mode == FINAL_AUDIO_MIX and use_clip_audio and use_narration:
        clip_vol = max(0.0, min(4.0, float(clip_audio_volume)))
        narration_vol = max(0.0, min(4.0, float(narration_audio_volume)))
        audio_filter = (
            f"[0:a:0]volume={clip_vol:.3f},aresample=48000[clipa];"
            f"[1:a:0]volume={narration_vol:.3f},aresample=48000[narra];"
            "[clipa][narra]amix=inputs=2:duration=longest:dropout_transition=0,"
            "alimiter=limit=0.95[aout]"
        )
        cmd.extend(["-filter_complex", audio_filter, "-map", "[aout]"])
    elif use_narration:
        cmd.extend(["-map", "1:a:0"])
    elif use_clip_audio:
        cmd.extend(["-map", "0:a:0"])
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
    if audio_filter or use_narration or use_clip_audio:
        cmd.extend(["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k"])
    else:
        cmd.extend(["-an"])
    cmd.append(str(output_video.resolve()))
    if progress_callback is not None:
        _run_ffmpeg_with_progress(
            cmd,
            cwd=cwd,
            duration_sec=duration_sec,
            progress_callback=progress_callback,
        )
    else:
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


def _split_tts_text_chunk(text: str, *, max_chars: int) -> list[str]:
    clean = " ".join((text or "").split()).strip()
    if not clean:
        return []
    max_chars = max(80, int(max_chars))
    if len("".join(clean.split())) <= max_chars:
        return [clean]

    sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？…])\s+", clean) if part.strip()]
    if len(sentences) <= 1:
        sentences = split_narration_lines(clean, max_chars)

    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for sentence in sentences:
        sentence_chars = len("".join(sentence.split()))
        if sentence_chars > max_chars:
            if current:
                chunks.append(" ".join(current))
                current = []
                current_chars = 0
            chunks.extend(split_narration_lines(sentence, max_chars))
            continue
        if current and current_chars + sentence_chars > max_chars:
            chunks.append(" ".join(current))
            current = []
            current_chars = 0
        current.append(sentence)
        current_chars += sentence_chars
    if current:
        chunks.append(" ".join(current))
    return chunks or [clean]


def _srt_from_cues(cues: list[tuple[float, float, str]]) -> str:
    blocks: list[str] = []
    for idx, (st, en, text) in enumerate(cues, start=1):
        blocks.extend(
            [
                str(idx),
                f"{seconds_to_srt_timestamp(st)} --> {seconds_to_srt_timestamp(max(st + 0.04, en))}",
                text,
                "",
            ]
        )
    return "\n".join(blocks).rstrip() + "\n" if blocks else ""


def _short_clock(seconds: float) -> str:
    value = max(0, int(float(seconds)))
    return f"{value // 60:02d}:{value % 60:02d}"


def _atempo_filter(tempo: float) -> str:
    value = max(0.5, min(2.0, float(tempo)))
    parts: list[str] = []
    while value > 2.0:
        parts.append("atempo=2.0")
        value /= 2.0
    while value < 0.5:
        parts.append("atempo=0.5")
        value /= 0.5
    parts.append(f"atempo={value:.6f}")
    return ",".join(parts)


def _media_duration(path: Path | None) -> float:
    if path is None or not path.is_file():
        return 0.0
    try:
        return max(0.0, float(ffprobe_duration_seconds(path.resolve())))
    except FfprobeError:
        return 0.0


def _require_valid_video(path: Path, label: str) -> float:
    duration = _media_duration(path)
    if duration <= 0:
        raise RuntimeError(f"{label} 파일이 손상되었거나 아직 완전히 생성되지 않았습니다: {path}")
    return duration


def _scene_image_candidates(parent: Path, scene_id: int) -> list[Path]:
    base = parent / "images" / "video_production"
    return [base / f"scene_{scene_id:03d}{suffix}" for suffix in (".png", ".jpg", ".jpeg", ".webp")]


def _existing_scene_image_relpath(parent: Path, scene: Scene) -> str:
    rel = scene.image_relpath.strip()
    if rel and (parent / rel).is_file():
        return rel
    for path in _scene_image_candidates(parent, scene.scene_id):
        if path.is_file():
            return _relative(path, parent)
    return ""


def _scene_video_path(parent: Path, scene_id: int) -> Path:
    return parent / "video_clips" / f"scene_{scene_id:03d}.mp4"


def _existing_scene_video_relpath(parent: Path, scene: Scene) -> str:
    prefix = "video_relpath:"
    if scene.notes.startswith(prefix):
        rel = scene.notes[len(prefix) :].strip()
        if rel and (parent / rel).is_file():
            return rel
    path = _scene_video_path(parent, scene.scene_id)
    return _relative(path, parent) if path.is_file() else ""


def _kling_model_name(model: object) -> str:
    value = str(model or "kling-v2-5-turbo").strip()
    return {
        "kling-v2.5-turbo": "kling-v2-5-turbo",
        "kling-v2.6": "kling-v2-6",
        "kling-v2.6-std": "kling-v2-6",
        "kling-v2.6-pro": "kling-v2-6",
        "kling-v3.0": "kling-v3",
        "kling-3.0": "kling-v3",
        "kling-v3-0": "kling-v3",
    }.get(value, value)


def _clip_duration_values(*, video_backend: object, kling_model: object = "") -> tuple[int, ...]:
    if str(video_backend or "") != VIDEO_BACKEND_KLING_API:
        return (4, 6, 8)
    return (5, 10)


def _default_clip_seconds_for_values(values: tuple[int, ...]) -> int:
    return 10 if 10 in values else values[-1]


def _normalize_clip_seconds_to_values(raw: object, values: tuple[int, ...]) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = _default_clip_seconds_for_values(values)
    return min(values, key=lambda item: (abs(item - value), item))


def _target_seconds(target_minutes: int) -> int:
    return max(1, int(target_minutes)) * 60


def _estimate_narration_seconds(narration: str) -> float:
    count = _narration_char_count(narration)
    if count <= 0:
        return 0.0
    sentence_count = max(1, len([s for s in re.split(r"[.!?。！？\n]+", narration) if s.strip()]))
    pause_seconds = max(0, sentence_count - 1) * 0.5
    return max(1.2, count / 3.5 + pause_seconds)


def _clip_seconds_for_narration(narration: str, default_clip_seconds: int = DEFAULT_VIDEO_CLIP_SECONDS) -> int:
    estimate = _estimate_narration_seconds(narration)
    if estimate <= 0:
        return _normalize_clip_seconds(default_clip_seconds, DEFAULT_VIDEO_CLIP_SECONDS)
    if estimate <= 4:
        return 4
    if estimate <= 6:
        return 6
    return 8


def _normalize_clip_seconds_mode(raw: object) -> str:
    return CLIP_SECONDS_MODE_ADJUSTED if str(raw or "").strip() == CLIP_SECONDS_MODE_ADJUSTED else CLIP_SECONDS_MODE_LLM


def _normalize_script_input_mode(raw: object) -> str:
    value = str(raw or "").strip()
    return SCRIPT_INPUT_MODE_FULL_SCRIPT if value == SCRIPT_INPUT_MODE_FULL_SCRIPT else SCRIPT_INPUT_MODE_TOPIC


def _scene_clip_seconds(scene: Scene, mode: object, default_clip_seconds: int = DEFAULT_VIDEO_CLIP_SECONDS) -> int:
    if _normalize_clip_seconds_mode(mode) == CLIP_SECONDS_MODE_LLM and scene.llm_clip_seconds:
        return _normalize_clip_seconds(scene.llm_clip_seconds, default_clip_seconds)
    return _clip_seconds_for_narration(scene.narration_ko, default_clip_seconds)


def _scene_clip_seconds_for_values(scene: Scene, mode: object, values: tuple[int, ...]) -> int:
    if _normalize_clip_seconds_mode(mode) == CLIP_SECONDS_MODE_LLM and scene.llm_clip_seconds:
        return _normalize_clip_seconds_to_values(scene.llm_clip_seconds, values)
    estimate = _estimate_narration_seconds(scene.narration_ko)
    if estimate <= 0:
        return _default_clip_seconds_for_values(values)
    for value in values:
        if estimate <= value:
            return value
    return values[-1]


def _narration_char_count(text: str) -> int:
    return len("".join(ch for ch in text.strip() if not ch.isspace()))


def _timeline_stats(
    scenes: list[Scene],
    *,
    target_minutes: int,
    clip_seconds_mode: str = CLIP_SECONDS_MODE_LLM,
    clip_duration_values: tuple[int, ...] = (4, 6, 8),
) -> dict[str, float]:
    target = float(_target_seconds(target_minutes))
    clip_total = float(sum(_scene_clip_seconds_for_values(s, clip_seconds_mode, clip_duration_values) for s in scenes))
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


def _narration_length_issues(scenes: list[Scene], *, values: tuple[int, ...]) -> list[str]:
    issues: list[str] = []
    for scene in scenes:
        clip_seconds = _normalize_clip_seconds_to_values(scene.clip_seconds, values)
        count = _narration_char_count(scene.narration_ko)
        recommended_min, recommended_max, hard_max = narration_length_limits(clip_seconds)
        if count < recommended_min:
            issues.append(
                f"scene {scene.scene_id}: {clip_seconds}초, {count}자 "
                f"(권장 {recommended_min}~{recommended_max}자, 너무 짧음)"
            )
        elif count > hard_max:
            issues.append(
                f"scene {scene.scene_id}: {clip_seconds}초, {count}자 "
                f"(권장 {recommended_min}~{recommended_max}자, 최대 {hard_max}자)"
            )
    return issues


def _format_prompt_log(title: str, text: str) -> str:
    return f"\n========== {title} ==========\n{text.rstrip()}\n========== /{title} =========="


def _log_prompt(title: str, text: str) -> None:
    logger.info("%s", _format_prompt_log(title, text))


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
    asset_ready = Signal(int, int, str)
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
                result = self._subtitles()
            elif self._step == 7:
                result = self._final()
            elif self._step == 9:
                result = self._reference_image()
            else:
                raise RuntimeError(f"Unknown step: {self._step}")
            self.succeeded.emit(self._step, result)
        except Exception as e:
            self.failed.emit(self._step, str(e))

    def _reference_image(self) -> str:
        parent = Path(self._args["project_parent"]).resolve()
        style = str(self._args.get("visual_style_prompt", "")).strip()
        prompt = str(self._args.get("reference_image_prompt", "")).strip()
        if not prompt:
            raise RuntimeError("참조 이미지 프롬프트가 비어 있습니다.")
        style_block = f"공통 시각 스타일:\n{style}\n\n" if style else ""
        full_prompt = (
            "영상 제작 프로젝트용 깨끗한 참조 이미지 1장을 생성하세요.\n"
            "이 참조 이미지는 이후 씬 이미지에서 반복 유지할 인물 정체성, 의상, 소품, 배경, "
            "색감, 조명, 전체 시각 톤을 명확히 보여줘야 합니다.\n"
            "공통 시각 스타일이 제공되면 반드시 그 스타일을 따르세요.\n"
            "이미지 안에는 읽을 수 있는 글자, 자막, 로고, 워터마크를 넣지 마세요.\n\n"
            f"{style_block}"
            f"참조 이미지 설명:\n{prompt}\n"
        )
        raw, mime = gemini_generate_image(
            str(self._args["gemini_api_key"]),
            str(self._args["gemini_image_model"]),
            prompt=full_prompt,
            aspect_ratio=api_aspect_ratio_from_resolution(str(self._args["resolution"])),
        )
        path = parent / "images" / "video_production" / f"reference{_suffix_for_mime(mime)}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return _relative(path, parent)

    def _script(self) -> dict[str, object]:
        target_seconds = _target_seconds(int(self._args["target_minutes"]))
        script_input_mode = _normalize_script_input_mode(self._args.get("script_input_mode", SCRIPT_INPUT_MODE_TOPIC))
        voice_min_chars = int(target_seconds * 2.8)
        voice_max_chars = int(target_seconds * 3.5)
        clip_values = tuple(int(x) for x in self._args.get("clip_duration_values", (4, 6, 8)))
        clip_values_text = "/".join(str(x) for x in clip_values)
        narration_length_rules = build_narration_length_rules(clip_values)
        min_clip_seconds = min(clip_values)
        max_clip_seconds = max(clip_values)
        min_scene_count = max(1, math.ceil(max(1, target_seconds - 4) / max_clip_seconds))
        max_scene_count = max(min_scene_count, math.ceil((target_seconds + 4) / min_clip_seconds))
        prompt_text = str(self._args["prompt"]).strip()
        if script_input_mode == SCRIPT_INPUT_MODE_FULL_SCRIPT:
            msg = (
                "다음 조건으로 사용자가 제공한 전체 대본을 씬 목록으로 나누어 주세요.\n\n"
                f"[영상 설정]\n"
                f"- 목표 길이: 약 {int(self._args['target_minutes'])}분\n"
                f"- 해상도: {self._args['resolution']}\n"
                f"- FPS: 24\n\n"
                "[전체 대본]\n"
                f"{prompt_text}\n"
            )
        else:
            msg = build_user_message(
                prompt_ko=prompt_text,
                target_minutes=int(self._args["target_minutes"]),
                resolution=str(self._args["resolution"]),
                fps=24,
            )
        msg += (
            f"\n[타임라인 배분]\n"
            f"- 목표 전체 길이: 약 {target_seconds}초\n"
            f"- 전체 클립 길이 합계 허용 범위: {target_seconds - 4}~{target_seconds + 4}초\n"
            f"- 예상 음성 길이 목표 범위: 약 {max(1, target_seconds - 8)}~{target_seconds + 2}초\n"
            f"- 전체 narration_ko 총 글자 수(공백 제외) 목표 범위: 약 {voice_min_chars}~{voice_max_chars}자\n"
            f"- 권장 씬 수 범위: 약 {min_scene_count}~{max_scene_count}개\n"
            f"- 각 씬의 clip_seconds는 반드시 {clip_values_text}초 중 하나\n"
            f"- clip_seconds별 narration_ko 길이 기준(공백 제외): {narration_length_rules}\n"
            "- 5초처럼 짧은 씬은 짧은 질문, 전환, 인사, 마무리에 사용하고 긴 설명을 넣지 마세요.\n"
            "- 10초처럼 긴 씬은 설명 문장을 충분히 담아 너무 비어 보이지 않게 작성하세요.\n"
            "- 전체 대본이 목표 길이에 충분히 차도록 씬 수, clip_seconds, 씬별 대본 길이를 함께 조절하세요.\n"
        )
        if script_input_mode == SCRIPT_INPUT_MODE_FULL_SCRIPT:
            msg += (
                "\n[전체 대본 모드 추가 규칙]\n"
                "- 사용자가 제공한 전체 대본의 의미, 정보 순서, 결론을 유지하세요.\n"
                "- 대본 내용을 요약해서 짧게 만들거나 새로운 설명을 추가하지 마세요.\n"
                "- 긴 문장은 자연스러운 절 단위로 나누어 여러 씬에 배정하세요.\n"
                "- 각 narration_ko를 이어 붙였을 때 전체 대본과 의미상 같은 흐름이 되게 하세요.\n"
                "- visual_prompt_ko와 video_prompt_ko는 각 narration_ko 내용에 맞춰 새로 작성하세요.\n"
            )
        first_model = str(self._args["gemini_text_model"])
        self.progress.emit(0, 100)
        self.log_line.emit("대본 생성: 1/?: 대본/씬 분할 생성")
        scene_system_instruction = self._scene_split_system_instruction(clip_values)
        _log_prompt("LLM scene split system instruction", scene_system_instruction)
        _log_prompt("LLM scene split user instruction", msg)
        scene_content, chosen_model = self._generate_script_text(
            system_instruction=scene_system_instruction,
            user_text=msg,
            label="대본/씬 분할",
        )
        scenes = self._parse_scene_split_json(scene_content, clip_values)
        scene_prompt_batches = max(1, math.ceil(len(scenes) / 5))
        total_progress_units = 2 + scene_prompt_batches + (0 if script_input_mode == SCRIPT_INPUT_MODE_FULL_SCRIPT else 1)
        progress_done = 1
        self.progress.emit(progress_done, total_progress_units)

        if script_input_mode != SCRIPT_INPUT_MODE_FULL_SCRIPT:
            self.log_line.emit(f"대본 생성: {progress_done + 1}/{total_progress_units}: 클립 길이에 맞게 대본 후처리")
            self._refine_narration_lengths(
                scenes=scenes,
                model=chosen_model or first_model,
                clip_values=clip_values,
                narration_length_rules=narration_length_rules,
                target_seconds=target_seconds,
                voice_min_chars=voice_min_chars,
                voice_max_chars=voice_max_chars,
            )
            progress_done += 1
            self.progress.emit(progress_done, total_progress_units)
        self.log_line.emit(f"대본 생성: {progress_done + 1}/{total_progress_units}: 공통 스타일/참조 프롬프트 생성")
        visual_style_prompt, reference_image_prompt = self._generate_style_prompts(
            model=chosen_model or first_model,
            prompt_text=prompt_text,
            script_input_mode=script_input_mode,
            scenes=scenes,
        )
        progress_done += 1
        self.progress.emit(progress_done, total_progress_units)
        progress_done = self._generate_scene_visual_prompts(
            model=chosen_model or first_model,
            prompt_text=prompt_text,
            visual_style_prompt=visual_style_prompt,
            reference_image_prompt=reference_image_prompt,
            scenes=scenes,
            progress_done=progress_done,
            progress_total=total_progress_units,
        )
        stats = _timeline_stats(
            scenes,
            target_minutes=int(self._args["target_minutes"]),
            clip_seconds_mode=str(self._args.get("clip_seconds_mode", CLIP_SECONDS_MODE_LLM)),
            clip_duration_values=clip_values,
        )
        narration_issues = _narration_length_issues(scenes, values=clip_values)
        if narration_issues:
            self.log_line.emit("대본 길이 점검: " + " / ".join(narration_issues[:5]))
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
            "reference_image_prompt": reference_image_prompt,
            "timeline_warning": validation_error,
            "timeline_stats": stats,
        }

    def _generate_script_text(self, *, system_instruction: str, user_text: str, label: str) -> tuple[str, str]:
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
                    user_text=user_text,
                    max_retries=3,
                )
                if model != first_model:
                    self.log_line.emit(f"{label} 모델 fallback 사용: {model}")
                chosen_model = model
                break
            except Exception as e:
                last_error = str(e)
                retryable = any(token in last_error for token in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"))
                if not retryable:
                    raise
                self.log_line.emit(f"{label} 모델 {model} 일시 실패: {last_error}")
        if not content:
            raise RuntimeError(last_error or f"{label}에 실패했습니다.")
        return content, chosen_model or first_model

    def _json_object_from_llm(self, content: str, *, label: str) -> dict[str, Any]:
        try:
            data = json.loads(strip_markdown_json_fence(content))
        except json.JSONDecodeError as e:
            raise ValueError(f"{label} JSON 파싱 실패: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"{label} JSON 루트는 객체여야 합니다.")
        return data

    def _scene_split_system_instruction(self, clip_values: tuple[int, ...]) -> str:
        values_text = ", ".join(str(v) for v in clip_values)
        return (
            "당신은 한국어 YouTube 영상용 씬 분할 보조입니다. "
            "JSON 객체 하나만 출력하세요. 코드펜스와 설명은 쓰지 마세요.\n\n"
            "출력 형식:\n"
            '{"scenes":[{"scene_id":1,"narration_ko":"한국어 내레이션","clip_seconds":10,"transition":"fade"}]}\n\n'
            "규칙:\n"
            "- scenes만 출력합니다. visual_prompt_ko와 video_prompt_ko는 출력하지 마세요.\n"
            "- scene_id는 1부터 연속 번호입니다.\n"
            f"- clip_seconds는 반드시 {values_text} 중 하나입니다.\n"
            "- transition은 fade 또는 cut만 사용합니다.\n"
            "- narration_ko는 한국어 TTS로 자연스럽게 읽을 문장입니다.\n"
            "- 문자열 안 줄바꿈은 넣지 말고 한 줄 문장으로 작성하세요.\n"
        )

    def _parse_scene_split_json(self, content: str, clip_values: tuple[int, ...]) -> list[Scene]:
        data = self._json_object_from_llm(content, label="대본/씬 분할")
        raw_scenes = data.get("scenes")
        if not isinstance(raw_scenes, list) or not raw_scenes:
            raise ValueError("대본/씬 분할 JSON에 scenes 배열이 없습니다.")
        scenes: list[Scene] = []
        for i, item in enumerate(raw_scenes, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"scenes[{i - 1}]가 객체가 아닙니다.")
            clip_seconds = _normalize_clip_seconds_to_values(item.get("clip_seconds", 0), clip_values)
            transition = str(item.get("transition", "fade") or "fade").strip()
            if transition not in ("fade", "cut"):
                transition = "fade"
            scenes.append(
                Scene(
                    scene_id=i,
                    narration_ko=str(item.get("narration_ko", "") or "").strip(),
                    visual_prompt_ko="",
                    video_prompt_ko="",
                    clip_seconds=clip_seconds,
                    llm_clip_seconds=clip_seconds,
                    transition=transition,
                )
            )
        return scenes

    def _generate_style_prompts(
        self,
        *,
        model: str,
        prompt_text: str,
        script_input_mode: str,
        scenes: list[Scene],
    ) -> tuple[str, str]:
        existing_style = str(self._args.get("visual_style_prompt", "") or "").strip()
        existing_reference = str(self._args.get("reference_image_prompt", "") or "").strip()
        if existing_style and existing_reference:
            return existing_style, existing_reference
        scene_summary = "\n".join(f"{s.scene_id}. {s.narration_ko}" for s in scenes[:12])
        system_instruction = (
            "당신은 영상 이미지 생성을 위한 스타일 프롬프트 작성자입니다. "
            "JSON 객체 하나만 출력하세요. 코드펜스와 설명은 쓰지 마세요."
        )
        user_text = (
            "아래 영상 기획과 씬 대본을 바탕으로 공통 스타일과 참조 이미지 프롬프트를 작성하세요.\n"
            "- 모든 내용은 한국어로 작성하세요.\n"
            "- visual_style_prompt에는 매체, 색감, 조명, 캐릭터 비율, 질감, 카메라 톤을 포함하세요.\n"
            "- reference_image_prompt에는 반복 유지할 인물/캐릭터, 의상, 배경 구조, 핵심 소품, 색감, 조명을 포함하세요.\n"
            '- 출력 형식: {"visual_style_prompt":"...","reference_image_prompt":"..."}\n\n'
            f"[입력 모드]\n{script_input_mode}\n\n"
            f"[기획 또는 전체 대본]\n{prompt_text}\n\n"
            f"[씬 대본]\n{scene_summary}"
        )
        _log_prompt("LLM style prompt system instruction", system_instruction)
        _log_prompt("LLM style prompt user instruction", user_text)
        content = gemini_generate_content(
            str(self._args["gemini_api_key"]),
            model,
            system_instruction=system_instruction,
            user_text=user_text,
            max_retries=2,
        )
        data = self._json_object_from_llm(content, label="스타일 프롬프트")
        style = existing_style or str(data.get("visual_style_prompt", "") or "").strip()
        reference = existing_reference or str(data.get("reference_image_prompt", "") or "").strip()
        return style, reference

    def _generate_scene_visual_prompts(
        self,
        *,
        model: str,
        prompt_text: str,
        visual_style_prompt: str,
        reference_image_prompt: str,
        scenes: list[Scene],
        progress_done: int = 0,
        progress_total: int = 0,
    ) -> int:
        system_instruction = (
            "당신은 영상 씬별 이미지/영상 프롬프트 작성자입니다. "
            "JSON 객체 하나만 출력하세요. 코드펜스와 설명은 쓰지 마세요."
        )
        batches = list(range(0, len(scenes), 5))
        for batch_index, start in enumerate(batches, start=1):
            batch = scenes[start : start + 5]
            if progress_total > 0:
                self.log_line.emit(
                    f"대본 생성: {progress_done + 1}/{progress_total}: "
                    f"씬별 이미지/영상 프롬프트 생성 {batch_index}/{len(batches)}"
                )
            payload = {
                "project_prompt": prompt_text,
                "visual_style_prompt": visual_style_prompt,
                "reference_image_prompt": reference_image_prompt,
                "scenes": [
                    {
                        "scene_id": scene.scene_id,
                        "narration_ko": scene.narration_ko,
                    }
                    for scene in batch
                ],
            }
            user_text = (
                "아래 씬 대본에 맞춰 visual_prompt_ko와 video_prompt_ko를 작성하세요.\n"
                "- scene_id 개수와 순서를 유지하세요.\n"
                "- narration_ko는 출력하지 마세요.\n"
                "- visual_prompt_ko는 이미지 생성을 위한 짧은 한국어 장면 묘사입니다.\n"
                "- video_prompt_ko는 이미지가 영상으로 변할 때의 움직임, 카메라, 연출 묘사입니다.\n"
                "- video_prompt_ko에는 반드시 무음 영상, 대사 없음, 내레이션 없음, 배경음악 없음, 효과음 없음 조건을 포함하세요.\n"
                '- 출력 형식: {"scenes":[{"scene_id":1,"visual_prompt_ko":"...","video_prompt_ko":"..."}]}\n\n'
                f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
            )
            _log_prompt("LLM scene prompt system instruction", system_instruction)
            _log_prompt("LLM scene prompt user instruction", user_text)
            content = gemini_generate_content(
                str(self._args["gemini_api_key"]),
                model,
                system_instruction=system_instruction,
                user_text=user_text,
                max_retries=2,
            )
            data = self._json_object_from_llm(content, label="씬별 프롬프트")
            items = data.get("scenes")
            if not isinstance(items, list):
                raise ValueError("씬별 프롬프트 JSON에 scenes 배열이 없습니다.")
            by_id = {scene.scene_id: scene for scene in batch}
            for item in items:
                if not isinstance(item, dict):
                    continue
                scene_id = int(item.get("scene_id", 0) or 0)
                scene = by_id.get(scene_id)
                if scene is None:
                    continue
                scene.visual_prompt_ko = str(item.get("visual_prompt_ko", "") or "").strip()
                scene.video_prompt_ko = str(item.get("video_prompt_ko", "") or "").strip()
            progress_done += 1
            if progress_total > 0:
                self.progress.emit(progress_done, progress_total)
        return progress_done

    def _refine_narration_lengths(
        self,
        *,
        scenes: list[Scene],
        model: str,
        clip_values: tuple[int, ...],
        narration_length_rules: str,
        target_seconds: int,
        voice_min_chars: int,
        voice_max_chars: int,
    ) -> None:
        payload = {
            "target_clip_seconds": target_seconds,
            "allowed_clip_seconds": list(clip_values),
            "total_narration_chars_target": {
                "min": voice_min_chars,
                "max": voice_max_chars,
            },
            "narration_length_rules": narration_length_rules,
            "scenes": [
                {
                    "scene_id": scene.scene_id,
                    "clip_seconds": _normalize_clip_seconds_to_values(scene.clip_seconds, clip_values),
                    "narration_ko": scene.narration_ko,
                }
                for scene in scenes
            ],
        }
        system_instruction = (
            "당신은 한국어 TTS 영상 대본 편집자입니다. "
            "입력된 scene_id와 clip_seconds를 유지하면서 narration_ko만 클립 길이에 맞게 다듬습니다. "
            "반드시 JSON 객체 하나만 출력하고, 코드펜스나 설명은 쓰지 마세요."
        )
        user_text = (
            "아래 JSON의 각 씬 대본을 clip_seconds에 맞는 길이로 다시 작성하세요.\n"
            "- scene_id 개수와 순서는 절대 바꾸지 마세요.\n"
            "- clip_seconds도 바꾸지 마세요.\n"
            "- narration_ko만 수정하세요.\n"
            "- 각 narration_ko는 해당 clip_seconds의 글자 수 기준에 맞게 너무 짧거나 길지 않게 작성하세요.\n"
            "- 전체 narration_ko는 목표 글자 수 범위 안에 최대한 들어오게 하세요.\n"
            "- 기존 흐름과 설명 순서는 유지하되, 초등학생이 듣기 자연스럽게 문장을 다듬으세요.\n"
            '- 출력 형식: {"scenes":[{"scene_id":1,"narration_ko":"..."}]}\n\n'
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        _log_prompt("LLM narration refine system instruction", system_instruction)
        _log_prompt("LLM narration refine user instruction", user_text)
        try:
            refined = gemini_generate_content(
                str(self._args["gemini_api_key"]),
                model,
                system_instruction=system_instruction,
                user_text=user_text,
                max_retries=2,
            )
            data = json.loads(strip_markdown_json_fence(refined))
            items = data.get("scenes") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("대본 후처리 응답에 scenes 배열이 없습니다.")
            by_id: dict[int, str] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                scene_id = int(item.get("scene_id", 0) or 0)
                narration = str(item.get("narration_ko", "") or "").strip()
                if scene_id and narration:
                    by_id[scene_id] = narration
            for scene in scenes:
                narration = by_id.get(scene.scene_id)
                if narration:
                    scene.narration_ko = narration
            self.log_line.emit("대본 길이 후처리 완료")
        except Exception as e:
            self.log_line.emit(f"대본 길이 후처리 실패, 원본 대본 사용: {e}")

    def _script_models_to_try(self) -> list[str]:
        first = str(self._args.get("gemini_text_model", "")).strip()
        models: list[str] = []
        for model in (first, "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash"):
            if model and model not in models:
                models.append(model)
        return models

    def _images(self) -> dict[str, list[tuple[int, str]]]:
        parent = Path(self._args["project_parent"]).resolve()
        scenes: list[Scene] = self._args["scenes"]
        missing_only = str(self._args.get("asset_policy", ASSET_POLICY_REPLACE)) == ASSET_POLICY_MISSING_ONLY
        aspect = api_aspect_ratio_from_resolution(str(self._args["resolution"]))
        model_candidates = self._image_models_to_try()
        generated: list[tuple[int, str]] = []
        skipped: list[tuple[int, str]] = []
        reference_prompt = str(self._args.get("reference_image_prompt", "") or "").strip()
        if reference_prompt and not self._reference_image_paths(parent):
            self.log_line.emit("참조 이미지가 없어 먼저 생성합니다.")
            rel = self._reference_image()
            self._args["reference_image_relpath"] = rel
            self.log_line.emit(f"참조 이미지 생성 완료: {rel}")
        self.progress.emit(0, len(scenes))
        for i, scene in enumerate(scenes, start=1):
            if missing_only:
                existing_rel = _existing_scene_image_relpath(parent, scene)
                if existing_rel:
                    self.log_line.emit(f"씬 {scene.scene_id}: 기존 이미지 사용")
                    skipped.append((scene.scene_id, existing_rel))
                    self.progress.emit(i, len(scenes))
                    continue
            self.log_line.emit(f"씬 {scene.scene_id}: 이미지 생성")
            raw: bytes | None = None
            mime = "image/png"
            last_error = ""
            prompt = self._image_prompt(scene)
            reference_paths = self._reference_image_paths(parent)
            for model in model_candidates:
                try:
                    raw, mime = gemini_generate_image(
                        str(self._args["gemini_api_key"]),
                        model,
                        prompt=prompt,
                        reference_image_paths=reference_paths,
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
            rel = _relative(path, parent)
            generated.append((scene.scene_id, rel))
            self.asset_ready.emit(2, scene.scene_id, rel)
            self.progress.emit(i, len(scenes))
        return {"generated": generated, "skipped": skipped}

    def _reference_image_paths(self, parent: Path) -> list[Path]:
        rel = str(self._args.get("reference_image_relpath", "") or "").strip()
        if not rel:
            return []
        path = Path(rel)
        if not path.is_absolute():
            path = parent / rel
        return [path] if path.is_file() else []

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
        reference = str(self._args.get("reference_image_prompt", "") or "").strip()
        style_block = ""
        if style:
            style_block = (
                f"\n공통 스타일 가이드:\n{style}\n"
                "모든 씬에서 이 스타일, 색감, 조명, 캐릭터 비율, 질감, 카메라 톤을 일관되게 유지하세요.\n"
            )
        reference_block = ""
        if reference:
            reference_block = (
                f"\n참조 이미지 일관성 가이드:\n{reference}\n"
                "첨부된 참조 이미지가 있다면 인물 정체성, 의상, 배경 구조, 주요 소품, 색감, 조명을 유지하세요.\n"
            )
        return (
            "아래 설명에 맞는 단일 배경 이미지 또는 자료화면 이미지를 반드시 생성하세요.\n"
            "텍스트 설명만 하지 말고 이미지 1장을 반환하세요.\n"
            "이미지 안에는 글자, 자막, 로고, 워터마크를 넣지 마세요.\n"
            f"{style_block}\n"
            f"{reference_block}\n"
            f"장면 설명:\n{scene.visual_prompt_ko}"
        )

    def _videos(self) -> dict[str, list[tuple[int, str]]]:
        parent = Path(self._args["project_parent"]).resolve()
        scenes: list[Scene] = self._args["scenes"]
        aspect = api_aspect_ratio_from_resolution(str(self._args["resolution"]))
        missing_only = str(self._args.get("asset_policy", ASSET_POLICY_REPLACE)) == ASSET_POLICY_MISSING_ONLY
        generated: list[tuple[int, str]] = []
        skipped: list[tuple[int, str]] = []
        backend = str(self._args.get("video_backend", VIDEO_BACKEND_VEO) or VIDEO_BACKEND_VEO)
        clip_values = tuple(int(x) for x in self._args.get("clip_duration_values", (4, 6, 8)))
        self.progress.emit(0, len(scenes))
        for i, scene in enumerate(scenes, start=1):
            if not scene.image_relpath.strip():
                raise RuntimeError(f"씬 {scene.scene_id} 이미지가 없습니다.")
            clip = _scene_video_path(parent, scene.scene_id)
            if missing_only:
                existing_rel = _existing_scene_video_relpath(parent, scene)
                if existing_rel:
                    existing_path = parent / existing_rel
                    if _media_duration(existing_path) > 0:
                        self.log_line.emit(f"씬 {scene.scene_id}: 기존 영상 클립 사용")
                        skipped.append((scene.scene_id, existing_rel))
                        self.progress.emit(i, len(scenes))
                        continue
                    self.log_line.emit(f"씬 {scene.scene_id}: 기존 영상 클립 손상 감지, 다시 생성")
            video_prompt = scene.video_prompt_ko.strip() or scene.visual_prompt_ko.strip()
            clip_seconds = _scene_clip_seconds_for_values(
                scene,
                str(self._args.get("clip_seconds_mode", CLIP_SECONDS_MODE_LLM)),
                clip_values,
            )
            scene.clip_seconds = clip_seconds
            prompt = (
                f"{self._style_prefix_for_video()}{video_prompt}\n\n"
                "이 이미지를 기반으로 시네마틱한 영상 클립을 생성하세요. 부드러운 카메라 움직임을 사용하세요. "
                "화면 안에 읽을 수 있는 글자를 추가하지 마세요. 무음 비주얼 영상으로 만드세요. "
                "대사 없음, 내레이션 없음, 음성 없음, 배경음악 없음, 효과음 없음."
            )
            backend_label = {
                VIDEO_BACKEND_COMFYUI_WAN: "ComfyUI Wan",
                VIDEO_BACKEND_KLING_API: "Kling API",
            }.get(backend, "Veo")
            self.log_line.emit(f"scene {scene.scene_id}: {backend_label} video generation")
            self._generate_clip(
                prompt=prompt,
                image_path=parent / scene.image_relpath,
                clip=clip,
                aspect=aspect,
                clip_seconds=clip_seconds,
            )
            _require_valid_video(clip, f"씬 {scene.scene_id} 영상 클립")
            rel = _relative(clip, parent)
            generated.append((scene.scene_id, rel))
            self.asset_ready.emit(3, scene.scene_id, rel)
            self.progress.emit(i, len(scenes))
        return {"generated": generated, "skipped": skipped}

    def _generate_clip(self, *, prompt: str, image_path: Path, clip: Path, aspect: str, clip_seconds: int) -> None:
        backend = str(self._args.get("video_backend", VIDEO_BACKEND_VEO) or VIDEO_BACKEND_VEO)
        if backend == VIDEO_BACKEND_KLING_API:
            generate_video_from_image_kling_api(
                access_key=str(self._args["kling_access_key"]),
                secret_key=str(self._args["kling_secret_key"]),
                base_url=str(self._args["kling_api_base_url"]),
                model=str(self._args["kling_api_model"]),
                mode=str(self._args["kling_api_mode"]),
                prompt=prompt,
                image_path=image_path,
                out_video_path=clip,
                aspect_ratio=aspect,
                duration_seconds=clip_seconds,
                negative_prompt=str(self._args["kling_api_negative_prompt"]),
            )
            return
        if backend == VIDEO_BACKEND_COMFYUI_WAN:
            generate_video_from_image_comfyui_wan(
                base_url=str(self._args["comfyui_url"]),
                model=str(self._args["comfyui_wan_model"]),
                prompt=prompt,
                image_path=image_path,
                out_video_path=clip,
                resolution=str(self._args["comfyui_wan_resolution"]),
                duration_seconds=clip_seconds,
                seed=int(self._args["comfyui_wan_seed"]),
                negative_prompt=str(self._args["comfyui_wan_negative_prompt"]),
                prompt_extend=bool(self._args["comfyui_wan_prompt_extend"]),
                watermark=bool(self._args["comfyui_wan_watermark"]),
                workflow_path=str(self._args["comfyui_wan_workflow_path"]),
            )
            return
        generate_video_from_image(
            api_key=str(self._args["gemini_api_key"]),
            model=str(self._args["veo_model"]),
            prompt=prompt,
            image_path=image_path,
            out_video_path=clip,
            resolution=str(self._args["veo_resolution"]),
            aspect_ratio=aspect,
            duration_seconds=clip_seconds,
        )

    def _concat(self) -> str:
        parent = Path(self._args["project_parent"]).resolve()
        clips = self._concat_clip_paths(parent)
        if not clips:
            raise RuntimeError("이어 붙일 영상 클립이 없습니다.")
        out = parent / "export" / "video_production_merged.mp4"
        width, height = parse_resolution(str(self._args["resolution"]))
        total = len(clips) + 1
        self.progress.emit(0, total)

        def on_normalize(index: int, count: int, path: Path) -> None:
            self.log_line.emit(f"영상 클립 정규화: {index}/{count} {path.name}")
            self.progress.emit(index, total)

        concat_segments_normalized(
            ffmpeg=which_ffmpeg(),
            segment_paths=clips,
            out_mp4=out,
            cwd=parent,
            width=width,
            height=height,
            fps=24,
            progress_callback=on_normalize,
        )
        _require_valid_video(out, "편집본")
        self.log_line.emit(f"편집본 생성: 정규화된 {len(clips)}개 클립 병합")
        self.progress.emit(total, total)
        return _relative(out, parent)

    def _concat_clip_paths(self, parent: Path) -> list[Path]:
        scenes: list[Scene] = self._args["scenes"]
        clips: list[Path] = []
        invalid: list[str] = []
        for scene in scenes:
            if not scene.notes.startswith("video_relpath:"):
                continue
            rel = scene.notes[len("video_relpath:") :].strip()
            path = parent / rel
            if _media_duration(path) <= 0:
                invalid.append(f"씬 {scene.scene_id}: {rel}")
                continue
            clips.append(path)
        if invalid:
            raise RuntimeError("손상되었거나 읽을 수 없는 영상 클립이 있습니다.\n" + "\n".join(invalid))
        return clips

    def _style_prefix_for_video(self) -> str:
        style = str(self._args.get("visual_style_prompt", "") or "").strip()
        if not style:
            return ""
        return (
            f"전체 프로젝트 공통 시각 스타일: {style}\n"
            "이전 씬과 스타일을 일관되게 유지하세요.\n\n"
        )

    def _narration_text(self) -> str:
        scenes: list[Scene] = self._args["scenes"]
        return "\n\n".join(s.narration_ko for s in scenes if s.narration_ko.strip()).strip()

    def _narration_segments(self) -> list[str]:
        scenes: list[Scene] = self._args["scenes"]
        raw_segments: list[str] = []
        for scene in scenes:
            text = scene.narration_ko.strip()
            if not text:
                continue
            parts = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
            raw_segments.extend(parts or [text])
        total_chars = sum(len("".join(segment.split())) for segment in raw_segments)
        target_chars = max(80, math.ceil(total_chars / max(1, GEMINI_TTS_TARGET_SEGMENTS)))
        split_segments: list[str] = []
        for segment in raw_segments:
            split_segments.extend(_split_tts_text_chunk(segment, max_chars=target_chars))
        segments: list[str] = []
        current: list[str] = []
        current_chars = 0
        for segment in split_segments:
            chars = len("".join(segment.split()))
            if current and current_chars + chars > target_chars:
                segments.append("\n\n".join(current))
                current = []
                current_chars = 0
            current.append(segment)
            current_chars += chars
        if current:
            segments.append("\n\n".join(current))
        return segments

    def _voice_target_seconds(self) -> float:
        parent = Path(self._args["project_parent"]).resolve()
        merged_rel = str(self._args.get("merged_video_relpath", "") or "").strip()
        if merged_rel:
            merged_duration = _media_duration(parent / merged_rel)
            if merged_duration > 0:
                return merged_duration
        scenes: list[Scene] = self._args["scenes"]
        clip_values = tuple(int(x) for x in self._args.get("clip_duration_values", (4, 6, 8)))
        target = sum(
            _scene_clip_seconds_for_values(
                scene,
                str(self._args.get("clip_seconds_mode", CLIP_SECONDS_MODE_LLM)),
                clip_values,
            )
            for scene in scenes
            if scene.narration_ko.strip()
        )
        return max(0.0, float(target))

    def _fit_voice_to_clip_length(self, *, audio_path: Path, srt_path: Path, target_seconds: float) -> None:
        if target_seconds <= 0 or not audio_path.is_file():
            return
        original_seconds = _media_duration(audio_path)
        if original_seconds <= 0:
            return
        delta = original_seconds - target_seconds
        if abs(delta) < 0.25:
            return
        tempo = original_seconds / target_seconds
        if tempo < VOICE_TEMPO_MIN or tempo > VOICE_TEMPO_MAX:
            self.log_line.emit(
                "음성 속도 자동 보정 생략: "
                f"음성 {original_seconds:.2f}s / 클립 {target_seconds:.2f}s / 필요 속도 {tempo:.2f}x "
                f"(허용 {VOICE_TEMPO_MIN:.2f}x~{VOICE_TEMPO_MAX:.2f}x)"
            )
            return

        tmp_audio = audio_path.with_name(f"{audio_path.stem}.tempo{audio_path.suffix}")
        if tmp_audio.exists():
            tmp_audio.unlink()
        run_ffmpeg(
            [
                which_ffmpeg(),
                "-y",
                "-i",
                str(audio_path.resolve()),
                "-filter:a",
                f"{_atempo_filter(tempo)},aresample=48000",
                "-vn",
                "-ar",
                "48000",
                "-ac",
                "1",
                str(tmp_audio.resolve()),
            ],
            cwd=audio_path.parent,
            timeout_sec=7200.0,
        )
        tmp_audio.replace(audio_path)

        adjusted_seconds = _media_duration(audio_path)
        if srt_path.is_file() and adjusted_seconds > 0:
            cues = parse_srt_file(srt_path)
            if cues:
                scale = adjusted_seconds / original_seconds
                scaled = [(st * scale, en * scale, text) for st, en, text in cues]
                srt_path.write_text(_srt_from_cues(scaled), encoding="utf-8")

        self.log_line.emit(
            "음성 속도 자동 보정: "
            f"{original_seconds:.2f}s → {adjusted_seconds:.2f}s "
            f"(목표 {target_seconds:.2f}s, 속도 {tempo:.2f}x)"
        )

    def _voice(self) -> str:
        parent = Path(self._args["project_parent"]).resolve()
        text = self._narration_text()
        if not text.strip():
            raise RuntimeError("음성으로 만들 대본이 없습니다.")
        self.progress.emit(0, 1)
        provider = str(self._args.get("voice_provider", VOICE_PROVIDER_ELEVENLABS) or VOICE_PROVIDER_ELEVENLABS)
        if provider == VOICE_PROVIDER_GEMINI_TTS:
            if _bool_value(self._args.get("gemini_tts_split_audio"), DEFAULT_GEMINI_TTS_SPLIT_AUDIO):
                segments = self._narration_segments()
                self.log_line.emit(
                    f"Gemini TTS: {len(segments)}개 묶음으로 나눠 48kHz WAV 생성 "
                    f"(목표 {GEMINI_TTS_TARGET_SEGMENTS}분할)"
                )
                result = synthesize_gemini_speech_segments(
                    text_segments=segments,
                    out_audio_path=parent / "audio" / "video_production_narration.wav",
                    out_srt_path=parent / "subs" / "video_production_voice_alignment.srt",
                    api_key=str(self._args["gemini_api_key"]),
                    model_id=str(self._args["gemini_tts_model"]),
                    voice_name=str(self._args["gemini_tts_voice_name"]),
                    style_prompt=str(self._args["gemini_tts_style_prompt"]),
                    max_line_chars=int(self._args["max_subtitle_chars"]),
                    progress_callback=lambda index, total: self.log_line.emit(
                        f"Gemini TTS 조각 {index}/{total} 생성 중..."
                    ),
                )
            else:
                self.log_line.emit("Gemini TTS: 분할 생성 끔 - 전체 대본을 한 번에 48kHz WAV 생성")
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
            if bool(self._args.get("auto_fit_voice_to_clip", True)):
                self._fit_voice_to_clip_length(
                    audio_path=result.audio_path,
                    srt_path=result.srt_path,
                    target_seconds=self._voice_target_seconds(),
                )
            else:
                self.log_line.emit("음성 속도 자동 보정 생략: 옵션이 꺼져 있습니다.")
            self.progress.emit(1, 1)
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
        if bool(self._args.get("auto_fit_voice_to_clip", True)):
            self._fit_voice_to_clip_length(
                audio_path=result.audio_path,
                srt_path=result.srt_path,
                target_seconds=self._voice_target_seconds(),
            )
        else:
            self.log_line.emit("음성 속도 자동 보정 생략: 옵션이 꺼져 있습니다.")
        self.progress.emit(1, 1)
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
        stt_raw_out = parent / "subs" / "video_production_narration_stt_raw.srt"
        refined_out = parent / "subs" / "video_production_narration_refined.srt"
        self.progress.emit(0, 100)
        if audio_rel:
            audio_path = parent / audio_rel
            if not audio_path.is_file():
                raise RuntimeError(f"자막을 만들 음성 파일이 없습니다: {audio_rel}")
            try:
                self.log_line.emit("최종 음성 파일 기준 STT 자막 생성 시작")
                last_stt_log_sec = -30.0

                def on_stt_progress(current_sec: float, total_sec: float, status: str) -> None:
                    nonlocal last_stt_log_sec
                    total = float(total_sec or _media_duration(audio_path) or 0.0)
                    current = max(0.0, float(current_sec or 0.0))
                    if total > 0:
                        percent = min(78, max(0, int((current / total) * 78)))
                        self.progress.emit(percent, 100)
                        if current - last_stt_log_sec >= 25.0 or current >= total:
                            self.log_line.emit(
                                f"{status}: {_short_clock(current)} / {_short_clock(total)}"
                            )
                            last_stt_log_sec = current
                    else:
                        self.progress.emit(5, 100)

                lines = transcribe_wav_sentences(audio_path, language="ko", progress_callback=on_stt_progress)
                self.progress.emit(80, 100)
                out.parent.mkdir(parents=True, exist_ok=True)
                stt_raw_out.write_text(
                    build_srt_from_timed_lines(lines, max_line_chars=int(self._args["max_subtitle_chars"])),
                    encoding="utf-8",
                )
                self.log_line.emit(f"STT 원본 자막 저장: {_relative(stt_raw_out, parent)}")
                uses_word_timestamps = any(str(line.get("_source", "")) == "faster_word" for line in lines)
                if uses_word_timestamps:
                    self.log_line.emit(f"faster-whisper 단어 타임스탬프 기반 구간 생성: {len(lines)}개")
                self.log_line.emit("STT 자막 텍스트를 원본 대본 기준으로 교정 시작")
                lines = refine_timed_lines_with_reference_script(
                    lines=lines,
                    reference_script=text,
                    api_key=str(self._args["gemini_api_key"]),
                    model=str(self._args["gemini_text_model"]),
                )
                self.progress.emit(94, 100)
                refined_out.write_text(
                    build_srt_from_timed_lines(lines, max_line_chars=int(self._args["max_subtitle_chars"])),
                    encoding="utf-8",
                )
                self.log_line.emit(f"Gemini 교정 자막 저장: {_relative(refined_out, parent)}")
                out.parent.mkdir(parents=True, exist_ok=True)
                self.progress.emit(98, 100)
                out.write_text(
                    build_srt_from_timed_lines(lines, max_line_chars=int(self._args["max_subtitle_chars"])),
                    encoding="utf-8",
                )
                self.progress.emit(100, 100)
                self.log_line.emit(f"최종 음성 파일 기준 STT 자막 생성 완료: {len(lines)}개 구간")
                return _relative(out, parent)
            except Exception as e:
                if alignment.is_file():
                    self.log_line.emit(f"STT 자막 생성 실패, TTS alignment 자막 사용: {e}")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(alignment.read_text(encoding="utf-8"), encoding="utf-8")
                    self.progress.emit(100, 100)
                    return _relative(out, parent)
                self.log_line.emit(f"STT 자막 생성 실패, 길이 기반 자막으로 대체: {e}")
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
            clip_values = tuple(int(x) for x in self._args.get("clip_duration_values", (4, 6, 8)))
            duration = max(
                0.04,
                sum(
                    _scene_clip_seconds_for_values(
                        s,
                        str(self._args.get("clip_seconds_mode", CLIP_SECONDS_MODE_LLM)),
                        clip_values,
                    )
                    for s in scenes
                ),
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            _duration_srt(text, duration, max_line_chars=int(self._args["max_subtitle_chars"])),
            encoding="utf-8",
        )
        self.progress.emit(100, 100)
        return _relative(out, parent)

    def _final(self) -> str:
        parent = Path(self._args["project_parent"]).resolve()
        out = parent / "export" / "video_production_final.mp4"
        self.progress.emit(0, 1)
        use_voice = bool(self._args.get("use_voice_in_final", True))
        use_subtitles = bool(self._args.get("use_subtitles_in_final", True))
        audio_mode = _normalize_final_audio_mode(self._args.get("final_audio_mode", FINAL_AUDIO_NARRATION_ONLY))
        audio_uses_narration = use_voice and audio_mode in (FINAL_AUDIO_NARRATION_ONLY, FINAL_AUDIO_MIX)
        audio_path = parent / str(self._args["audio_relpath"]) if audio_uses_narration else None
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
        self.progress.emit(10, 100)
        video_pad_sec = max(0.0, target_duration - video_duration)
        if video_pad_sec > 0.01:
            self.log_line.emit(
                f"최종 영상 길이 보정: 영상 {video_duration:.2f}s → {target_duration:.2f}s "
                f"(마지막 프레임 {video_pad_sec:.2f}s 연장)"
            )
        self.progress.emit(15, 100)

        def on_ffmpeg_progress(percent: int) -> None:
            mapped = 15 + int(max(0, min(99, percent)) * 83 / 100)
            self.progress.emit(mapped, 100)

        _compose_final_video(
            ffmpeg=which_ffmpeg(),
            input_video=input_video,
            narration_audio=audio_path,
            srt_path=srt_path,
            output_video=out,
            cwd=parent,
            video_pad_sec=video_pad_sec,
            audio_mode=audio_mode,
            clip_audio_volume=float(self._args.get("clip_audio_volume_percent", 20) or 20) / 100.0,
            narration_audio_volume=float(self._args.get("narration_audio_volume_percent", 100) or 100) / 100.0,
            duration_sec=target_duration,
            progress_callback=on_ffmpeg_progress,
        )
        self.progress.emit(100, 100)
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
        self._syncing_form = False
        self._scenes: list[Scene] = []
        self._scene_table_rows: list[dict[str, object]] = []
        self._result_table_rows: list[dict[str, str]] = []
        self._merged_video_relpath = ""
        self._audio_relpath = ""
        self._srt_relpath = ""
        self._final_video_relpath = ""
        self._reference_image_relpath = ""
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
            ("3. 클립 영상 생성", 3),
            ("4. 편집본 생성", 4),
            ("5. 음성 생성", 5),
            ("6. 자막 생성", 6),
            ("7. 음성/자막 입히기", 7),
        )):
            btn = QPushButton(label)
            btn.setMinimumHeight(34)
            btn.clicked.connect(lambda _checked=False, s=step: self._run_step(s))
            step_lay.addWidget(btn, 0, col)
            self._buttons.append(btn)
        root.addWidget(steps)

        top = QGroupBox("제작 프로젝트 입력")
        top_lay = QVBoxLayout(top)
        mode_row = QHBoxLayout()
        self._combo_script_input_mode = QComboBox()
        self._combo_script_input_mode.addItem("주제/기획으로 생성", SCRIPT_INPUT_MODE_TOPIC)
        self._combo_script_input_mode.addItem("전체 대본으로 씬 구성", SCRIPT_INPUT_MODE_FULL_SCRIPT)
        self._combo_script_input_mode.currentIndexChanged.connect(lambda _i: self._on_script_input_mode_changed())
        mode_row.addWidget(QLabel("대본 입력 방식"))
        mode_row.addWidget(self._combo_script_input_mode)
        mode_row.addStretch(1)
        top_lay.addLayout(mode_row)
        self._prompt = QTextEdit()
        self._prompt.setPlaceholderText("만들고 싶은 영상 주제, 톤, 대상 시청자를 입력하세요.")
        self._prompt.textChanged.connect(self.stateChanged.emit)
        top_lay.addWidget(self._prompt)

        form = QGridLayout()
        self._spin_minutes = QSpinBox()
        self._spin_minutes.setRange(1, 120)
        self._spin_minutes.setValue(1)
        self._combo_clip_seconds_mode = QComboBox()
        self._combo_clip_seconds_mode.addItem("LLM 초", CLIP_SECONDS_MODE_LLM)
        self._combo_clip_seconds_mode.addItem("보정 초", CLIP_SECONDS_MODE_ADJUSTED)
        self._combo_clip_seconds_mode.currentIndexChanged.connect(lambda _i: self._on_clip_seconds_mode_changed())
        self._edit_resolution = QLineEdit("1920x1080")
        self._spin_minutes.valueChanged.connect(lambda _v: self.stateChanged.emit())
        self._edit_resolution.editingFinished.connect(self.stateChanged.emit)
        form.addWidget(QLabel("목표 분"), 0, 0)
        form.addWidget(self._spin_minutes, 0, 1)
        form.addWidget(QLabel("길이 기준"), 0, 2)
        form.addWidget(self._combo_clip_seconds_mode, 0, 3)
        form.addWidget(QLabel("해상도"), 0, 4)
        form.addWidget(self._edit_resolution, 0, 5)
        top_lay.addLayout(form)

        voice_row = QHBoxLayout()
        self._label_voice = QLabel("")
        self._btn_find_voice = QPushButton("어울리는 목소리 찾기")
        self._btn_find_voice.clicked.connect(self._find_matching_voice)
        self._check_use_voice = QCheckBox("최종 영상에 음성 입히기")
        self._check_use_voice.setChecked(True)
        self._check_use_voice.toggled.connect(lambda _checked: self._on_final_audio_options_changed())
        self._check_use_subtitles = QCheckBox("최종 영상에 자막 입히기")
        self._check_use_subtitles.setChecked(True)
        self._check_use_subtitles.toggled.connect(lambda _checked: self._on_final_audio_options_changed())
        self._combo_final_audio_mode = QComboBox()
        self._combo_final_audio_mode.addItem("대본 음성만", FINAL_AUDIO_NARRATION_ONLY)
        self._combo_final_audio_mode.addItem("클립 원음만", FINAL_AUDIO_CLIP_ONLY)
        self._combo_final_audio_mode.addItem("클립 원음 + 대본 음성", FINAL_AUDIO_MIX)
        self._combo_final_audio_mode.currentIndexChanged.connect(lambda _i: self._on_final_audio_options_changed())
        self._spin_clip_audio_volume = QSpinBox()
        self._spin_clip_audio_volume.setRange(0, 200)
        self._spin_clip_audio_volume.setValue(20)
        self._spin_clip_audio_volume.setSuffix("%")
        self._spin_clip_audio_volume.valueChanged.connect(lambda _v: self._on_final_audio_options_changed())
        self._spin_narration_audio_volume = QSpinBox()
        self._spin_narration_audio_volume.setRange(0, 200)
        self._spin_narration_audio_volume.setValue(100)
        self._spin_narration_audio_volume.setSuffix("%")
        self._spin_narration_audio_volume.valueChanged.connect(lambda _v: self._on_final_audio_options_changed())
        self._check_auto_fit_voice = QCheckBox("음성 속도 자동 보정")
        self._check_auto_fit_voice.setChecked(True)
        self._check_auto_fit_voice.toggled.connect(lambda _checked: self._on_final_audio_options_changed())
        self._check_gemini_tts_split_audio = QCheckBox("Gemini 음성 분할")
        self._check_gemini_tts_split_audio.setChecked(
            _bool_value(
                self._settings.value("gemini_tts/split_audio", DEFAULT_GEMINI_TTS_SPLIT_AUDIO),
                DEFAULT_GEMINI_TTS_SPLIT_AUDIO,
            )
        )
        self._check_gemini_tts_split_audio.toggled.connect(lambda _checked: self._on_final_audio_options_changed())
        voice_row.addWidget(self._label_voice, stretch=1)
        voice_row.addWidget(self._check_use_voice)
        voice_row.addWidget(self._check_use_subtitles)
        voice_row.addWidget(self._check_auto_fit_voice)
        voice_row.addWidget(self._check_gemini_tts_split_audio)
        voice_row.addWidget(QLabel("최종 오디오"))
        voice_row.addWidget(self._combo_final_audio_mode)
        voice_row.addWidget(QLabel("원음"))
        voice_row.addWidget(self._spin_clip_audio_volume)
        voice_row.addWidget(QLabel("대본"))
        voice_row.addWidget(self._spin_narration_audio_volume)
        voice_row.addWidget(self._btn_find_voice)
        top_lay.addLayout(voice_row)
        self._label_length_status = QLabel("영상/음성 길이: 아직 계산되지 않았습니다.")
        self._label_length_status.setWordWrap(True)
        top_lay.addWidget(self._label_length_status)
        root.addWidget(top)

        style_box = QGroupBox("스타일/참조 정보")
        style_lay = QVBoxLayout(style_box)

        self._style_prompt = QPlainTextEdit()
        self._style_prompt.setPlaceholderText(
            "공통 이미지 스타일 예: 밝고 따뜻한 3D 애니메이션, 둥근 캐릭터, 파스텔 색감, 부드러운 조명, 동일한 카메라 톤"
        )
        self._style_prompt.setMaximumHeight(72)
        self._style_prompt.textChanged.connect(self._on_style_or_reference_changed)
        style_lay.addWidget(QLabel("공통 이미지 스타일"))
        style_lay.addWidget(self._style_prompt)

        self._reference_prompt = QPlainTextEdit()
        self._reference_prompt.setPlaceholderText(
            "참조 이미지 설명: 모든 씬에서 유지할 인물, 의상, 배경, 소품, 색감"
        )
        self._reference_prompt.setMaximumHeight(72)
        self._reference_prompt.textChanged.connect(self._on_style_or_reference_changed)
        style_lay.addWidget(QLabel("참조 이미지 프롬프트"))
        style_lay.addWidget(self._reference_prompt)

        reference_row = QHBoxLayout()
        self._reference_image = QLineEdit()
        self._reference_image.setPlaceholderText("선택) 예: images/video_production/reference.png")
        self._reference_image.editingFinished.connect(self._on_reference_path_changed)
        self._btn_browse_reference = QPushButton("찾기...")
        self._btn_browse_reference.clicked.connect(self._browse_reference_image)
        self._btn_generate_reference = QPushButton("참조 이미지 생성")
        self._btn_generate_reference.clicked.connect(self._generate_reference_image)
        self._btn_preview_reference = QPushButton("미리 보기")
        self._btn_preview_reference.clicked.connect(self._show_reference_preview_dialog)
        reference_row.addWidget(self._reference_image, stretch=1)
        reference_row.addWidget(self._btn_browse_reference)
        reference_row.addWidget(self._btn_generate_reference)
        reference_row.addWidget(self._btn_preview_reference)
        style_lay.addLayout(reference_row)

        root.addWidget(style_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._scene_table = QTableWidget(0, 7)
        self._scene_table.setHorizontalHeaderLabels(["씬", "길이", "대본", "이미지 프롬프트", "영상 프롬프트", "이미지", "영상"])
        self._scene_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._scene_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._scene_table.itemSelectionChanged.connect(self._on_scene_selection_changed)
        self._scene_table.itemChanged.connect(self._on_scene_table_item_changed)

        table_panel = QWidget()
        table_lay = QVBoxLayout(table_panel)
        table_lay.setContentsMargins(0, 0, 0, 0)
        table_lay.addWidget(self._scene_table, stretch=3)
        scene_table_buttons = QHBoxLayout()
        self._btn_scene_add = QPushButton("씬 추가")
        self._btn_scene_add.clicked.connect(self._add_scene_after_selection)
        self._btn_scene_delete = QPushButton("씬 삭제")
        self._btn_scene_delete.clicked.connect(self._delete_selected_scene)
        self._btn_scene_export = QPushButton("테이블 내보내기")
        self._btn_scene_export.clicked.connect(self._export_scene_table)
        self._btn_scene_import = QPushButton("테이블 가져오기")
        self._btn_scene_import.clicked.connect(self._import_scene_table)
        scene_table_buttons.addStretch(1)
        scene_table_buttons.addWidget(self._btn_scene_import)
        scene_table_buttons.addWidget(self._btn_scene_export)
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
        self._edit_scene_clip_seconds.setRange(4, 10)
        self._edit_scene_clip_seconds.setSingleStep(1)
        self._edit_scene_clip_seconds.valueChanged.connect(lambda _v: self._on_scene_editor_changed())
        form_edit = QFormLayout()
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
        self._btn_scene_select_image = QPushButton("이미지 선택")
        self._btn_scene_select_image.clicked.connect(self._select_scene_image_file)
        self._btn_scene_select_clip = QPushButton("영상 선택")
        self._btn_scene_select_clip.clicked.connect(self._select_scene_video_file)
        for btn in (
            self._btn_scene_save,
            self._btn_scene_select_image,
            self._btn_scene_select_clip,
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
        self._syncing_form = True
        if project.prompt_ko and not self._prompt.toPlainText().strip():
            self._prompt.setPlainText(project.prompt_ko)
        script_mode_index = self._combo_script_input_mode.findData(_normalize_script_input_mode(project.script_input_mode))
        self._combo_script_input_mode.setCurrentIndex(max(0, script_mode_index))
        self._spin_minutes.setValue(max(1, int(project.target_minutes)))
        mode_index = self._combo_clip_seconds_mode.findData(_normalize_clip_seconds_mode(project.clip_seconds_mode))
        self._combo_clip_seconds_mode.setCurrentIndex(max(0, mode_index))
        self._edit_resolution.setText(project.resolution or "1920x1080")
        self._reference_prompt.setPlainText(project.reference_image_prompt)
        self._reference_image_relpath = project.reference_image_relpath
        self._reference_image.setText(self._reference_image_relpath)
        if project.export_final_relpath:
            self._final_video_relpath = project.export_final_relpath
        self._scenes = list(project.scenes)
        self._load_state(project_parent)
        self._refresh_scene_table()
        self._refresh_result_table()
        self._refresh_buttons()
        self._refresh_voice_label()
        self._refresh_length_status()
        self._syncing_form = False

    def apply_to_project(self, project: StoryProject) -> None:
        self._commit_scene_editor_to_model()
        project.prompt_ko = self._prompt.toPlainText().strip()
        project.script_input_mode = self._script_input_mode()
        project.target_minutes = int(self._spin_minutes.value())
        project.clip_seconds_mode = self._clip_seconds_mode()
        project.resolution = self._edit_resolution.text().strip() or "1920x1080"
        project.fps = 24
        project.scenes = list(self._scenes)
        project.merged_srt_relpath = self._srt_relpath
        project.export_final_relpath = self._final_video_relpath
        project.reference_image_prompt = self._reference_prompt.toPlainText().strip()
        project.reference_image_relpath = self._reference_image.text().strip()
        self._save_state()

    def _project_parent(self) -> Path | None:
        parent = self._project_parent_getter()
        return parent.resolve() if parent is not None else None

    def _clip_seconds_mode(self) -> str:
        return _normalize_clip_seconds_mode(self._combo_clip_seconds_mode.currentData())

    def _script_input_mode(self) -> str:
        return _normalize_script_input_mode(self._combo_script_input_mode.currentData())

    def _final_audio_mode(self) -> str:
        return _normalize_final_audio_mode(self._combo_final_audio_mode.currentData())

    def _final_audio_uses_narration(self) -> bool:
        return self._check_use_voice.isChecked() and self._final_audio_mode() in (
            FINAL_AUDIO_NARRATION_ONLY,
            FINAL_AUDIO_MIX,
        )

    def _on_final_audio_options_changed(self) -> None:
        if self._syncing_form:
            return
        self._refresh_buttons()
        self._save_state()
        self.stateChanged.emit()

    def _on_script_input_mode_changed(self) -> None:
        if self._script_input_mode() == SCRIPT_INPUT_MODE_FULL_SCRIPT:
            self._prompt.setPlaceholderText("완성된 전체 내레이션 대본을 입력하세요. 이 대본을 의미 단위로 나누어 씬을 구성합니다.")
        else:
            self._prompt.setPlaceholderText("만들고 싶은 영상 주제, 톤, 대상 시청자를 입력하세요.")
        if self._syncing_form:
            return
        self._save_state()
        self.stateChanged.emit()

    def _on_clip_seconds_mode_changed(self) -> None:
        self._refresh_scene_table()
        self._sync_scene_editor_from_selection()
        self._refresh_length_status()
        self._save_state()
        self.stateChanged.emit()

    def _on_style_or_reference_changed(self) -> None:
        if self._syncing_form:
            return
        self._save_state()
        self.stateChanged.emit()

    def _on_reference_path_changed(self) -> None:
        self._reference_image_relpath = self._reference_image.text().strip()
        self._save_state()
        self.stateChanged.emit()

    def _browse_reference_image(self) -> None:
        parent = self._project_parent()
        if parent is None:
            QMessageBox.warning(self, "참조 이미지", "먼저 프로젝트를 저장해주세요.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "참조 이미지",
            str(parent),
            "이미지 (*.png *.jpg *.jpeg *.webp);;모든 파일 (*.*)",
        )
        if not path:
            return
        self._reference_image_relpath = _relative(Path(path), parent)
        self._reference_image.setText(self._reference_image_relpath)
        self._save_state()
        self.stateChanged.emit()

    def _generate_reference_image(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "작업 중", "이미 실행 중인 작업이 있습니다.")
            return
        if not self._reference_prompt.toPlainText().strip():
            QMessageBox.warning(self, "참조 이미지", "참조 이미지 프롬프트를 입력해주세요.")
            return
        try:
            args = self._args()
        except RuntimeError as e:
            QMessageBox.warning(self, "참조 이미지", str(e))
            return
        self._start_worker(9, args)

    def _reference_image_path(self) -> Path | None:
        parent = self._project_parent()
        rel = self._reference_image.text().strip()
        return (parent / rel) if parent is not None and rel else None

    def _show_reference_preview_dialog(self) -> None:
        path = self._reference_image_path()
        if path is None or not path.is_file():
            QMessageBox.information(self, "참조 이미지", "표시할 참조 이미지가 없습니다.")
            return
        pix = QPixmap(str(path))
        if pix.isNull():
            QMessageBox.warning(self, "참조 이미지", "참조 이미지를 표시할 수 없습니다.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("참조 이미지 미리 보기")
        dlg.resize(900, 640)
        lay = QVBoxLayout(dlg)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("background: #111111;")
        label.setPixmap(pix.scaled(860, 560, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(label, stretch=1)
        lay.addWidget(QLabel(str(path)))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        dlg.exec()

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
        mode_index = self._combo_clip_seconds_mode.findData(_normalize_clip_seconds_mode(data.get("clip_seconds_mode", self._clip_seconds_mode())))
        self._combo_clip_seconds_mode.setCurrentIndex(max(0, mode_index))
        script_mode_index = self._combo_script_input_mode.findData(
            _normalize_script_input_mode(data.get("script_input_mode", self._script_input_mode()))
        )
        self._combo_script_input_mode.setCurrentIndex(max(0, script_mode_index))
        self._style_prompt.setPlainText(str(data.get("visual_style_prompt", self._style_prompt.toPlainText()) or ""))
        self._reference_prompt.setPlainText(str(data.get("reference_image_prompt", self._reference_prompt.toPlainText()) or ""))
        self._reference_image_relpath = str(data.get("reference_image_relpath", self._reference_image_relpath))
        self._reference_image.setText(self._reference_image_relpath)
        self._audio_relpath = str(data.get("audio_relpath", ""))
        self._srt_relpath = str(data.get("srt_relpath", ""))
        self._final_video_relpath = str(data.get("final_video_relpath", self._final_video_relpath))
        if "use_voice_in_final" in data:
            self._check_use_voice.setChecked(bool(data.get("use_voice_in_final")))
        if "use_subtitles_in_final" in data:
            self._check_use_subtitles.setChecked(bool(data.get("use_subtitles_in_final")))
        if "auto_fit_voice_to_clip" in data:
            self._check_auto_fit_voice.setChecked(bool(data.get("auto_fit_voice_to_clip")))
        if "gemini_tts_split_audio" in data:
            self._check_gemini_tts_split_audio.setChecked(
                _bool_value(data.get("gemini_tts_split_audio"), DEFAULT_GEMINI_TTS_SPLIT_AUDIO)
            )
        audio_mode_index = self._combo_final_audio_mode.findData(
            _normalize_final_audio_mode(data.get("final_audio_mode", self._final_audio_mode()))
        )
        self._combo_final_audio_mode.setCurrentIndex(max(0, audio_mode_index))
        self._spin_clip_audio_volume.setValue(max(0, min(200, int(data.get("clip_audio_volume_percent", 20) or 20))))
        self._spin_narration_audio_volume.setValue(
            max(0, min(200, int(data.get("narration_audio_volume_percent", 100) or 100)))
        )

    def _save_state(self) -> None:
        parent = self._project_parent()
        if parent is None:
            return
        parent.mkdir(parents=True, exist_ok=True)
        self._state_path(parent).write_text(
            json.dumps(
                {
                    "merged_video_relpath": self._merged_video_relpath,
                    "clip_seconds_mode": self._clip_seconds_mode(),
                    "script_input_mode": self._script_input_mode(),
                    "visual_style_prompt": self._style_prompt.toPlainText().strip(),
                    "reference_image_prompt": self._reference_prompt.toPlainText().strip(),
                    "reference_image_relpath": self._reference_image.text().strip(),
                    "audio_relpath": self._audio_relpath,
                    "srt_relpath": self._srt_relpath,
                    "final_video_relpath": self._final_video_relpath,
                    "use_voice_in_final": self._check_use_voice.isChecked(),
                    "use_subtitles_in_final": self._check_use_subtitles.isChecked(),
                    "auto_fit_voice_to_clip": self._check_auto_fit_voice.isChecked(),
                    "gemini_tts_split_audio": self._check_gemini_tts_split_audio.isChecked(),
                    "final_audio_mode": self._final_audio_mode(),
                    "clip_audio_volume_percent": self._spin_clip_audio_volume.value(),
                    "narration_audio_volume_percent": self._spin_narration_audio_volume.value(),
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
        video_backend = str(self._settings.value("video/backend", VIDEO_BACKEND_VEO) or VIDEO_BACKEND_VEO)
        kling_model = str(self._settings.value("kling/model", "kling-v2-5-turbo") or "kling-v2-5-turbo")
        clip_values = _clip_duration_values(video_backend=video_backend, kling_model=kling_model)
        default_clip_seconds = _default_clip_seconds_for_values(clip_values)
        return {
            "project_parent": str(parent),
            "prompt": self._prompt.toPlainText().strip(),
            "script_input_mode": self._script_input_mode(),
            "visual_style_prompt": self._style_prompt.toPlainText().strip(),
            "reference_image_prompt": self._reference_prompt.toPlainText().strip(),
            "reference_image_relpath": self._reference_image.text().strip(),
            "target_minutes": self._spin_minutes.value(),
            "clip_seconds": default_clip_seconds,
            "clip_duration_values": list(clip_values),
            "clip_seconds_mode": self._clip_seconds_mode(),
            "resolution": self._edit_resolution.text().strip() or "1920x1080",
            "gemini_api_key": str(self._settings.value("gemini/api_key", "") or os.environ.get("GEMINI_API_KEY", "")),
            "gemini_text_model": str(self._settings.value("gemini/model", "gemini-2.5-flash") or "gemini-2.5-flash"),
            "gemini_image_model": str(
                self._settings.value("gemini/image_model", DEFAULT_GEMINI_IMAGE_MODEL)
                or DEFAULT_GEMINI_IMAGE_MODEL
            ),
            "veo_model": str(self._settings.value("video/veo_model", "veo-3.1-generate-preview") or "veo-3.1-generate-preview"),
            "veo_resolution": str(self._settings.value("video/veo_resolution", "720p") or "720p"),
            "video_backend": video_backend,
            "comfyui_url": str(self._settings.value("comfyui/url", "http://127.0.0.1:8188") or "http://127.0.0.1:8188"),
            "comfyui_wan_model": str(self._settings.value("comfyui/wan_model", "wan2.6-i2v") or "wan2.6-i2v"),
            "comfyui_wan_resolution": str(self._settings.value("comfyui/wan_resolution", "720P") or "720P"),
            "comfyui_wan_seed": int(self._settings.value("comfyui/wan_seed", 0) or 0),
            "comfyui_wan_negative_prompt": str(self._settings.value("comfyui/wan_negative_prompt", "") or ""),
            "comfyui_wan_prompt_extend": str(self._settings.value("comfyui/wan_prompt_extend", "true")).lower() not in ("0", "false", "no", "off"),
            "comfyui_wan_watermark": str(self._settings.value("comfyui/wan_watermark", "false")).lower() in ("1", "true", "yes", "on"),
            "comfyui_wan_workflow_path": str(self._settings.value("comfyui/wan_workflow_path", "") or ""),
            "kling_access_key": str(
                self._settings.value("kling/access_key", "")
                or self._settings.value("kling/api_key", "")
                or os.environ.get("KLING_ACCESS_KEY", "")
                or os.environ.get("KLING_API_KEY", "")
            ),
            "kling_secret_key": str(
                self._settings.value("kling/secret_key", "") or os.environ.get("KLING_SECRET_KEY", "")
            ),
            "kling_api_base_url": str(
                self._settings.value("kling/base_url", "https://api-singapore.klingai.com")
                or "https://api-singapore.klingai.com"
            ),
            "kling_api_model": kling_model,
            "kling_api_mode": str(self._settings.value("kling/mode", "std") or "std"),
            "kling_api_negative_prompt": str(
                self._settings.value("kling/negative_prompt", "low quality, blurry, text, watermark, logo")
                or "low quality, blurry, text, watermark, logo"
            ),
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
            "audio_relpath": self._audio_relpath,
            "srt_relpath": self._srt_relpath,
            "use_voice_in_final": self._check_use_voice.isChecked(),
            "use_subtitles_in_final": self._check_use_subtitles.isChecked(),
            "auto_fit_voice_to_clip": self._check_auto_fit_voice.isChecked(),
            "gemini_tts_split_audio": self._check_gemini_tts_split_audio.isChecked(),
            "final_audio_mode": self._final_audio_mode(),
            "clip_audio_volume_percent": self._spin_clip_audio_volume.value(),
            "narration_audio_volume_percent": self._spin_narration_audio_volume.value(),
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

    def _current_clip_duration_values(self) -> tuple[int, ...]:
        backend = str(self._settings.value("video/backend", VIDEO_BACKEND_VEO) or VIDEO_BACKEND_VEO)
        model = str(self._settings.value("kling/model", "kling-v2-5-turbo") or "kling-v2-5-turbo")
        return _clip_duration_values(video_backend=backend, kling_model=model)

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
                self._edit_scene_clip_seconds.setValue(_default_clip_seconds_for_values(self._current_clip_duration_values()))
                self._label_scene_assets.setText("")
            else:
                video_rel = scene.notes[len("video_relpath:") :] if scene.notes.startswith("video_relpath:") else ""
                selected_clip_seconds = _scene_clip_seconds_for_values(
                    scene,
                    self._clip_seconds_mode(),
                    self._current_clip_duration_values(),
                )
                self._edit_scene_title.setText(f"씬 {scene.scene_id}")
                self._edit_scene_narration.setPlainText(scene.narration_ko)
                self._edit_scene_image_prompt.setPlainText(scene.visual_prompt_ko)
                self._edit_scene_video_prompt.setPlainText(scene.video_prompt_ko)
                self._edit_scene_clip_seconds.setValue(selected_clip_seconds)
                self._label_scene_assets.setText(
                    f"길이: {selected_clip_seconds}초 / "
                    f"LLM 초: {scene.llm_clip_seconds or '(없음)'}\n"
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
        self._btn_scene_select_image.setEnabled(editable)
        self._btn_scene_select_clip.setEnabled(editable)
        self._btn_scene_image.setEnabled(editable)
        self._btn_scene_clip.setEnabled(editable and bool(scene and scene.image_relpath.strip()))

    def _on_scene_editor_changed(self) -> None:
        if self._syncing_scene_editor:
            return
        if self._selected_scene() is not None:
            self._btn_scene_save.setEnabled(True)
            self.stateChanged.emit()

    def _commit_scene_editor_to_model(self) -> bool:
        if self._syncing_scene_editor:
            return False
        scene = self._selected_scene()
        if scene is None:
            return False
        scene.narration_ko = self._edit_scene_narration.toPlainText().strip()
        scene.visual_prompt_ko = self._edit_scene_image_prompt.toPlainText().strip()
        scene.video_prompt_ko = self._edit_scene_video_prompt.toPlainText().strip()
        scene.clip_seconds = _scene_clip_seconds_for_values(
            scene,
            self._clip_seconds_mode(),
            self._current_clip_duration_values(),
        )
        return True

    def _save_selected_scene_edits(self) -> bool:
        if self._selected_scene() is None:
            QMessageBox.information(self, "씬 편집", "편집할 씬을 선택하세요.")
            return False
        self._commit_scene_editor_to_model()
        self._refresh_scene_table()
        self._refresh_buttons()
        self._save_state()
        self.stateChanged.emit()
        return True

    def _on_scene_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._syncing_scene_editor:
            return
        row = item.row()
        col = item.column()
        if row < 0 or row >= len(self._scene_table_rows):
            return
        row_info = self._scene_table_rows[row]
        if row_info.get("type") != "scene":
            return
        scene = row_info.get("scene")
        if not isinstance(scene, Scene):
            return
        text = item.text().strip()
        if col == 1:
            scene.clip_seconds = _normalize_clip_seconds_to_values(text, self._current_clip_duration_values())
            scene.llm_clip_seconds = scene.clip_seconds
        elif col == 2:
            scene.narration_ko = text
        elif col == 3:
            scene.visual_prompt_ko = text
        elif col == 4:
            scene.video_prompt_ko = text
        else:
            return
        self._sync_scene_editor_from_selection()
        self._refresh_length_status()
        self.stateChanged.emit()

    def _select_scene_image_file(self) -> None:
        scene = self._selected_scene()
        parent = self._project_parent()
        if scene is None:
            QMessageBox.information(self, "이미지 선택", "이미지를 지정할 씬을 선택하세요.")
            return
        if parent is None:
            QMessageBox.warning(self, "이미지 선택", "먼저 프로젝트를 저장해주세요.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "씬 이미지 선택",
            str(parent),
            "이미지 (*.png *.jpg *.jpeg *.webp);;모든 파일 (*.*)",
        )
        if not path:
            return
        scene.image_relpath = _relative(Path(path), parent)
        scene.notes = ""
        self._clear_generated_video_outputs()
        self._after_scene_asset_changed()

    def _select_scene_video_file(self) -> None:
        scene = self._selected_scene()
        parent = self._project_parent()
        if scene is None:
            QMessageBox.information(self, "영상 선택", "영상을 지정할 씬을 선택하세요.")
            return
        if parent is None:
            QMessageBox.warning(self, "영상 선택", "먼저 프로젝트를 저장해주세요.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "씬 영상 선택",
            str(parent),
            "영상 (*.mp4 *.mov *.webm *.mkv);;모든 파일 (*.*)",
        )
        if not path:
            return
        scene.notes = f"video_relpath:{_relative(Path(path), parent)}"
        self._clear_generated_video_outputs()
        self._after_scene_asset_changed()

    def _clear_generated_video_outputs(self) -> None:
        self._merged_video_relpath = ""
        self._final_video_relpath = ""

    def _after_scene_asset_changed(self) -> None:
        self._save_state()
        self._refresh_scene_table()
        self._sync_scene_editor_from_selection()
        self._update_preview_from_selection()
        self._refresh_result_table()
        self._refresh_buttons()
        self._refresh_length_status()
        self.stateChanged.emit()

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
            clip_seconds=_default_clip_seconds_for_values(self._current_clip_duration_values()),
            llm_clip_seconds=0,
        )
        self._scenes.insert(insert_at, new_scene)
        self._renumber_scenes()
        self._merged_video_relpath = ""
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

    def _export_scene_table(self) -> None:
        if not self._scene_table_rows:
            QMessageBox.information(self, "테이블 내보내기", "내보낼 씬 테이블이 없습니다.")
            return
        self._commit_scene_editor_to_model()
        self._refresh_scene_table()
        parent = self._project_parent()
        default_dir = parent if parent is not None else Path.cwd()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "씬 테이블 내보내기",
            str(default_dir / "video_production_scenes.csv"),
            "CSV (*.csv);;모든 파일 (*.*)",
        )
        if not path:
            return
        out_path = Path(path)
        if out_path.suffix.lower() != ".csv":
            out_path = out_path.with_suffix(".csv")
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["구분", "씬", "길이", "대본", "이미지 프롬프트", "영상 프롬프트", "이미지", "영상"])
                for row_info in self._scene_table_rows:
                    index = int(row_info.get("index", 0) or 0)
                    writer.writerow(
                        [
                            "씬",
                            str(index),
                            str(row_info.get("clip_seconds", "")),
                            str(row_info.get("narration", "")),
                            str(row_info.get("prompt", "")),
                            str(row_info.get("video_prompt", "")),
                            str(row_info.get("image", "")),
                            str(row_info.get("video", "")),
                        ]
                    )
            self._append_log(f"씬 테이블 내보내기 완료: {out_path}")
        except OSError as e:
            QMessageBox.warning(self, "테이블 내보내기 실패", str(e))

    def _import_scene_table(self) -> None:
        if not self._scenes:
            QMessageBox.information(self, "테이블 가져오기", "대본을 덮어쓸 기존 씬이 없습니다.")
            return
        parent = self._project_parent()
        default_dir = parent if parent is not None else Path.cwd()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "씬 테이블 가져오기",
            str(default_dir),
            "CSV (*.csv);;모든 파일 (*.*)",
        )
        if not path:
            return

        try:
            narrations = self._read_scene_narration_csv(Path(path))
        except (OSError, csv.Error, ValueError) as e:
            QMessageBox.warning(self, "테이블 가져오기 실패", str(e))
            return

        self._commit_scene_editor_to_model()
        updated = 0
        for scene in self._scenes:
            narration = narrations.get(scene.scene_id)
            if narration is None:
                continue
            scene.narration_ko = narration
            updated += 1
        if updated <= 0:
            QMessageBox.warning(self, "테이블 가져오기 실패", "현재 씬 번호와 일치하는 대본 행이 없습니다.")
            return
        self._audio_relpath = ""
        self._srt_relpath = ""
        self._final_video_relpath = ""
        self._save_state()
        self._refresh_scene_table()
        self._refresh_result_table()
        self._refresh_buttons()
        self._refresh_length_status()
        if self._scene_table.rowCount():
            self._scene_table.selectRow(0)
        self._append_log(f"씬 대본 가져오기 완료: {updated}개 행 갱신 - {path}")
        self.stateChanged.emit()

    def _read_scene_narration_csv(self, path: Path) -> dict[int, str]:
        def cell(row: dict[str, str], *names: str) -> str:
            for name in names:
                if name in row:
                    return str(row.get(name, "") or "").strip()
            return ""

        narrations: dict[int, str] = {}
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise ValueError("CSV 헤더가 없습니다.")
            for row in reader:
                scene_text = cell(row, "씬", "scene_id", "scene")
                try:
                    scene_id = int(re.sub(r"\D+", "", scene_text) or "0")
                except ValueError:
                    scene_id = 0
                if scene_id <= 0:
                    scene_id = len(narrations) + 1
                narration = cell(row, "대본", "narration")
                if narration:
                    narrations[scene_id] = narration

        if not narrations:
            raise ValueError("가져올 대본 행이 없습니다.")
        return narrations

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
        if step in (2, 3, 4, 5, 6) and not self._scenes:
            QMessageBox.warning(self, "영상 제작", "먼저 1단계 대본 생성을 완료하세요.")
            return
        if step == 3 and not all(s.image_relpath.strip() for s in self._scenes):
            QMessageBox.warning(self, "이미지 생성", "먼저 2단계 이미지 생성을 완료하세요.")
            return
        if step == 4 and not all(s.notes.startswith("video_relpath:") for s in self._scenes):
            QMessageBox.warning(self, "영상 병합", "먼저 3단계 클립 영상 생성을 완료하세요.")
            return
        if step == 7:
            if not self._merged_video_relpath:
                QMessageBox.warning(self, "최종 영상", "먼저 4단계 편집본 생성을 완료하세요.")
                return
            if self._final_audio_uses_narration() and not self._audio_relpath:
                QMessageBox.warning(self, "최종 영상", "음성을 입히려면 5단계 음성 생성을 완료하거나 음성 입히기를 해제하세요.")
                return
            if self._check_use_subtitles.isChecked() and not self._srt_relpath:
                QMessageBox.warning(self, "최종 영상", "자막을 입히려면 6단계 자막 생성을 완료하거나 자막 입히기를 해제하세요.")
                return
        try:
            args = self._args()
        except RuntimeError as e:
            QMessageBox.warning(self, "영상 제작", str(e))
            return
        if step in (2, 3):
            policy = self._ask_asset_generation_policy(step)
            if not policy:
                return
            args["asset_policy"] = policy
        self._start_worker(step, args)

    def _ask_asset_generation_policy(self, step: int) -> str:
        parent = self._project_parent()
        if parent is None:
            QMessageBox.warning(self, "영상 제작", "먼저 프로젝트를 저장해주세요.")
            return ""
        if step == 2:
            existing_count = sum(1 for scene in self._scenes if _existing_scene_image_relpath(parent, scene))
            title = "이미지 생성"
            label = "이미지"
        elif step == 3:
            existing_count = sum(1 for scene in self._scenes if _existing_scene_video_relpath(parent, scene))
            title = "클립 영상 생성"
            label = "영상 클립"
        else:
            return ASSET_POLICY_REPLACE
        if existing_count <= 0:
            return ASSET_POLICY_REPLACE

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(f"이미 생성된 {label}이 {existing_count}개 있습니다.")
        box.setInformativeText("어떻게 진행할까요?")
        missing_btn = box.addButton("없는 씬만 생성", QMessageBox.ButtonRole.AcceptRole)
        replace_btn = box.addButton("모두 다시 생성", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(missing_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked == missing_btn:
            return ASSET_POLICY_MISSING_ONLY
        if clicked == replace_btn:
            return ASSET_POLICY_REPLACE
        return ""

    def _start_worker(self, step: int, args: dict[str, Any]) -> None:
        self._progress.setValue(0)
        self._worker = VideoProductionWorker(step, args)
        self._worker.log_line.connect(self._append_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.asset_ready.connect(self._on_asset_ready)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self._set_busy(True)
        self._worker.start()

    def _on_progress(self, cur: int, total: int) -> None:
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(max(0, min(cur, max(1, total))))

    def _on_asset_ready(self, step: int, scene_id: int, relpath: str) -> None:
        for scene in self._scenes:
            if scene.scene_id != int(scene_id):
                continue
            if step == 2:
                scene.image_relpath = str(relpath)
                scene.notes = ""
                self._clear_generated_video_outputs()
            elif step == 3:
                scene.notes = f"video_relpath:{relpath}"
                self._clear_generated_video_outputs()
            break
        self._save_state()
        self._refresh_scene_table()
        self._refresh_result_table()
        self._refresh_buttons()
        self._refresh_length_status()
        self._update_preview_from_selection()
        self.stateChanged.emit()

    def _asset_result_items(self, result: object) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        if isinstance(result, dict):
            generated = [(int(sid), str(rel)) for sid, rel in result.get("generated", [])]
            skipped = [(int(sid), str(rel)) for sid, rel in result.get("skipped", [])]
            return generated, skipped
        if isinstance(result, list):
            return [(int(sid), str(rel)) for sid, rel in result], []
        return [], []

    def _on_succeeded(self, step: int, result: object) -> None:
        if step == 9:
            self._reference_image_relpath = str(result)
            self._reference_image.setText(self._reference_image_relpath)
            self._append_log(f"참조 이미지 생성 완료: {self._reference_image_relpath}")
        elif step == 1:
            if isinstance(result, dict):
                self._scenes = list(result.get("scenes", []))
                generated_style = str(result.get("visual_style_prompt", "") or "").strip()
                if generated_style:
                    self._style_prompt.setPlainText(generated_style)
                    self._append_log(f"공통 이미지 스타일 생성: {generated_style}")
                generated_reference = str(result.get("reference_image_prompt", "") or "").strip()
                if generated_reference:
                    self._reference_prompt.setPlainText(generated_reference)
                    self._append_log(f"참조 이미지 프롬프트 생성: {generated_reference}")
                timeline_warning = str(result.get("timeline_warning", "") or "").strip()
                if timeline_warning:
                    self._append_log(f"대본 타임라인 경고: {timeline_warning}")
                    QMessageBox.warning(
                        self,
                        "대본 타임라인 경고",
                        "생성된 대본이 목표 길이와 다를 수 있습니다.\n\n"
                        f"{timeline_warning}\n\n"
                        "씬별 편집에서 대본을 조정할 수 있습니다.",
                    )
            else:
                self._scenes = list(result) if isinstance(result, list) else []
            self._merged_video_relpath = ""
            self._audio_relpath = ""
            self._srt_relpath = ""
            self._final_video_relpath = ""
            self._append_log(f"대본 생성 완료: {len(self._scenes)}개 씬")
        elif step == 2:
            if self._worker is not None:
                reference_rel = str(self._worker._args.get("reference_image_relpath", "") or "").strip()
                if reference_rel and reference_rel != self._reference_image.text().strip():
                    self._reference_image_relpath = reference_rel
                    self._reference_image.setText(reference_rel)
            generated, skipped = self._asset_result_items(result)
            for sid, rel in generated:
                for scene in self._scenes:
                    if scene.scene_id == int(sid):
                        scene.image_relpath = str(rel)
                        scene.notes = ""
            for sid, rel in skipped:
                for scene in self._scenes:
                    if scene.scene_id == int(sid) and not scene.image_relpath.strip():
                        scene.image_relpath = str(rel)
            if generated:
                self._merged_video_relpath = ""
                self._final_video_relpath = ""
            self._append_log(f"이미지 생성 완료: 생성 {len(generated)}개, 스킵 {len(skipped)}개")
        elif step == 3:
            generated, skipped = self._asset_result_items(result)
            if generated:
                self._merged_video_relpath = ""
                self._final_video_relpath = ""
            for sid, rel in [*generated, *skipped]:
                for scene in self._scenes:
                    if scene.scene_id == int(sid):
                        scene.notes = f"video_relpath:{rel}"
            self._append_log(f"클립 영상 생성 완료: 생성 {len(generated)}개, 스킵 {len(skipped)}개")
        elif step == 4:
            self._merged_video_relpath = str(result)
            self._append_log(f"편집본: {self._merged_video_relpath}")
        elif step == 5:
            self._audio_relpath = str(result)
            self._append_log(f"음성: {self._audio_relpath}")
        elif step == 6:
            self._srt_relpath = str(result)
            self._append_log(f"자막: {self._srt_relpath}")
        elif step == 7:
            self._final_video_relpath = str(result)
            self._append_log(f"최종 영상: {self._final_video_relpath}")
        self._save_state()
        self._refresh_scene_table()
        self._refresh_result_table()
        self._refresh_buttons()
        self._refresh_length_status()
        if step == 4:
            self._select_result_row("merged")
        elif step == 7:
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
            self._btn_scene_import,
            self._btn_scene_export,
            self._btn_scene_select_image,
            self._btn_scene_select_clip,
            self._btn_scene_image,
            self._btn_scene_clip,
            self._btn_browse_reference,
            self._btn_generate_reference,
            self._btn_preview_reference,
        ):
            btn.setEnabled(not busy)
        self._reference_prompt.setEnabled(not busy)
        self._combo_script_input_mode.setEnabled(not busy)
        self._combo_final_audio_mode.setEnabled(not busy)
        self._spin_clip_audio_volume.setEnabled(not busy)
        self._spin_narration_audio_volume.setEnabled(not busy)
        self._check_auto_fit_voice.setEnabled(not busy)
        self._check_gemini_tts_split_audio.setEnabled(not busy)
        self._check_use_voice.setEnabled(not busy)
        self._check_use_subtitles.setEnabled(not busy)
        self._reference_image.setEnabled(not busy)
        if not busy:
            self._refresh_buttons()
            self._sync_scene_editor_from_selection()

    def _refresh_buttons(self) -> None:
        has_script = bool(self._scenes)
        has_images = has_script and all(s.image_relpath.strip() for s in self._scenes)
        has_clips = has_script and all(s.notes.startswith("video_relpath:") for s in self._scenes)
        has_merged = bool(self._merged_video_relpath)
        has_audio = bool(self._audio_relpath)
        needs_voice = self._final_audio_uses_narration()
        needs_subtitles = self._check_use_subtitles.isChecked()
        has_required_voice = (not needs_voice) or has_audio
        has_required_subtitles = (not needs_subtitles) or bool(self._srt_relpath)
        can_final = has_merged and has_required_voice and has_required_subtitles
        enabled = [True, has_script, has_images, has_clips, has_script, has_script, can_final]
        for btn, on in zip(self._buttons, enabled):
            btn.setEnabled(on)

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
                stats = _timeline_stats(
                    self._scenes,
                    target_minutes=self._spin_minutes.value(),
                    clip_seconds_mode=self._clip_seconds_mode(),
                )
                self._label_length_status.setText(
                    f"계획 길이: 목표 {stats['target']:.0f}s / 클립 합계 {stats['clip_total']:.0f}s / "
                    f"예상 음성 {stats['voice_estimate']:.1f}s"
                )
            else:
                self._label_length_status.setText("영상/음성 길이: 아직 계산되지 않았습니다.")
            return
        deficit = audio_sec - video_sec
        if deficit > 3.0:
            suffix = f"부족 {deficit:.2f}s"
        elif deficit > 0:
            suffix = f"짧은 부족 {deficit:.2f}s(최종 합성에서 보정)"
        else:
            suffix = "부족 없음"
        self._label_length_status.setText(
            f"영상 {video_sec:.2f}s / 음성 {audio_sec:.2f}s / {suffix}"
        )

    def _refresh_result_table(self) -> None:
        selected_kind = ""
        current_row = self._result_table.currentRow()
        if 0 <= current_row < len(self._result_table_rows):
            selected_kind = self._result_table_rows[current_row].get("kind", "")

        parent = self._project_parent()
        rows: list[dict[str, str]] = []
        if self._merged_video_relpath:
            rows.append({"kind": "merged", "label": "편집본", "relpath": self._merged_video_relpath})
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
        self._scene_table.blockSignals(True)
        self._scene_table_rows = []
        self._scene_table.setRowCount(0)
        clip_values = self._current_clip_duration_values()
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
                    "clip_seconds": str(_scene_clip_seconds_for_values(scene, self._clip_seconds_mode(), clip_values)),
                    "narration": scene.narration_ko,
                }
            )
            row = self._scene_table.rowCount()
            self._scene_table.insertRow(row)
            values = [
                str(scene.scene_id),
                str(_scene_clip_seconds_for_values(scene, self._clip_seconds_mode(), clip_values)),
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
        self._scene_table.resizeColumnsToContents()
        self._scene_table.blockSignals(False)
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
            return
        row_info = self._scene_table_rows[row]
        image_rel = str(row_info.get("image", ""))
        video_rel = str(row_info.get("video", ""))
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
        if _media_duration(video_path) <= 0:
            self._clear_clip_preview()
            self._label_clip_path.setText(f"읽을 수 없는 영상 파일입니다: {label}")
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
