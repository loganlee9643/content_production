from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any


class VeoVideoApiError(RuntimeError):
    pass


def _import_genai() -> tuple[Any, Any]:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise VeoVideoApiError("google-genai is required for Veo video generation.") from e
    return genai, types


def generate_video_from_image(
    *,
    api_key: str = "",
    model: str = "veo-3.1-generate-preview",
    prompt: str,
    image_path: Path,
    out_video_path: Path,
    resolution: str = "720p",
    aspect_ratio: str = "16:9",
    duration_seconds: int = 8,
    poll_interval_sec: float = 10.0,
    timeout_sec: float = 1800.0,
) -> Path:
    if not image_path.is_file():
        raise VeoVideoApiError(f"Image file does not exist: {image_path}")
    genai, types = _import_genai()
    key = (api_key or os.environ.get("GEMINI_API_KEY", "") or "").strip()
    client = genai.Client(api_key=key) if key else genai.Client()
    try:
        operation = client.models.generate_videos(
            model=(model or "veo-3.1-generate-preview").strip(),
            prompt=(prompt or "").strip(),
            image=types.Image.from_file(location=str(image_path.resolve())),
            config=types.GenerateVideosConfig(
                number_of_videos=1,
                resolution=(resolution or "720p").strip(),
                duration_seconds=int(duration_seconds),
                aspect_ratio=aspect_ratio or "16:9",
            ),
        )
    except Exception as e:
        raise VeoVideoApiError(f"Veo generation request failed: {e}") from e

    deadline = time.monotonic() + max(30.0, float(timeout_sec))
    while not getattr(operation, "done", False):
        if time.monotonic() > deadline:
            raise VeoVideoApiError("Veo generation timed out.")
        time.sleep(max(1.0, float(poll_interval_sec)))
        try:
            operation = client.operations.get(operation)
        except Exception as e:
            raise VeoVideoApiError(f"Veo operation polling failed: {e}") from e

    videos = getattr(getattr(operation, "response", None), "generated_videos", None)
    if not videos:
        raise VeoVideoApiError("Veo response did not include a generated video.")
    out_video_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.files.download(file=videos[0].video)
        videos[0].video.save(str(out_video_path.resolve()))
    except Exception as e:
        raise VeoVideoApiError(f"Veo video download failed: {e}") from e
    return out_video_path
