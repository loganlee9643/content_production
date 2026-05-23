from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal

from app.workers.cancellable_thread import CancellableQThread, WorkerCancelled

from app.models.storyboard import Scene
from app.services.piper_tts import (
    PiperTtsError,
    espeak_voice_from_config,
    onnx_filename_language_red_flags,
    piper_config_path_for_model,
    synthesize_wav,
    voice_config_log_lines,
)


def _text_has_hangul(text: str) -> bool:
    return any("\uac00" <= c <= "\ud7a3" for c in text)


def _espeak_voice_sounds_non_korean(voice: str) -> bool:
    v = (voice or "").strip().lower()
    if not v:
        return False
    if v.startswith("ko"):
        return False
    if v.startswith(("zh", "cmn", "yue", "mand")):
        return True
    if v.startswith("ja"):
        return True
    if v.startswith("en"):
        return True
    return False


class PiperTtsWorker(CancellableQThread):
    """씬별 나레이션 → Piper → WAV. 프로젝트 디렉터리 기준 상대 경로를 씬에 기록."""

    log_line = Signal(str)
    progress = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        scenes: list[Scene],
        project_parent: Path,
        piper_executable: Path,
        model_path: Path,
        only_scene_ids: frozenset[int] | None = None,
    ) -> None:
        super().__init__()
        self._scenes = scenes
        self._project_parent = project_parent
        self._piper_executable = piper_executable
        self._model_path = model_path
        self._only_scene_ids = only_scene_ids

    def run(self) -> None:
        try:
            self.check_cancelled()
            audio_dir = self._project_parent / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            todo = list(self._scenes)
            if self._only_scene_ids is not None:
                todo = [s for s in todo if s.scene_id in self._only_scene_ids]
            total = len(todo)
            pairs: list[tuple[int, str]] = []

            for line in voice_config_log_lines(self._model_path):
                self.log_line.emit(line)
            for line in onnx_filename_language_red_flags(self._model_path):
                self.log_line.emit(line)

            for i, scene in enumerate(todo):
                self.check_cancelled()
                self.progress.emit(i + 1, max(1, total))
                sid = scene.scene_id
                fname = f"scene_{sid:03d}.wav"
                wav_abs = audio_dir / fname
                rel = f"audio/{fname}"

                if not scene.narration_ko.strip():
                    self.log_line.emit(f"씬 {sid}: 나레이션 없음 — 건너뜀")
                    pairs.append((sid, ""))
                    if wav_abs.exists():
                        try:
                            wav_abs.unlink()
                        except OSError:
                            pass
                    continue

                self.log_line.emit(f"씬 {sid}: Piper 실행 중… ({len(scene.narration_ko)}자)")
                cfg = piper_config_path_for_model(self._model_path)
                if cfg is None:
                    self.log_line.emit(
                        f"씬 {sid}: 경고 — ONNX 옆에 '{self._model_path.name}.json' 이 없습니다. "
                        "한국어 음성은 보통 `ko_KR-…-medium.onnx` + 같은 이름의 `.onnx.json` 세트로 받습니다."
                    )
                else:
                    voice = espeak_voice_from_config(cfg)
                    if voice and _espeak_voice_sounds_non_korean(voice) and _text_has_hangul(scene.narration_ko):
                        self.log_line.emit(
                            f"씬 {sid}: 경고 — JSON에서 읽은 음성/언어 코드가 '{voice}' 입니다. "
                            "한국어는 `ko_KR-…-medium.onnx` + `.onnx.json` 세트(예: rhasspy/piper-voices)를 쓰세요."
                        )
                try:
                    synthesize_wav(
                        scene.narration_ko,
                        wav_abs,
                        piper_executable=self._piper_executable,
                        model_path=self._model_path,
                    )
                except PiperTtsError as e:
                    self.failed.emit(str(e))
                    return

                self.log_line.emit(f"씬 {sid}: 저장됨 {rel}")
                pairs.append((sid, rel))

            self.succeeded.emit(pairs)
        except WorkerCancelled:
            self.failed.emit("작업이 중지되었습니다.")
