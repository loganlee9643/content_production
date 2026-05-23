from __future__ import annotations

import json
import mimetypes
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ComfyUIWanVideoError(RuntimeError):
    pass


def _base_url(url: str) -> str:
    value = (url or "http://127.0.0.1:8188").strip().rstrip("/")
    return value or "http://127.0.0.1:8188"


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise ComfyUIWanVideoError(f"ComfyUI API error {e.code}: {detail}") from e
    except URLError as e:
        raise ComfyUIWanVideoError(f"ComfyUI is not reachable: {e}") from e
    return json.loads(raw.decode("utf-8")) if raw else {}


def _upload_image(base_url: str, image_path: Path) -> str:
    if not image_path.is_file():
        raise ComfyUIWanVideoError(f"Image file does not exist: {image_path}")
    boundary = f"----ContentProduction{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
    parts = [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{image_path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8"),
        image_path.read_bytes(),
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode("utf-8"),
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    req = Request(
        f"{base_url}/upload/image",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=120.0) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise ComfyUIWanVideoError(f"ComfyUI image upload failed {e.code}: {detail}") from e
    except URLError as e:
        raise ComfyUIWanVideoError(f"ComfyUI image upload failed: {e}") from e
    name = str(result.get("name", "")).strip()
    subfolder = str(result.get("subfolder", "")).strip().replace("\\", "/")
    if not name:
        raise ComfyUIWanVideoError("ComfyUI image upload did not return a filename.")
    return f"{subfolder}/{name}" if subfolder else name


def _duration_for_comfy(seconds: int) -> int:
    if int(seconds) <= 5:
        return 5
    if int(seconds) <= 10:
        return 10
    return 15


def _resolution_for_wan_model(model: str, resolution: str) -> str:
    normalized = (resolution or "720P").strip().upper()
    if (model or "").strip().lower() == "wan2.6-i2v" and normalized == "480P":
        return "720P"
    return normalized or "720P"


def _build_prompt_graph(
    *,
    uploaded_image: str,
    prompt: str,
    filename_prefix: str,
    model: str,
    resolution: str,
    duration_seconds: int,
    seed: int,
    negative_prompt: str,
    prompt_extend: bool,
    watermark: bool,
) -> dict[str, Any]:
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": uploaded_image}},
        "2": {
            "class_type": "WanImageToVideoApi",
            "inputs": {
                "model": model or "wan2.6-i2v",
                "image": ["1", 0],
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "resolution": _resolution_for_wan_model(model, resolution),
                "duration": _duration_for_comfy(duration_seconds),
                "seed": max(0, int(seed)),
                "generate_audio": False,
                "prompt_extend": bool(prompt_extend),
                "watermark": bool(watermark),
                "shot_type": "single",
            },
        },
        "3": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["2", 0],
                "filename_prefix": filename_prefix,
                "format": "mp4",
                "codec": "h264",
            },
        },
    }


def _load_workflow_graph(
    *,
    workflow_path: Path,
    uploaded_image: str,
    prompt: str,
    filename_prefix: str,
    seed: int,
    negative_prompt: str,
) -> dict[str, Any]:
    try:
        raw = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ComfyUIWanVideoError(f"ComfyUI workflow JSON could not be loaded: {workflow_path} ({e})") from e
    graph = raw.get("prompt", raw) if isinstance(raw, dict) else raw
    if not isinstance(graph, dict):
        raise ComfyUIWanVideoError("ComfyUI workflow JSON must be API-format node graph.")

    patched_load_image = False
    patched_prompt = False
    patched_output = False
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type", ""))
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue

        if class_type in ("LoadImage", "LoadImageOutput") and "image" in inputs:
            inputs["image"] = uploaded_image
            patched_load_image = True

        for key in ("prompt", "positive_prompt"):
            if key in inputs and isinstance(inputs[key], str):
                inputs[key] = prompt
                patched_prompt = True
        if "negative_prompt" in inputs and isinstance(inputs["negative_prompt"], str):
            inputs["negative_prompt"] = negative_prompt
        if class_type in ("CLIPTextEncode", "TextEncodeHunyuanVideo_ImageToVideo") and isinstance(inputs.get("text"), str):
            text = str(inputs.get("text") or "")
            if "negative" in class_type.lower() or "low quality" in text.lower() or "blurry" in text.lower():
                inputs["text"] = negative_prompt or text
            else:
                inputs["text"] = prompt
                patched_prompt = True

        for key in ("seed", "noise_seed"):
            if key in inputs:
                inputs[key] = max(0, int(seed))

        if "filename_prefix" in inputs:
            inputs["filename_prefix"] = filename_prefix
            patched_output = True

    if not patched_load_image:
        raise ComfyUIWanVideoError("Workflow JSON did not contain a LoadImage node to patch.")
    if not patched_prompt:
        raise ComfyUIWanVideoError("Workflow JSON did not contain a prompt/text input to patch.")
    if not patched_output:
        raise ComfyUIWanVideoError("Workflow JSON did not contain a filename_prefix output node to patch.")
    return graph


def _find_video_ref(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict):
        for key in ("videos", "gifs", "images"):
            items = value.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith((".mp4", ".webm", ".mov")):
                        return {
                            "filename": str(item.get("filename", "")),
                            "subfolder": str(item.get("subfolder", "")),
                            "type": str(item.get("type", "output") or "output"),
                        }
        for child in value.values():
            found = _find_video_ref(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_video_ref(child)
            if found is not None:
                return found
    return None


def _download_output(base_url: str, ref: dict[str, str], out_video_path: Path) -> Path:
    query = urlencode({"filename": ref["filename"], "subfolder": ref.get("subfolder", ""), "type": ref.get("type", "output")})
    req = Request(f"{base_url}/view?{query}", method="GET")
    try:
        with urlopen(req, timeout=120.0) as resp:
            out_video_path.parent.mkdir(parents=True, exist_ok=True)
            with out_video_path.open("wb") as f:
                shutil.copyfileobj(resp, f)
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise ComfyUIWanVideoError(f"ComfyUI video download failed {e.code}: {detail}") from e
    except URLError as e:
        raise ComfyUIWanVideoError(f"ComfyUI video download failed: {e}") from e
    return out_video_path


def generate_video_from_image_comfyui_wan(
    *,
    base_url: str = "http://127.0.0.1:8188",
    prompt: str,
    image_path: Path,
    out_video_path: Path,
    model: str = "wan2.6-i2v",
    resolution: str = "720P",
    duration_seconds: int = 5,
    seed: int = 0,
    negative_prompt: str = "",
    prompt_extend: bool = True,
    watermark: bool = False,
    workflow_path: str = "",
    poll_interval_sec: float = 5.0,
    timeout_sec: float = 3600.0,
) -> Path:
    base = _base_url(base_url)
    uploaded = _upload_image(base, image_path)
    filename_prefix = f"content_production/{out_video_path.stem}_{uuid.uuid4().hex[:8]}"
    workflow = Path(workflow_path).expanduser() if workflow_path.strip() else None
    if workflow is not None:
        graph = _load_workflow_graph(
            workflow_path=workflow,
            uploaded_image=uploaded,
            prompt=prompt,
            filename_prefix=filename_prefix,
            seed=seed,
            negative_prompt=negative_prompt,
        )
    else:
        graph = _build_prompt_graph(
            uploaded_image=uploaded,
            prompt=prompt,
            filename_prefix=filename_prefix,
            model=model,
            resolution=resolution,
            duration_seconds=duration_seconds,
            seed=seed,
            negative_prompt=negative_prompt,
            prompt_extend=prompt_extend,
            watermark=watermark,
        )
    queued = _request_json("POST", f"{base}/prompt", {"prompt": graph, "client_id": str(uuid.uuid4())}, timeout=30.0)
    prompt_id = str(queued.get("prompt_id", "")).strip()
    if not prompt_id:
        raise ComfyUIWanVideoError(f"ComfyUI did not return prompt_id: {queued}")
    deadline = time.monotonic() + max(30.0, float(timeout_sec))
    last_status = ""
    while time.monotonic() <= deadline:
        hist_all = _request_json("GET", f"{base}/history/{prompt_id}", timeout=30.0)
        hist = hist_all.get(prompt_id, hist_all) if isinstance(hist_all, dict) else {}
        status = hist.get("status") if isinstance(hist, dict) else None
        if isinstance(status, dict):
            last_status = str(status.get("status_str", "") or status)
            if last_status.lower() in ("error", "failed"):
                status_text = json.dumps(status, ensure_ascii=False)
                if "Please login first" in status_text or "Unauthorized" in status_text:
                    raise ComfyUIWanVideoError(
                        "ComfyUI Wan API node requires login. Export a local Wan workflow as API JSON and set it "
                        "in Settings > Video Production > ComfyUI workflow JSON, or log in to ComfyUI."
                    )
                raise ComfyUIWanVideoError(f"ComfyUI generation failed: {status}")
            if status.get("completed"):
                ref = _find_video_ref(hist.get("outputs", hist))
                if ref is None:
                    raise ComfyUIWanVideoError("ComfyUI completed, but no generated video was found in history.")
                return _download_output(base, ref, out_video_path)
        time.sleep(max(1.0, float(poll_interval_sec)))
    raise ComfyUIWanVideoError(f"ComfyUI generation timed out. Last status: {last_status or 'unknown'}")
