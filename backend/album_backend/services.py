from __future__ import annotations

import array
import asyncio
import base64
import html
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
from PIL import ImageChops, ImageStat

from album_backend import db, schemas
from auth_capture import (
    SunoAuthError,
    capture_auth_with_browser,
    save_auth,
    validate_auth,
)
from cookie import suno_auth, update_token
from utils import (
    SunoAPIError,
    SunoGenerationVerificationError,
    generate_music,
    get_feed,
)


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parents[1]
VIDEO_ICON_DIR = Path(os.getenv("VIDEO_ICON_DIR", ROOT / "images")).resolve()
VIDEO_ICON_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
DEFAULT_SUNO_MODEL = os.getenv("SUNO_MODEL", "chirp-fenix")
SUNO_AUTH_FILE = ROOT / ".auth" / "suno-auth.json"
SUNO_PROFILE_DIR = ROOT / ".auth" / "native-browser-profile"
SUNO_LOGIN_TIMEOUT_SECONDS = float(os.getenv("SUNO_LOGIN_TIMEOUT_SECONDS", "900"))
SUNO_MAX_TITLE_CHARS = 80
SUNO_MAX_STYLE_CHARS = 200
SUNO_MAX_NEGATIVE_STYLE_CHARS = 1000
SUNO_MAX_CUSTOM_PROMPT_CHARS = 5000
logger = logging.getLogger("album_backend.services")
_suno_reauth_lock = asyncio.Lock()
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image-preview"

BUNDLED_FONT_DIR = ROOT.parent / "frontend" / "public" / "fonts"
BUNDLED_VIDEO_FONTS = {
    "malgun": BUNDLED_FONT_DIR / "NotoSansKR.ttf",
    "noto_sans_kr": BUNDLED_FONT_DIR / "NotoSansKR.ttf",
    "noto_serif_kr": BUNDLED_FONT_DIR / "NotoSerifKR.ttf",
    "nanum_gothic": BUNDLED_FONT_DIR / "NanumGothic-Regular.ttf",
    "nanum_pen": BUNDLED_FONT_DIR / "NanumPenScript-Regular.ttf",
    "han_dotum": BUNDLED_FONT_DIR / "NotoSansKR.ttf",
    "han_batang": BUNDLED_FONT_DIR / "NotoSerifKR.ttf",
    "batang": BUNDLED_FONT_DIR / "NotoSerifKR.ttf",
    "arial": BUNDLED_FONT_DIR / "Roboto.ttf",
    "roboto": BUNDLED_FONT_DIR / "Roboto.ttf",
    "bebas": BUNDLED_FONT_DIR / "BebasNeue-Regular.ttf",
    "anton": BUNDLED_FONT_DIR / "Anton-Regular.ttf",
    "cinzel": BUNDLED_FONT_DIR / "Cinzel.ttf",
    "georgia": BUNDLED_FONT_DIR / "NotoSerifKR.ttf",
    "impact": BUNDLED_FONT_DIR / "Anton-Regular.ttf",
    "consolas": BUNDLED_FONT_DIR / "Roboto.ttf",
    "black_han_sans": BUNDLED_FONT_DIR / "BlackHanSans-Regular.ttf",
    "do_hyeon": BUNDLED_FONT_DIR / "DoHyeon-Regular.ttf",
    "jua": BUNDLED_FONT_DIR / "Jua-Regular.ttf",
    "gowun_dodum": BUNDLED_FONT_DIR / "GowunDodum-Regular.ttf",
    "gowun_batang": BUNDLED_FONT_DIR / "GowunBatang-Regular.ttf",
    "song_myung": BUNDLED_FONT_DIR / "SongMyung-Regular.ttf",
    "poor_story": BUNDLED_FONT_DIR / "PoorStory-Regular.ttf",
    "gaegu": BUNDLED_FONT_DIR / "Gaegu-Regular.ttf",
    "single_day": BUNDLED_FONT_DIR / "SingleDay-Regular.ttf",
    "montserrat": BUNDLED_FONT_DIR / "Montserrat.ttf",
    "oswald": BUNDLED_FONT_DIR / "Oswald.ttf",
    "playfair": BUNDLED_FONT_DIR / "PlayfairDisplay.ttf",
}
HANGUL_VIDEO_FONT_KEYS = {
    "malgun",
    "noto_sans_kr",
    "noto_serif_kr",
    "nanum_gothic",
    "nanum_pen",
    "han_dotum",
    "han_batang",
    "batang",
    "georgia",
    "black_han_sans",
    "do_hyeon",
    "jua",
    "gowun_dodum",
    "gowun_batang",
    "song_myung",
    "poor_story",
    "gaegu",
    "single_day",
    "windows_malgun",
    "windows_gulim",
    "windows_dotum",
    "windows_batang",
    "windows_gungsuh",
}


def _video_font_path(font_family: str = "malgun") -> Path | None:
    configured = os.getenv("VIDEO_FONT_PATH")
    windows_system_fonts = {
        "windows_malgun": Path(r"C:\Windows\Fonts\malgun.ttf"),
        "windows_gulim": Path(r"C:\Windows\Fonts\gulim.ttc"),
        "windows_dotum": Path(r"C:\Windows\Fonts\gulim.ttc"),
        "windows_batang": Path(r"C:\Windows\Fonts\batang.ttc"),
        "windows_gungsuh": Path(r"C:\Windows\Fonts\batang.ttc"),
        "windows_arial": Path(r"C:\Windows\Fonts\arial.ttf"),
        "windows_georgia": Path(r"C:\Windows\Fonts\georgia.ttf"),
        "windows_impact": Path(r"C:\Windows\Fonts\impact.ttf"),
        "windows_consolas": Path(r"C:\Windows\Fonts\consola.ttf"),
    }
    windows_fonts = {
        "malgun": Path(r"C:\Windows\Fonts\malgun.ttf"),
        "noto_sans_kr": Path(r"C:\Windows\Fonts\NotoSansKR-Regular.ttf"),
        "noto_serif_kr": Path(r"C:\Windows\Fonts\NotoSerifKR-VF.ttf"),
        "nanum_gothic": Path(r"C:\Windows\Fonts\NanumGothic.ttf"),
        "nanum_pen": Path(r"C:\Windows\Fonts\NanumPen.ttf"),
        "han_dotum": Path(r"C:\Windows\Fonts\HANDotum.ttf"),
        "han_batang": Path(r"C:\Windows\Fonts\HANBatang.ttf"),
        "batang": Path(r"C:\Windows\Fonts\batang.ttc"),
        "arial": Path(r"C:\Windows\Fonts\arial.ttf"),
        "roboto": Path(r"C:\Windows\Fonts\Roboto-Regular.ttf"),
        "bebas": Path(r"C:\Windows\Fonts\BebasNeue-Regular.ttf"),
        "anton": Path(r"C:\Windows\Fonts\Anton-Regular.ttf"),
        "cinzel": Path(r"C:\Windows\Fonts\Cinzel-Regular.ttf"),
        "georgia": Path(r"C:\Windows\Fonts\georgia.ttf"),
        "impact": Path(r"C:\Windows\Fonts\impact.ttf"),
        "consolas": Path(r"C:\Windows\Fonts\consola.ttf"),
    }
    candidates = [
        Path(configured) if configured else None,
        windows_system_fonts.get(font_family),
        BUNDLED_VIDEO_FONTS.get(font_family),
        windows_fonts.get(font_family),
        Path(r"C:\Windows\Fonts\malgun.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    return next((path for path in candidates if path and path.is_file()), None)


def _contains_hangul(value: str) -> bool:
    return any(
        "\u1100" <= char <= "\u11ff"
        or "\u3130" <= char <= "\u318f"
        or "\uac00" <= char <= "\ud7af"
        for char in value
    )


def _korean_video_font_path() -> Path | None:
    configured = os.getenv("VIDEO_KOREAN_FONT_PATH")
    candidates = [
        Path(configured) if configured else None,
        BUNDLED_VIDEO_FONTS["noto_sans_kr"],
        Path(r"C:\Windows\Fonts\malgun.ttf"),
        Path(r"C:\Windows\Fonts\malgunbd.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf"),
    ]
    return next((path for path in candidates if path and path.is_file()), None)


def _video_text_font_path(font_family: str, value: str) -> Path | None:
    if _contains_hangul(value) and font_family not in HANGUL_VIDEO_FONT_KEYS:
        korean_font = _korean_video_font_path()
        if korean_font:
            return korean_font
    return _video_font_path(font_family)


def _ffmpeg_filter_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:")


def _ffmpeg_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _percent_position(value: Any, default: float) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return default


def resolve_video_icon(filename: str) -> Path | None:
    if not filename or Path(filename).name != filename:
        return None
    candidate = (VIDEO_ICON_DIR / filename).resolve()
    if (
        candidate.parent != VIDEO_ICON_DIR
        or candidate.suffix.lower() not in VIDEO_ICON_EXTENSIONS
        or not candidate.is_file()
    ):
        return None
    return candidate


def list_video_icons() -> list[dict[str, str]]:
    if not VIDEO_ICON_DIR.is_dir():
        return []
    return [
        {
            "filename": path.name,
            "label": path.stem.replace("-", " ").replace("_", " "),
        }
        for path in sorted(
            VIDEO_ICON_DIR.iterdir(),
            key=lambda item: item.name.casefold(),
        )
        if path.is_file() and path.suffix.lower() in VIDEO_ICON_EXTENSIONS
    ]


def _browser_executable() -> Path | None:
    configured = os.getenv("VIDEO_SVG_BROWSER")
    candidates = [
        Path(configured) if configured else None,
        Path(shutil.which("msedge") or ""),
        Path(shutil.which("chrome") or ""),
        Path(shutil.which("chromium") or ""),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ]
    return next((path for path in candidates if path and path.is_file()), None)


def rasterize_svg_icon(source: Path, destination: Path, size: int) -> Path:
    browser = _browser_executable()
    if not browser:
        raise RuntimeError(
            "SVG video icons require Edge, Chrome, Chromium, or VIDEO_SVG_BROWSER."
        )
    render_size = max(256, min(2048, size * 4))
    profile_dir = destination.parent / f".svg-browser-{destination.stem}"
    shutil.rmtree(profile_dir, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--disable-background-networking",
        "--default-background-color=00000000",
        f"--user-data-dir={profile_dir}",
        f"--window-size={render_size},{render_size}",
        f"--screenshot={destination}",
        source.resolve().as_uri(),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        deadline = time.monotonic() + 10
        while not destination.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    if completed.returncode != 0 or not destination.is_file():
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(
            f"SVG video icon conversion failed: {stderr[-800:] or 'no output'}"
        )
    return destination


def _load_video_font(
    font_family: str, size: int, weight: int = 400
) -> ImageFont.FreeTypeFont:
    font_path = _video_font_path(font_family)
    if not font_path:
        raise RuntimeError(f"Video font was not found: {font_family}")
    font = ImageFont.truetype(str(font_path), size=size)
    try:
        axes = font.get_variation_axes()
        if axes:
            values = [
                max(axis["minimum"], min(axis["maximum"], weight))
                if axis.get("name") == b"Weight"
                else axis["default"]
                for axis in axes
            ]
            font.set_variation_by_axes(values)
    except (AttributeError, OSError, KeyError, TypeError):
        pass
    return font


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    letter_spacing: float,
) -> float:
    characters = list(text)
    return sum(
        draw.textlength(character, font=font)
        + (letter_spacing if index < len(characters) - 1 else 0)
        for index, character in enumerate(characters)
    )


def _draw_spaced_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
    letter_spacing: float,
    *,
    centered: bool,
) -> None:
    x, y = position
    if centered:
        x -= _text_width(draw, text, font, letter_spacing) / 2
    bbox = draw.textbbox((0, 0), text or " ", font=font)
    baseline_y = y - (bbox[3] - bbox[1]) / 2 - bbox[1]
    for index, character in enumerate(text):
        draw.text(
            (x, baseline_y),
            character,
            font=font,
            fill=fill,
            stroke_width=2,
            stroke_fill=(0, 0, 0, 150),
        )
        x += draw.textlength(character, font=font)
        if index < len(text) - 1:
            x += letter_spacing


def render_static_video_frame(
    source: Path,
    destination: Path,
    compose: dict[str, Any],
) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"Video frame source image was not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    width, height = 1920, 1080

    with Image.open(source) as opened:
        frame = ImageOps.fit(
            opened.convert("RGB"),
            (width, height),
            method=Image.Resampling.LANCZOS,
        )
    frame = ImageEnhance.Brightness(frame).enhance(
        max(0, 1 + float(compose.get("brightness", 0) or 0) / 100)
    )
    frame = ImageEnhance.Contrast(frame).enhance(
        max(0, 1 + float(compose.get("contrast", 0) or 0) / 100)
    )
    frame = ImageEnhance.Color(frame).enhance(
        max(0, 1 + float(compose.get("saturation", 0) or 0) / 100)
    )
    blur = max(0, float(compose.get("blur", 0) or 0))
    if blur:
        frame = frame.filter(ImageFilter.GaussianBlur(radius=min(20, blur)))
    frame = frame.convert("RGBA")

    overlay_opacity = max(
        0,
        min(1, float(compose.get("overlay_opacity", 0) or 0)),
    )
    if overlay_opacity:
        overlay_rgb = ImageColor.getrgb(
            str(compose.get("overlay_color") or "#21000f")
        )
        overlay = Image.new(
            "RGBA",
            frame.size,
            (*overlay_rgb, round(255 * overlay_opacity)),
        )
        frame = Image.alpha_composite(frame, overlay)

    draw = ImageDraw.Draw(frame)
    title = str(compose.get("title") or "")
    if title:
        title_size = max(24, min(240, int(compose.get("title_size", 72) or 72)))
        title_font = _load_video_font(
            str(compose.get("font_family") or "malgun"),
            title_size,
            700,
        )
        title_spacing = title_size * 0.12
        title_x = width * _percent_position(compose.get("title_x"), 18) / 100
        title_y = height * _percent_position(compose.get("title_y"), 82) / 100
        anchor_text = str(compose.get("title_anchor_text") or "")
        if anchor_text:
            title_x -= _text_width(
                draw,
                anchor_text,
                title_font,
                title_spacing,
            ) / 2
        _draw_spaced_text(
            draw,
            (title_x, title_y),
            title,
            title_font,
            str(compose.get("text_color") or "#ffffff"),
            title_spacing,
            centered=not bool(anchor_text),
        )

    artist = str(compose.get("artist_name") or "")
    if artist:
        artist_size = max(
            12,
            min(120, int(compose.get("artist_size", 28) or 28)),
        )
        artist_font = _load_video_font(
            str(
                compose.get("artist_font_family")
                or compose.get("font_family")
                or "malgun"
            ),
            artist_size,
        )
        _draw_spaced_text(
            draw,
            (
                width * _percent_position(compose.get("artist_x"), 18) / 100,
                height * _percent_position(compose.get("artist_y"), 88) / 100,
            ),
            artist,
            artist_font,
            str(compose.get("artist_color") or "#ffffff"),
            artist_size * 0.16,
            centered=True,
        )

    icon_size = max(16, min(240, int(compose.get("icon_size", 64) or 64)))
    icon_x = width * _percent_position(compose.get("icon_x"), 50) / 100
    icon_y = height * _percent_position(compose.get("icon_y"), 18) / 100
    icon_image_name = str(compose.get("icon_image") or "")
    icon_image = resolve_video_icon(icon_image_name) if icon_image_name else None
    if icon_image and icon_image.suffix.lower() != ".svg":
        with Image.open(icon_image) as opened_icon:
            rendered_icon = ImageOps.contain(
                opened_icon.convert("RGBA"),
                (icon_size, icon_size),
                method=Image.Resampling.LANCZOS,
            )
        frame.alpha_composite(
            rendered_icon,
            (
                round(icon_x - rendered_icon.width / 2),
                round(icon_y - rendered_icon.height / 2),
            ),
        )
    elif icon_image:
        logger.warning("SVG video icon omitted by browser-free renderer path=%s", icon_image)
    else:
        icon = str(compose.get("icon") or "")
        if icon:
            icon_font = _load_video_font(
                str(compose.get("font_family") or "malgun"),
                icon_size,
            )
            _draw_spaced_text(
                draw,
                (icon_x, icon_y),
                icon,
                icon_font,
                str(compose.get("text_color") or "#ffffff"),
                0,
                centered=True,
            )

    frame.convert("RGB").save(destination, format="PNG")
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("Video frame composition failed: Pillow produced no output")
    logger.info(
        "Video frame composed without browser source=%s destination=%s size=%s",
        source,
        destination,
        destination.stat().st_size,
    )
    return destination


def render_thumbnail(
    background: Path,
    destination: Path,
    design: dict[str, Any],
) -> Path:
    if not background.is_file():
        raise FileNotFoundError(f"Thumbnail background was not found: {background}")
    width = max(320, min(3840, int(design.get("width", 1280) or 1280)))
    height = max(180, min(2160, int(design.get("height", 720) or 720)))
    with Image.open(background) as opened:
        canvas = ImageOps.fit(
            opened.convert("RGB"),
            (width, height),
            method=Image.Resampling.LANCZOS,
        )
    canvas = ImageEnhance.Brightness(canvas).enhance(
        max(0, 1 + float(design.get("brightness", 0) or 0) / 100)
    )
    canvas = ImageEnhance.Contrast(canvas).enhance(
        max(0, 1 + float(design.get("contrast", 0) or 0) / 100)
    )
    canvas = ImageEnhance.Color(canvas).enhance(
        max(0, 1 + float(design.get("saturation", 0) or 0) / 100)
    )
    blur = max(0, min(30, float(design.get("blur", 0) or 0)))
    if blur:
        canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
    canvas = canvas.convert("RGBA")
    overlay_opacity = max(0, min(1, float(design.get("overlay_opacity", 0) or 0)))
    if overlay_opacity:
        overlay_rgb = ImageColor.getrgb(str(design.get("overlay_color") or "#000000"))
        canvas = Image.alpha_composite(
            canvas,
            Image.new("RGBA", canvas.size, (*overlay_rgb, round(255 * overlay_opacity))),
        )

    for layer in design.get("layers") or []:
        layer_type = str(layer.get("type") or "")
        opacity = max(0, min(1, float(layer.get("opacity", 1) or 0)))
        if opacity <= 0:
            continue
        if layer_type == "text":
            text = str(layer.get("text") or "")
            if not text:
                continue
            font_size = max(12, min(240, int(layer.get("font_size", 72) or 72)))
            font = _load_video_font(str(layer.get("font_family") or "malgun"), font_size)
            layer_width = max(40, round(width * float(layer.get("width", 70) or 70) / 100))
            padding = max(0, min(80, int(layer.get("padding", 12) or 0)))
            stroke_width = max(0, min(20, int(layer.get("stroke_width", 3) or 0)))
            lines = text.splitlines() or [text]
            line_height = round(font_size * 1.2)
            text_height = max(line_height, line_height * len(lines))
            layer_image = Image.new(
                "RGBA",
                (layer_width + padding * 2, text_height + padding * 2),
                (0, 0, 0, 0),
            )
            draw = ImageDraw.Draw(layer_image)
            background_opacity = max(
                0, min(1, float(layer.get("background_opacity", 0) or 0))
            )
            if background_opacity:
                rgb = ImageColor.getrgb(str(layer.get("background_color") or "#000000"))
                draw.rounded_rectangle(
                    (0, 0, layer_image.width - 1, layer_image.height - 1),
                    radius=max(4, padding),
                    fill=(*rgb, round(255 * background_opacity)),
                )
            align = str(layer.get("align") or "center")
            text_x = padding if align == "left" else (
                layer_image.width - padding if align == "right" else layer_image.width / 2
            )
            anchor = "lm" if align == "left" else ("rm" if align == "right" else "mm")
            if bool(layer.get("shadow", True)):
                draw.multiline_text(
                    (text_x + 4, layer_image.height / 2 + 5),
                    text,
                    font=font,
                    fill=(0, 0, 0, round(180 * opacity)),
                    anchor=anchor,
                    align=align,
                    spacing=round(font_size * 0.2),
                    stroke_width=stroke_width,
                    stroke_fill=(0, 0, 0, round(180 * opacity)),
                )
            color = ImageColor.getrgb(str(layer.get("color") or "#ffffff"))
            stroke = ImageColor.getrgb(str(layer.get("stroke_color") or "#000000"))
            draw.multiline_text(
                (text_x, layer_image.height / 2),
                text,
                font=font,
                fill=(*color, round(255 * opacity)),
                anchor=anchor,
                align=align,
                spacing=round(font_size * 0.2),
                stroke_width=stroke_width,
                stroke_fill=(*stroke, round(255 * opacity)),
            )
        elif layer_type == "icon":
            size = max(16, min(500, int(layer.get("size", 96) or 96)))
            icon_name = str(layer.get("icon_image") or "")
            icon_path = resolve_video_icon(icon_name) if icon_name else None
            if icon_path:
                source = icon_path
                temporary_svg = None
                if icon_path.suffix.lower() == ".svg":
                    temporary_svg = destination.parent / f".{db.new_id()}-icon.png"
                    source = rasterize_svg_icon(icon_path, temporary_svg, size)
                try:
                    with Image.open(source) as opened:
                        layer_image = ImageOps.contain(
                            opened.convert("RGBA"),
                            (size, size),
                            method=Image.Resampling.LANCZOS,
                        )
                finally:
                    if temporary_svg:
                        temporary_svg.unlink(missing_ok=True)
                if opacity < 1:
                    alpha = layer_image.getchannel("A").point(
                        lambda value: round(value * opacity)
                    )
                    layer_image.putalpha(alpha)
            else:
                icon = str(layer.get("icon") or "")
                if not icon:
                    continue
                font = _load_video_font("malgun", size)
                layer_image = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
                draw = ImageDraw.Draw(layer_image)
                color = ImageColor.getrgb(str(layer.get("color") or "#ffffff"))
                draw.text(
                    (layer_image.width / 2, layer_image.height / 2),
                    icon,
                    font=font,
                    fill=(*color, round(255 * opacity)),
                    anchor="mm",
                )
        else:
            continue

        rotation = float(layer.get("rotation", 0) or 0)
        if rotation:
            layer_image = layer_image.rotate(
                -rotation,
                expand=True,
                resample=Image.Resampling.BICUBIC,
            )
        center_x = width * float(layer.get("x", 50) or 50) / 100
        center_y = height * float(layer.get("y", 50) or 50) / 100
        canvas.alpha_composite(
            layer_image,
            (
                round(center_x - layer_image.width / 2),
                round(center_y - layer_image.height / 2),
            ),
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(destination, "PNG")
    return destination


def validate_static_video_frame(source: Path, frame: Path) -> None:
    if not frame.is_file() or frame.stat().st_size == 0:
        raise RuntimeError("Video frame validation failed: output file is missing")
    with Image.open(source) as source_image, Image.open(frame) as frame_image:
        expected = ImageOps.fit(
            source_image.convert("RGB"),
            (1920, 1080),
            method=Image.Resampling.LANCZOS,
        )
        actual = frame_image.convert("RGB")
        if actual.size != expected.size:
            raise RuntimeError(
                f"Video frame validation failed: unexpected size {actual.size}"
            )
        sample_size = (64, 36)
        expected_sample = expected.resize(sample_size, Image.Resampling.BILINEAR)
        actual_sample = actual.resize(sample_size, Image.Resampling.BILINEAR)
        difference = ImageChops.difference(expected_sample, actual_sample)
        mean_difference = sum(ImageStat.Stat(difference).mean) / 3
        actual_stddev = sum(ImageStat.Stat(actual_sample).stddev) / 3
        if mean_difference > 90 or actual_stddev < 8:
            raise RuntimeError(
                "Video frame validation failed: output does not resemble the "
                f"source image (mean_difference={mean_difference:.2f}, "
                f"stddev={actual_stddev:.2f})"
            )
        logger.info(
            "Video frame validation passed source=%s frame=%s "
            "mean_difference=%.2f stddev=%.2f",
            source,
            frame,
            mean_difference,
            actual_stddev,
        )


def _render_static_video_frame_browser(
    source: Path,
    destination: Path,
    compose: dict[str, Any],
) -> Path:
    browser = _browser_executable()
    if not browser:
        raise RuntimeError(
            "Video frame composition requires Edge, Chrome, Chromium, or VIDEO_SVG_BROWSER."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    html_path = destination.with_suffix(".html")
    diagnostic_html_path = destination.with_suffix(".diagnostic.html")
    diagnostic_html_path.unlink(missing_ok=True)

    font_families = {
        "malgun": '"Bundled Noto Sans KR", "Malgun Gothic", sans-serif',
        "noto_sans_kr": '"Bundled Noto Sans KR", "Malgun Gothic", sans-serif',
        "noto_serif_kr": '"Bundled Noto Serif KR", "Batang", serif',
        "nanum_gothic": '"Bundled Nanum Gothic", "Malgun Gothic", sans-serif',
        "nanum_pen": '"Bundled Nanum Pen Script", "Malgun Gothic", cursive',
        "han_dotum": '"Bundled Noto Sans KR", "Malgun Gothic", sans-serif',
        "han_batang": '"Bundled Noto Serif KR", "Batang", serif',
        "batang": '"Bundled Noto Serif KR", "Batang", serif',
        "arial": '"Bundled Roboto", Arial, "Malgun Gothic", sans-serif',
        "roboto": '"Bundled Roboto", "Noto Sans KR", "Malgun Gothic", sans-serif',
        "bebas": '"Bundled Bebas Neue", "Noto Sans KR", "Malgun Gothic", sans-serif',
        "anton": '"Bundled Anton", "Noto Sans KR", "Malgun Gothic", sans-serif',
        "cinzel": '"Bundled Cinzel", "Noto Serif KR", "Batang", serif',
        "georgia": '"Bundled Noto Serif KR", Georgia, "Malgun Gothic", serif',
        "impact": '"Bundled Anton", Impact, "Malgun Gothic", sans-serif',
        "consolas": '"Bundled Roboto", Consolas, "Noto Sans KR", monospace',
        "black_han_sans": '"Bundled Black Han Sans", "Bundled Noto Sans KR", sans-serif',
        "do_hyeon": '"Bundled Do Hyeon", "Bundled Noto Sans KR", sans-serif',
        "jua": '"Bundled Jua", "Bundled Noto Sans KR", sans-serif',
        "gowun_dodum": '"Bundled Gowun Dodum", "Bundled Noto Sans KR", sans-serif',
        "gowun_batang": '"Bundled Gowun Batang", "Bundled Noto Serif KR", serif',
        "song_myung": '"Bundled Song Myung", "Bundled Noto Serif KR", serif',
        "poor_story": '"Bundled Poor Story", "Bundled Noto Sans KR", cursive',
        "gaegu": '"Bundled Gaegu", "Bundled Noto Sans KR", cursive',
        "single_day": '"Bundled Single Day", "Bundled Noto Sans KR", cursive',
        "montserrat": '"Bundled Montserrat", "Bundled Noto Sans KR", sans-serif',
        "oswald": '"Bundled Oswald", "Bundled Noto Sans KR", sans-serif',
        "playfair": '"Bundled Playfair Display", "Bundled Noto Serif KR", serif',
    }

    def percent(name: str, default: float) -> float:
        return _percent_position(compose.get(name), default)

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(compose.get(name, default) or default)))
        except (TypeError, ValueError):
            return default

    title = str(compose.get("title") or "")
    artist = str(compose.get("artist_name") or "")
    icon = str(compose.get("icon") or "")
    icon_image_name = str(compose.get("icon_image") or "")
    icon_image = resolve_video_icon(icon_image_name) if icon_image_name else None
    if icon_image_name and not icon_image:
        raise ValueError(f"Selected video icon was not found: {icon_image_name}")

    brightness = max(0, 1 + float(compose.get("brightness", 0) or 0) / 100)
    contrast = max(0, 1 + float(compose.get("contrast", 0) or 0) / 100)
    saturation = max(0, 1 + float(compose.get("saturation", 0) or 0) / 100)
    blur = max(0, float(compose.get("blur", 0) or 0))
    overlay_color = str(compose.get("overlay_color") or "#21000f")
    overlay_opacity = max(0, min(1, float(compose.get("overlay_opacity", 0) or 0)))
    title_font_key = str(compose.get("font_family") or "malgun")
    artist_font_key = str(
        compose.get("artist_font_family") or title_font_key
    )
    title_font_file = _video_font_path(title_font_key)
    artist_font_file = _video_font_path(artist_font_key)
    title_font = (
        '"VideoTitleFont", ' + font_families.get(title_font_key, font_families["malgun"])
        if title_font_file
        else font_families.get(title_font_key, font_families["malgun"])
    )
    artist_font = (
        '"VideoArtistFont", ' + font_families.get(artist_font_key, font_families["malgun"])
        if artist_font_file
        else font_families.get(artist_font_key, font_families["malgun"])
    )
    title_font_attr = html.escape(title_font, quote=True)
    artist_font_attr = html.escape(artist_font, quote=True)
    font_face_css = "".join(
        [
            (
                "@font-face{font-family:'VideoTitleFont';"
                f"src:url('{title_font_file.resolve().as_uri()}')}}"
            )
            if title_font_file
            else "",
            (
                "@font-face{font-family:'VideoArtistFont';"
                f"src:url('{artist_font_file.resolve().as_uri()}')}}"
            )
            if artist_font_file
            else "",
        ]
    )
    icon_size = integer("icon_size", 64, 16, 240)
    icon_markup = ""
    if icon_image:
        icon_markup = (
            f'<img class="icon" src="{html.escape(icon_image.resolve().as_uri())}" '
            f'style="left:{percent("icon_x", 50)}%;top:{percent("icon_y", 18)}%;'
            f'width:{icon_size}px;height:{icon_size}px" alt="">'
        )
    elif icon:
        icon_markup = (
            f'<div class="icon icon-text" style="left:{percent("icon_x", 50)}%;'
            f'top:{percent("icon_y", 18)}%;font-family:{title_font_attr};'
            f'font-size:{icon_size}px;color:{html.escape(str(compose.get("text_color") or "#ffffff"))}">'
            f"{html.escape(icon)}</div>"
        )

    title_markup = ""
    if title:
        title_style = (
            f'left:{percent("title_x", 18)}%;top:{percent("title_y", 82)}%;'
            f'font-family:{title_font_attr};'
            f'font-size:{integer("title_size", 72, 24, 240)}px;'
            f'color:{html.escape(str(compose.get("text_color") or "#ffffff"))}'
        )
        title_anchor_text = str(compose.get("title_anchor_text") or "")
        if title_anchor_text:
            title_markup = (
                f'<div class="title-anchor" style="{title_style}">'
                f'<span class="title-reference">{html.escape(title_anchor_text)}</span>'
                f'<div class="title title-left">{html.escape(title)}</div></div>'
            )
        else:
            title_markup = (
                f'<div class="title" style="{title_style}">{html.escape(title)}</div>'
            )
    artist_markup = ""
    if artist:
        artist_markup = (
            f'<div class="artist" style="left:{percent("artist_x", 18)}%;'
            f'top:{percent("artist_y", 88)}%;font-family:{artist_font_attr};'
            f'font-size:{integer("artist_size", 28, 12, 120)}px;'
            f'color:{html.escape(str(compose.get("artist_color") or "#ffffff"))}">'
            f"{html.escape(artist)}</div>"
        )

    html_path.write_text(
        f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{font_face_css}
html,body{{margin:0;width:1920px;height:1080px;overflow:hidden;background:#000}}
.background{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
filter:brightness({brightness}) contrast({contrast}) saturate({saturation}) blur({blur}px);
transform:scale({1.02 if blur else 1})}}
.overlay{{position:absolute;inset:0;background:{overlay_color};opacity:{overlay_opacity}}}
.title,.artist,.icon,.title-anchor{{position:absolute;transform:translate(-50%,-50%);white-space:nowrap;
text-shadow:0 2px 8px rgba(0,0,0,.7)}}
.title{{font-weight:700;letter-spacing:.12em}}
.title-anchor{{font-weight:700;letter-spacing:.12em}}
.title-reference{{visibility:hidden}}
.title-anchor .title-left{{left:0;top:50%;transform:translateY(-50%)}}
.artist{{font-weight:400}}
.icon{{object-fit:contain}}
.icon-text{{font-weight:400;line-height:1}}
</style></head><body>
<img class="background" src="{html.escape(source.resolve().as_uri())}" alt="">
<div class="overlay"></div>{title_markup}{artist_markup}{icon_markup}
</body></html>""",
        encoding="utf-8",
    )
    source_stat = source.stat() if source.is_file() else None
    icon_stat = icon_image.stat() if icon_image and icon_image.is_file() else None
    try:
        browser_version = subprocess.run(
            [str(browser), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        browser_version_text = (
            (browser_version.stdout or browser_version.stderr or "").strip()
        )
    except Exception as exc:
        browser_version_text = f"version check failed: {type(exc).__name__}: {exc}"
    try:
        source_header = source.read_bytes()[:32].hex()
    except OSError as exc:
        source_header = f"read failed: {type(exc).__name__}: {exc}"
    logger.info(
        "Video frame composition prepared source=%s source_exists=%s "
        "source_size=%s source_mtime_ns=%s source_header=%s destination=%s "
        "html=%s html_size=%s browser=%s browser_version=%s title=%r "
        "title_codepoints=%s artist=%r icon_name=%r icon_path=%s "
        "icon_size=%s title_font=%s artist_font=%s compose=%s",
        source,
        source.is_file(),
        source_stat.st_size if source_stat else None,
        source_stat.st_mtime_ns if source_stat else None,
        source_header,
        destination,
        html_path,
        html_path.stat().st_size,
        browser,
        browser_version_text,
        title,
        " ".join(f"U+{ord(character):04X}" for character in title),
        artist,
        icon_image_name,
        icon_image,
        icon_stat.st_size if icon_stat else None,
        title_font_file,
        artist_font_file,
        json.dumps(compose, ensure_ascii=False, sort_keys=True),
    )
    completed: subprocess.CompletedProcess[str] | None = None
    succeeded = False
    try:
        for attempt in range(1, 4):
            profile_dir = destination.parent / (
                f".frame-browser-{destination.stem}-{attempt}-{uuid.uuid4().hex[:8]}"
            )
            destination.unlink(missing_ok=True)
            command = [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--disable-background-networking",
                "--allow-file-access-from-files",
                "--force-device-scale-factor=1",
                f"--user-data-dir={profile_dir}",
                "--window-size=1920,1080",
                f"--screenshot={destination}",
                html_path.resolve().as_uri(),
            ]
            attempt_started = time.monotonic()
            logger.info(
                "Video frame composition attempt started attempt=%s "
                "profile=%s command=%s",
                attempt,
                profile_dir,
                json.dumps(command, ensure_ascii=False),
            )
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                deadline = time.monotonic() + 10
                while not destination.is_file() and time.monotonic() < deadline:
                    time.sleep(0.1)
                output_stat = destination.stat() if destination.is_file() else None
                logger.info(
                    "Video frame composition attempt finished attempt=%s "
                    "elapsed=%.3f exit=%s output_exists=%s output_size=%s "
                    "stdout=%r stderr=%r",
                    attempt,
                    time.monotonic() - attempt_started,
                    completed.returncode,
                    destination.is_file(),
                    output_stat.st_size if output_stat else None,
                    (completed.stdout or "")[-2000:],
                    (completed.stderr or "")[-2000:],
                )
                if completed.returncode == 0 and destination.is_file():
                    succeeded = True
                    return destination
            except subprocess.TimeoutExpired as exc:
                completed = subprocess.CompletedProcess(
                    command,
                    -1,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "browser timed out",
                )
                logger.warning(
                    "Video frame composition attempt timed out attempt=%s "
                    "elapsed=%.3f profile=%s",
                    attempt,
                    time.monotonic() - attempt_started,
                    profile_dir,
                )
            finally:
                shutil.rmtree(profile_dir, ignore_errors=True)
            if attempt < 3:
                time.sleep(attempt)
    finally:
        if succeeded:
            html_path.unlink(missing_ok=True)
        elif html_path.is_file():
            shutil.copy2(html_path, diagnostic_html_path)
            html_path.unlink(missing_ok=True)
            logger.error(
                "Video frame composition diagnostic HTML preserved path=%s",
                diagnostic_html_path,
            )
    stdout = ((completed.stdout if completed else "") or "").strip()
    stderr = ((completed.stderr if completed else "") or "").strip()
    detail = stderr or stdout or "no output"
    return_code = completed.returncode if completed else "not started"
    raise RuntimeError(
        f"Video frame composition failed after 3 attempts "
        f"(exit={return_code}): {detail[-800:]}"
    )


def _model_dump(model: Any, *, exclude_none: bool = False) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=exclude_none)
    return model.dict(exclude_none=exclude_none)


def create_job(
    job_type: str,
    resource_type: str,
    resource_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = db.now_iso()
    return db.insert(
        "jobs",
        {
            "id": db.new_id(),
            "type": job_type,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "status": "pending",
            "progress": 0,
            "attempt": 0,
            "max_attempts": 3,
            "error_code": None,
            "error_message": None,
            "payload_json": db.encode_json(payload or {}),
            "result_json": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        },
    )


def set_job_running(job_id: str) -> None:
    job = db.get_one("jobs", job_id)
    db.update(
        "jobs",
        job_id,
        {
            "status": "running",
            "attempt": int(job["attempt"]) + 1 if job else 1,
            "started_at": db.now_iso(),
            "error_code": None,
            "error_message": None,
        },
    )


def set_job_progress(job_id: str, progress: int) -> None:
    db.update("jobs", job_id, {"progress": max(0, min(100, progress))})


def set_job_activity(job_id: str, activity: dict[str, Any]) -> None:
    job = db.get_one("jobs", job_id)
    payload = (job or {}).get("payload") or {}
    payload["activity"] = activity
    db.update("jobs", job_id, {"payload_json": db.encode_json(payload)})


def set_job_succeeded(job_id: str, result: dict[str, Any]) -> None:
    db.update(
        "jobs",
        job_id,
        {
            "status": "succeeded",
            "progress": 100,
            "result_json": db.encode_json(result),
            "finished_at": db.now_iso(),
        },
    )


def set_job_failed(job_id: str, exc: Exception, code: str = "JOB_FAILED") -> None:
    db.update(
        "jobs",
        job_id,
        {
            "status": "failed",
            "error_code": code,
            "error_message": str(exc)[:2000],
            "finished_at": db.now_iso(),
        },
    )


def _extract_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Gemini response did not contain a JSON object")
        parsed = json.loads(candidate[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Gemini response root must be an object")
    return parsed


def _gemini_generate_content(
    model: str,
    payload: dict[str, Any],
    timeout: float = 300,
) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is not configured. Add it to the project .env file "
            "and restart the backend."
        )
    model = model.strip()
    if not model:
        raise ValueError("A Gemini model name is required.")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model, safe='')}:generateContent"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("error", {}).get("message", body)
        except json.JSONDecodeError:
            detail = body
        raise RuntimeError(f"Gemini API request failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not connect to the Gemini API: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini API returned invalid JSON.") from exc

    if not isinstance(result, dict):
        raise RuntimeError("Gemini API returned an invalid response.")
    return result


def _gemini_response_parts(result: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = result.get("candidates") or []
    if not candidates:
        block_reason = (result.get("promptFeedback") or {}).get("blockReason")
        suffix = f": {block_reason}" if block_reason else ""
        raise RuntimeError(f"Gemini API returned no candidates{suffix}.")
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    if not parts:
        raise RuntimeError("Gemini API returned an empty response.")
    return parts


def _gemini_text(system_instruction: str, user_text: str) -> str:
    result = _gemini_generate_content(
        os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        {
            "systemInstruction": {
                "parts": [{"text": system_instruction}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_text}],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.8,
            },
        },
    )
    text = "".join(
        str(part.get("text", ""))
        for part in _gemini_response_parts(result)
        if "text" in part
    ).strip()
    if not text:
        raise RuntimeError("Gemini API response did not contain text.")
    return text


def _gemini_image(prompt: str, aspect_ratio: str) -> tuple[bytes, str]:
    result = _gemini_generate_content(
        os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_GEMINI_IMAGE_MODEL),
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": aspect_ratio},
            },
        },
    )
    for part in _gemini_response_parts(result):
        inline_data = part.get("inlineData") or part.get("inline_data")
        if not inline_data or not inline_data.get("data"):
            continue
        try:
            raw = base64.b64decode(inline_data["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Gemini API returned invalid image data.") from exc
        return raw, inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
    raise RuntimeError("Gemini API response did not contain an image.")


THUMBNAIL_COPY_SYSTEM = """
You are a Korean YouTube thumbnail copywriter for music and playlist channels.
Return one valid JSON object only, without Markdown or commentary.

Create concise thumbnail copy that earns attention through relevance, curiosity,
specific mood, and clear listening context. Do not use deceptive clickbait,
fabricated popularity claims, fake urgency, guarantees, or unsupported numbers.

Return exactly these string fields:
- headline: the strongest main phrase, preferably 8-18 Korean characters
- subheadline: a supporting phrase, preferably 12-28 Korean characters
- accent: a short category or mood label, preferably 2-10 Korean characters

Make the three phrases complementary rather than repetitive.
Avoid quotation marks, hashtags, emojis, and ending punctuation.
"""


def run_thumbnail_copy_generation(
    job_id: str,
    album_id: str,
    instruction: str,
) -> None:
    try:
        set_job_running(job_id)
        album = db.get_one("albums", album_id)
        if not album:
            raise ValueError("Album not found")
        tracks = db.fetch_all(
            "SELECT title, concept FROM tracks WHERE album_id = ? ORDER BY sequence",
            (album_id,),
        )
        user = json.dumps(
            {
                "album_title": album.get("title"),
                "artist_name": album.get("artist_name"),
                "description": album.get("description"),
                "genre": album.get("genre"),
                "mood": album.get("mood"),
                "keywords": album.get("keywords"),
                "style_prompt": album.get("style_prompt"),
                "track_titles": [track.get("title") for track in tracks[:12]],
                "track_concepts": [track.get("concept") for track in tracks[:6]],
                "user_direction": instruction,
                "language": "Korean",
            },
            ensure_ascii=False,
        )
        result = _extract_json(_gemini_text(THUMBNAIL_COPY_SYSTEM, user))
        copy = {}
        limits = {"headline": 40, "subheadline": 60, "accent": 20}
        for key, limit in limits.items():
            value = re.sub(r"\s+", " ", str(result.get(key) or "")).strip()
            if not value:
                raise ValueError(f"Gemini thumbnail copy did not include {key}")
            copy[key] = value[:limit]
        set_job_succeeded(job_id, copy)
    except Exception as exc:
        set_job_failed(job_id, exc, "THUMBNAIL_COPY_GENERATION_FAILED")


SUNO_ALBUM_PLAN_SYSTEM = """
You are an expert music director, lyricist, and prompt engineer for Suno Custom Mode.
Return one valid JSON object only. Do not use Markdown fences or add commentary.

Plan a cohesive album while giving every track a distinct musical identity.
Honor all user settings and write lyrics in the requested lyrics language.
Do not mention real artists, copyrighted song titles, or imitation requests.

SUNO STYLE FIELD RULES
- Write common_style_prompt and every track style_prompt in English.
- Format them as concise, comma-separated production tags, not prose sentences.
- Keep every style_prompt at or below 200 characters.
- Put only musical directions in style_prompt. Never put lyrics or section labels there.
- Include these categories in a natural order:
  1. genre and subgenre
  2. era or aesthetic
  3. vocal type, tone, and delivery
  4. exact or narrow BPM and time signature
  5. rhythm and groove, such as upbeat, groovy, syncopated, half-time, or four-on-the-floor
  6. lead and supporting instruments
  7. mood and emotional arc
  8. arrangement and dynamics
  9. recording, mix, and mastering qualities
- Prefer concrete directions such as "102 BPM", "4/4 beat", "syncopated bass groove",
  "warm analog synthesizers", and "intimate breathy female vocal".
- Avoid vague filler, contradictory tags, excessive genre lists, and repeated synonyms.
- common_style_prompt defines the album's shared sonic identity.
- Each track style_prompt preserves that identity but adds track-specific tempo,
  rhythm, instrumentation, arrangement, and emotional development.

SUNO LYRICS FIELD RULES
- The lyrics value must contain the full singable lyrics, not a synopsis.
- Use square-bracket structure tags to control the song flow.
- Use a suitable structure such as:
  [Intro], [Verse 1], [Pre-Chorus], [Chorus], [Verse 2], [Chorus],
  [Bridge], [Final Chorus], [Outro].
- Add [Instrumental Solo] or [Drop] only when appropriate for the genre.
- Vocal meta-tags such as [Vocal: Female], [Vocal: Male], [Harmonized],
  [Ad-lib], and [Spoken Word] may be used sparingly where they improve delivery.
- Place vocal meta-tags on their own line immediately before the relevant section.
- Make the chorus memorable and repeatable while allowing the final chorus to develop.
- Keep verse imagery specific, maintain a consistent point of view, and avoid generic AI cliches.
- Do not place production notes, explanations, translations, or style keywords inside lyric lines.

IMAGE PROMPT RULES
- Write visual_concept in cinematic English as the album-wide visual identity.
- Write thumbnail_image_prompt in cinematic English for a YouTube playlist
  thumbnail background that matches the album, not a generic music image.
- The thumbnail prompt must include album-specific subject matter, setting,
  lighting, color palette, emotional tone, and clear negative space for text.
- The thumbnail prompt must not request typography, logos, watermarks, UI,
  album covers, microphones, headphones, or generic music-note imagery unless
  explicitly requested by the user.
- Write image_prompt in cinematic English for a 16:9 composition.
- Describe subject, setting, lighting, palette, camera framing, and atmosphere.
- Do not request typography, logos, watermarks, or text in the image.

JSON schema:
{
  "album_summary": "album concept in the requested language",
  "common_style_prompt": "comma-separated English Suno style tags",
  "visual_concept": "album-wide cinematic visual identity in English",
  "thumbnail_image_prompt": "album-specific cinematic 16:9 YouTube thumbnail background prompt in English",
  "tracks": [{
    "sequence": 1,
    "title": "title in the requested language",
    "concept": "track concept in the requested language",
    "lyrics": "[Intro]\\n...\\n[Verse 1]\\n...\\n[Chorus]\\n...",
    "style_prompt": "comma-separated English Suno style tags",
    "image_prompt": "cinematic English 16:9 image prompt"
  }]
}
""".strip()


SUNO_LYRICS_SYSTEM = """
You are an expert lyricist and prompt engineer for Suno Custom Mode.
Return one valid JSON object only. Do not use Markdown fences or commentary.

Write complete, performance-ready lyrics in the requested language.
Use square-bracket song structure tags, normally including [Intro], [Verse 1],
[Pre-Chorus], [Chorus], [Verse 2], [Bridge], [Final Chorus], and [Outro].
Use [Instrumental Solo] or [Drop] only when musically appropriate.
Use vocal meta-tags such as [Vocal: Female], [Vocal: Male], [Harmonized],
[Ad-lib], or [Spoken Word] sparingly and place each tag on its own line.
Create a memorable chorus, concrete verse imagery, a consistent point of view,
natural phrasing, and an emotional progression. Do not put production notes in lyric lines.

When regenerate_style is true, write style_prompt in English as concise,
comma-separated Suno tags. Include genre/subgenre, era, vocal tone and delivery,
specific BPM, time signature, rhythm/groove, lead and supporting instruments,
mood, arrangement/dynamics, and mix/mastering qualities. Avoid prose, vague filler,
contradictory tags, real artist names, lyrics, and square-bracket section labels.
Keep style_prompt at or below 200 characters.

Write image_prompt in cinematic English for 16:9. Include subject, setting,
lighting, palette, framing, and atmosphere, with no typography or watermark.

JSON schema:
{
  "lyrics": "[Intro]\\n...\\n[Verse 1]\\n...\\n[Chorus]\\n...",
  "style_prompt": "comma-separated English Suno style tags",
  "image_prompt": "cinematic English 16:9 image prompt"
}
""".strip()


def run_album_plan(job_id: str, album_id: str) -> None:
    try:
        set_job_running(job_id)
        album = db.get_one("albums", album_id)
        if not album:
            raise ValueError("Album not found")
        db.update(
            "albums",
            album_id,
            {"status": "planning", "updated_at": db.now_iso()},
        )
        set_job_progress(job_id, 10)
        user = json.dumps(
            {
                "task": "Create a production-ready Suno Custom Mode album plan.",
                "title": album["title"],
                "artist_name": album.get("artist_name"),
                "description": album.get("description"),
                "genre": album["genre"],
                "vocal_style": album["vocal_style"],
                "tempo": album["tempo"],
                "lyrics_language": album["lyrics_language"],
                "mood": album["mood"],
                "instruments": album.get("instruments") or [],
                "keywords": album["keywords"],
                "additional_instructions": album["additional_instructions"],
                "track_count": album["track_count"],
                "quality_checklist": {
                    "style_format": "English comma-separated tags",
                    "required_style_categories": [
                        "genre and subgenre",
                        "era or aesthetic",
                        "vocal tone and delivery",
                        "BPM and time signature",
                        "rhythm and groove",
                        "lead and supporting instruments",
                        "mood and emotional arc",
                        "arrangement and dynamics",
                        "mix and mastering",
                    ],
                    "lyrics_format": "full lyrics with square-bracket structure tags",
                    "minimum_core_sections": [
                        "[Verse 1]",
                        "[Chorus]",
                        "[Verse 2]",
                        "[Bridge]",
                        "[Final Chorus]",
                    ],
                },
            },
            ensure_ascii=False,
        )
        plan = _extract_json(_gemini_text(SUNO_ALBUM_PLAN_SYSTEM, user))
        tracks = plan.get("tracks")
        if not isinstance(tracks, list) or not tracks:
            raise ValueError("Gemini plan did not include tracks")
        tracks = tracks[: int(album["track_count"])]
        set_job_progress(job_id, 60)
        db.execute("DELETE FROM tracks WHERE album_id = ?", (album_id,))
        now = db.now_iso()
        created = []
        for index, item in enumerate(tracks, start=1):
            if not isinstance(item, dict):
                continue
            created.append(
                db.insert(
                    "tracks",
                    {
                        "id": db.new_id(),
                        "album_id": album_id,
                        "sequence": index,
                        "title": str(item.get("title") or f"Track {index}")[:200],
                        "concept": str(item.get("concept") or ""),
                        "lyrics": str(item.get("lyrics") or ""),
                        "style_prompt": str(item.get("style_prompt") or ""),
                        "image_prompt": str(item.get("image_prompt") or ""),
                        "negative_tags": "",
                        "instrumental": 0,
                        "model": DEFAULT_SUNO_MODEL,
                        "status": "lyrics_ready",
                        "selected_generation_id": None,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            )
        if not created:
            raise ValueError("Gemini plan did not contain valid tracks")
        db.update(
            "albums",
            album_id,
            {
                "description": str(
                    plan.get("album_summary") or album.get("description") or ""
                ),
                "style_prompt": str(plan.get("common_style_prompt") or ""),
                "visual_concept": str(plan.get("visual_concept") or ""),
                "thumbnail_image_prompt": str(
                    plan.get("thumbnail_image_prompt") or ""
                ),
                "status": "lyrics_ready",
                "updated_at": db.now_iso(),
            },
        )
        set_job_succeeded(job_id, {"track_ids": [track["id"] for track in created]})
    except Exception as exc:
        db.update(
            "albums",
            album_id,
            {"status": "failed", "updated_at": db.now_iso()},
        )
        set_job_failed(job_id, exc, "ALBUM_PLAN_FAILED")


def run_lyrics_generation(
    job_id: str,
    track_id: str,
    instruction: str = "",
    regenerate_style: bool = True,
) -> None:
    try:
        set_job_running(job_id)
        track = db.fetch_one(
            """
            SELECT t.*, a.title AS album_title, a.genre, a.vocal_style, a.tempo,
                   a.lyrics_language, a.mood, a.instruments_json, a.keywords,
                   a.additional_instructions
              FROM tracks t JOIN albums a ON a.id = t.album_id
             WHERE t.id = ?
            """,
            (track_id,),
        )
        if not track:
            raise ValueError("Track not found")
        user = json.dumps(
            {
                "task": "Create or revise production-ready Suno Custom Mode lyrics and metadata.",
                "album_title": track["album_title"],
                "track_title": track["title"],
                "concept": track["concept"],
                "current_lyrics": track["lyrics"],
                "current_style_prompt": track["style_prompt"],
                "genre": track["genre"],
                "vocal_style": track["vocal_style"],
                "tempo": track["tempo"],
                "lyrics_language": track["lyrics_language"],
                "mood": track["mood"],
                "instruments": track.get("instruments") or [],
                "keywords": track["keywords"],
                "additional_instructions": track["additional_instructions"],
                "revision_instruction": instruction,
                "regenerate_style": regenerate_style,
                "quality_checklist": {
                    "lyrics": "full singable lyrics with structured square-bracket tags",
                    "style": "English comma-separated tags covering genre, vocal, BPM, rhythm, instruments, mood, arrangement, and production",
                },
            },
            ensure_ascii=False,
        )
        result = _extract_json(_gemini_text(SUNO_LYRICS_SYSTEM, user))
        values = {
            "lyrics": str(result.get("lyrics") or track["lyrics"]),
            "image_prompt": str(result.get("image_prompt") or track["image_prompt"]),
            "status": "lyrics_ready",
            "updated_at": db.now_iso(),
        }
        if regenerate_style:
            values["style_prompt"] = str(
                result.get("style_prompt") or track["style_prompt"]
            )
        db.update("tracks", track_id, values)
        set_job_succeeded(job_id, {"track_id": track_id})
    except Exception as exc:
        set_job_failed(job_id, exc, "LYRICS_GENERATION_FAILED")


def _suno_token() -> str:
    token = suno_auth.get_token()
    if not token:
        update_token(suno_auth)
        token = suno_auth.get_token()
    if not token:
        raise RuntimeError("Suno authentication token is unavailable")
    return token


async def _refresh_suno_auth_with_warmup(rejected_cookie: str) -> str:
    async with _suno_reauth_lock:
        if suno_auth.get_cookie() != rejected_cookie:
            update_token(suno_auth)
            return _suno_token()

        logger.warning(
            "Suno generation validation failed; opening browser for login/warmup"
        )
        try:
            auth = await asyncio.to_thread(
                capture_auth_with_browser,
                profile_dir=SUNO_PROFILE_DIR,
                timeout_sec=SUNO_LOGIN_TIMEOUT_SECONDS,
                wait_for_browser_close=True,
            )
            valid = await asyncio.to_thread(validate_auth, auth)
            if not valid:
                raise SunoAuthError("The refreshed Suno login could not be validated.")
            await asyncio.to_thread(save_auth, SUNO_AUTH_FILE, auth)
            os.environ["SESSION_ID"] = auth.session_id
            os.environ["COOKIE"] = auth.cookie
            suno_auth.replace_auth(auth.session_id, auth.cookie)
            await asyncio.to_thread(update_token, suno_auth)
        except Exception as exc:
            logger.exception("Suno login/warmup refresh failed")
            raise SunoGenerationVerificationError(
                "Suno 로그인/생성 페이지 워밍업에 실패했습니다. 열린 Suno 창에서 "
                "로그인 후 생성 페이지를 새로고침하거나 직접 생성 테스트를 마치고 "
                "창을 닫은 뒤 다시 시도해 주세요."
            ) from exc

        logger.info("Suno login/warmup refreshed and applied to the running server")
        return _suno_token()


def _validate_suno_payload(payload: dict[str, Any]) -> None:
    limits = (
        ("title", SUNO_MAX_TITLE_CHARS),
        ("negative_tags", SUNO_MAX_NEGATIVE_STYLE_CHARS),
    )
    for field, limit in limits:
        value = str(payload.get(field) or "")
        if len(value) > limit:
            raise ValueError(
                f"Suno {field} is too long: {len(value)} characters (maximum {limit})"
            )
    if "gpt_description_prompt" not in payload:
        prompt = str(payload.get("prompt") or "")
        if len(prompt) > SUNO_MAX_CUSTOM_PROMPT_CHARS:
            raise ValueError(
                "Suno prompt is too long: "
                f"{len(prompt)} characters (maximum {SUNO_MAX_CUSTOM_PROMPT_CHARS})"
            )


def _prepare_suno_payload(payload: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(payload)
    tags = str(prepared.get("tags") or "")
    if len(tags) > SUNO_MAX_STYLE_CHARS:
        complete_tags = []
        for tag in (part.strip() for part in tags.split(",")):
            if not tag:
                continue
            candidate = ", ".join([*complete_tags, tag])
            if len(candidate) > SUNO_MAX_STYLE_CHARS:
                break
            complete_tags.append(tag)
        prepared["tags"] = ", ".join(complete_tags) or tags[:SUNO_MAX_STYLE_CHARS]
        logger.warning(
            "Suno style tags compacted original_length=%s submitted_length=%s",
            len(tags),
            len(prepared["tags"]),
        )
    _validate_suno_payload(prepared)
    return prepared


async def _submit_suno_generation(payload: dict[str, Any]) -> tuple[Any, str]:
    payload = _prepare_suno_payload(payload)
    summary = {
        "mode": "description" if "gpt_description_prompt" in payload else "custom",
        "model": payload.get("mv"),
        "title_length": len(str(payload.get("title") or "")),
        "lyrics_length": len(str(payload.get("prompt") or "")),
        "style_length": len(
            str(payload.get("tags") or payload.get("gpt_description_prompt") or "")
        ),
        "negative_tags_length": len(str(payload.get("negative_tags") or "")),
        "instrumental": bool(payload.get("make_instrumental", False)),
    }
    logger.info("Suno generation submit attempt=1 summary=%s", summary)
    update_token(suno_auth)
    token = _suno_token()
    submitted_cookie = suno_auth.get_cookie()
    try:
        response = await generate_music(payload, token, submitted_cookie)
        logger.info(
            "Suno generation accepted attempt=1 request_id=%s clip_count=%s",
            response.get("id") if isinstance(response, dict) else None,
            len(response.get("clips") or []) if isinstance(response, dict) else 0,
        )
        return response, token
    except SunoAPIError as exc:
        logger.error(
            "Suno generation rejected attempt=1 status=%s error_type=%s "
            "detail=%r response_body=%r summary=%s",
            exc.status_code,
            exc.error_type,
            exc.upstream_detail,
            exc.body[:2000],
            summary,
        )
        if exc.error_type != "token_validation_failed":
            raise
        token = await _refresh_suno_auth_with_warmup(submitted_cookie)
        logger.info("Suno generation submit attempt=2 after browser warmup")
        try:
            response = await generate_music(payload, token, suno_auth.get_cookie())
        except SunoAPIError as retry_exc:
            logger.error(
                "Suno generation rejected after browser warmup status=%s "
                "error_type=%s detail=%r response_body=%r",
                retry_exc.status_code,
                retry_exc.error_type,
                retry_exc.upstream_detail,
                retry_exc.body[:2000],
            )
            if retry_exc.error_type == "token_validation_failed":
                raise SunoGenerationVerificationError(
                    "Suno 로그인/생성 페이지 워밍업 후에도 음악 생성 검증이 거부됐습니다. "
                    "Suno 웹에서 직접 생성이 되는지 확인한 뒤 다시 시도해 주세요."
                ) from retry_exc
            raise
        logger.info(
            "Suno generation accepted attempt=2 request_id=%s clip_count=%s",
            response.get("id") if isinstance(response, dict) else None,
            len(response.get("clips") or []) if isinstance(response, dict) else 0,
        )
        return response, token


def _feed_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("clips", "data", "songs", "result"):
            found = _feed_items(payload.get(key))
            if found:
                return found
        if payload.get("id"):
            return [payload]
    return []


def _submitted_clips(payload: Any) -> tuple[str | None, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        return None, []
    clips = payload.get("clips")
    if not isinstance(clips, list):
        clips = _feed_items(payload)
    return payload.get("id"), [item for item in clips if isinstance(item, dict)]


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=300) as response:
        destination.write_bytes(response.read())


async def run_track_generation(
    job_id: str,
    track_id: str,
    mode: str,
    download_audio: bool,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> None:
    try:
        set_job_running(job_id)
        track = db.get_one("tracks", track_id)
        if not track:
            raise ValueError("Track not found")
        if mode == "custom" and not track["lyrics"].strip():
            raise ValueError("Custom mode requires saved lyrics")
        if mode == "custom" and not track["style_prompt"].strip():
            raise ValueError("Custom mode requires an English style prompt")
        logger.info(
            "Track generation started job_id=%s track_id=%s mode=%s model=%s "
            "lyrics_length=%s style_length=%s negative_tags_length=%s",
            job_id,
            track_id,
            mode,
            track["model"],
            len(track["lyrics"]),
            len(track["style_prompt"]),
            len(track["negative_tags"]),
        )
        db.update(
            "tracks",
            track_id,
            {"status": "submitted", "updated_at": db.now_iso()},
        )
        if mode == "description":
            payload = {
                "gpt_description_prompt": track["style_prompt"] or track["concept"],
                "make_instrumental": track["instrumental"],
                "mv": track["model"],
                "prompt": "",
            }
        else:
            payload = {
                "prompt": track["lyrics"],
                "mv": track["model"],
                "title": track["title"],
                "tags": track["style_prompt"],
                "negative_tags": track["negative_tags"],
                "continue_at": None,
                "continue_clip_id": None,
            }
        submitted, token = await _submit_suno_generation(payload)
        request_id, clips = _submitted_clips(submitted)
        if not clips:
            raise RuntimeError("Suno response did not contain clips")
        generation_ids: dict[str, str] = {}
        for clip in clips:
            clip_id = str(clip.get("id") or "")
            if not clip_id:
                continue
            generation = db.insert(
                "generations",
                {
                    "id": db.new_id(),
                    "track_id": track_id,
                    "job_id": job_id,
                    "request_id": request_id,
                    "clip_id": clip_id,
                    "status": str(clip.get("status") or "submitted"),
                    "title": str(clip.get("title") or track["title"]),
                    "audio_url": clip.get("audio_url"),
                    "image_url": clip.get("image_url"),
                    "local_audio_path": None,
                    "generated_lyrics": None,
                    "tags": None,
                    "raw_response_json": db.encode_json(clip),
                    "is_selected": 0,
                    "created_at": db.now_iso(),
                    "completed_at": None,
                },
            )
            generation_ids[clip_id] = generation["id"]
        if not generation_ids:
            raise RuntimeError("Suno clips did not contain IDs")
        logger.info(
            "Track generation submitted job_id=%s track_id=%s request_id=%s "
            "clip_ids=%s",
            job_id,
            track_id,
            request_id,
            list(generation_ids),
        )
        deadline = time.monotonic() + timeout_seconds
        completed: set[str] = set()
        while time.monotonic() < deadline and len(completed) < len(generation_ids):
            feed = await get_feed(",".join(generation_ids), token)
            for item in _feed_items(feed):
                clip_id = str(item.get("id") or "")
                generation_id = generation_ids.get(clip_id)
                if not generation_id:
                    continue
                status = str(item.get("status") or "unknown")
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                audio_url = str(item.get("audio_url") or "")
                values = {
                    "status": status,
                    "title": str(item.get("title") or track["title"]),
                    "audio_url": audio_url or None,
                    "image_url": item.get("image_url"),
                    "generated_lyrics": metadata.get("prompt"),
                    "tags": metadata.get("tags"),
                    "raw_response_json": db.encode_json(item),
                }
                if status.lower() in {"error", "failed"}:
                    db.update("generations", generation_id, values)
                    raise RuntimeError(f"Suno generation failed for clip {clip_id}")
                if audio_url:
                    values["completed_at"] = db.now_iso()
                    completed.add(clip_id)
                db.update("generations", generation_id, values)
            set_job_progress(
                job_id,
                15 + int(70 * len(completed) / max(1, len(generation_ids))),
            )
            if len(completed) < len(generation_ids):
                await asyncio.sleep(poll_interval_seconds)
        if len(completed) < len(generation_ids):
            raise TimeoutError("Suno generation timed out")
        if download_audio:
            album_id = track["album_id"]
            for clip_id, generation_id in generation_ids.items():
                generation = db.get_one("generations", generation_id)
                if not generation or not generation.get("audio_url"):
                    continue
                relative = (
                    Path("albums")
                    / album_id
                    / "tracks"
                    / track_id
                    / f"{generation_id}.mp3"
                )
                destination = db.STORAGE_DIR / relative
                await asyncio.to_thread(_download, generation["audio_url"], destination)
                db.update(
                    "generations",
                    generation_id,
                    {"local_audio_path": str(relative).replace("\\", "/")},
                )
                create_asset(
                    album_id=album_id,
                    track_id=track_id,
                    generation_id=generation_id,
                    asset_type="audio",
                    path=destination,
                    original_name=f"{track['title']}.mp3",
                    content_type="audio/mpeg",
                )
        db.update(
            "tracks",
            track_id,
            {"status": "complete", "updated_at": db.now_iso()},
        )
        set_job_succeeded(job_id, {"generation_ids": list(generation_ids.values())})
        logger.info(
            "Track generation completed job_id=%s track_id=%s generation_count=%s",
            job_id,
            track_id,
            len(generation_ids),
        )
    except Exception as exc:
        logger.exception(
            "Track generation failed job_id=%s track_id=%s mode=%s error_type=%s "
            "error=%s",
            job_id,
            track_id,
            mode,
            type(exc).__name__,
            exc,
        )
        db.update(
            "tracks",
            track_id,
            {"status": "failed", "updated_at": db.now_iso()},
        )
        error_code = (
            "SUNO_BROWSER_VERIFICATION_REQUIRED"
            if isinstance(exc, SunoGenerationVerificationError)
            else "SUNO_GENERATION_FAILED"
        )
        set_job_failed(job_id, exc, error_code)


def create_asset(
    *,
    album_id: str | None,
    track_id: str | None,
    generation_id: str | None,
    asset_type: str,
    path: Path,
    original_name: str,
    content_type: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    relative = path.resolve().relative_to(db.STORAGE_DIR.resolve())
    return db.insert(
        "assets",
        {
            "id": db.new_id(),
            "album_id": album_id,
            "track_id": track_id,
            "generation_id": generation_id,
            "type": asset_type,
            "storage_key": str(relative).replace("\\", "/"),
            "original_name": original_name,
            "content_type": content_type,
            "size_bytes": path.stat().st_size,
            "metadata_json": db.encode_json(metadata or {}),
            "created_at": db.now_iso(),
        },
    )


def _compact_prompt_value(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _album_thumbnail_prompt(album: dict[str, Any], tracks: list[dict[str, Any]]) -> str:
    track_titles = ", ".join(
        _compact_prompt_value(track.get("title"), 80)
        for track in tracks[:8]
        if track.get("title")
    )
    track_concepts = "; ".join(
        _compact_prompt_value(track.get("concept"), 160)
        for track in tracks[:5]
        if track.get("concept")
    )
    planned_prompt = _compact_prompt_value(
        album.get("thumbnail_image_prompt"), 1200
    )
    visual_concept = _compact_prompt_value(album.get("visual_concept"), 700)
    album_context = {
        "album_title": album.get("title"),
        "artist_name": album.get("artist_name"),
        "description": album.get("description"),
        "genre": album.get("genre"),
        "mood": album.get("mood"),
        "keywords": album.get("keywords"),
        "style_prompt": album.get("style_prompt"),
        "visual_concept": visual_concept,
        "track_titles": track_titles,
        "track_concepts": track_concepts,
    }
    context_lines = [
        f"{key}: {_compact_prompt_value(value)}"
        for key, value in album_context.items()
        if _compact_prompt_value(value)
    ]
    base = planned_prompt or (
        "Create an album-specific cinematic YouTube music playlist thumbnail "
        "background based on the album context below. The scene must clearly "
        "match the album's story, mood, genre, and recurring imagery."
    )
    return "\n".join(
        [
            base,
            "Format: 16:9 landscape thumbnail background.",
            "Composition: strong single focal scene, clean negative space for headline text, readable at small size.",
            "Visual constraints: no words, no letters, no logos, no watermark, no UI, no album-cover mockup.",
            "Avoid generic microphones, headphones, music notes, stage lights, or random stock-photo symbolism unless the album context explicitly asks for them.",
            "Album context:",
            *context_lines,
        ]
    )


def run_image_generation(
    job_id: str,
    album_id: str,
    track_id: str | None,
    instruction: str,
    aspect_ratio: str,
    candidate_count: int,
    asset_type: str = "cover",
) -> None:
    try:
        set_job_running(job_id)
        album = db.get_one("albums", album_id)
        track = db.get_one("tracks", track_id) if track_id else None
        if not album:
            raise ValueError("Album not found")
        album_tracks = db.fetch_all(
            "SELECT title, concept, image_prompt FROM tracks WHERE album_id = ? ORDER BY sequence",
            (album_id,),
        )
        if asset_type == "template_preview":
            prompt = (
                "16:9 visual design reference image for a music video template, "
                f"{album['style_prompt'] or album['genre']}"
            )
        elif asset_type == "thumbnail_background":
            prompt = _album_thumbnail_prompt(album, album_tracks)
        else:
            prompt = (
                (track or {}).get("image_prompt")
                or f"Cinematic album artwork for {album['title']}, {album['style_prompt']}"
            )
        if instruction:
            prompt = f"{prompt}\nAdditional direction: {instruction}"
        assets = []
        for index in range(candidate_count):
            raw, mime = _gemini_image(prompt, aspect_ratio)
            extension = ".png" if "png" in mime else ".jpg"
            relative = (
                Path("albums")
                / album_id
                / ("template-previews" if asset_type == "template_preview" else "images")
                / f"{job_id}-{index + 1}{extension}"
            )
            destination = db.STORAGE_DIR / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            assets.append(
                create_asset(
                    album_id=album_id,
                    track_id=track_id,
                    generation_id=None,
                    asset_type=asset_type,
                    path=destination,
                    original_name=f"{album['title']}-{index + 1}{extension}",
                    content_type=mime,
                    metadata={"prompt": prompt, "aspect_ratio": aspect_ratio},
                )
            )
            set_job_progress(job_id, int(100 * (index + 1) / candidate_count))
        set_job_succeeded(job_id, {"asset_ids": [asset["id"] for asset in assets]})
    except Exception as exc:
        set_job_failed(job_id, exc, "IMAGE_GENERATION_FAILED")


def save_uploaded_asset(
    album_id: str,
    body: bytes,
    filename: str,
    content_type: str,
    asset_type: str = "cover",
) -> dict[str, Any]:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "upload.bin"
    folder = {
        "template_preview": "template-previews",
        "thumbnail_background": "thumbnail-backgrounds",
    }.get(asset_type, "uploads")
    relative = Path("albums") / album_id / folder / f"{db.new_id()}-{safe_name}"
    destination = db.STORAGE_DIR / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    return create_asset(
        album_id=album_id,
        track_id=None,
        generation_id=None,
        asset_type=asset_type,
        path=destination,
        original_name=filename,
        content_type=content_type or "application/octet-stream",
    )


def _hex_color_to_rgb(value: str) -> tuple[int, int, int]:
    text = str(value or "#ffffff").strip().lower().replace("0x", "").replace("#", "")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    text = (text + "ffffff")[:6]
    try:
        number = int(text, 16)
    except ValueError:
        number = 0xFFFFFF
    return (number >> 16) & 255, (number >> 8) & 255, number & 255


def _render_fake_equalizer_overlay(
    *,
    ffmpeg: str,
    audio_path: Path,
    output_path: Path,
    width: int,
    height: int,
    duration: float | None,
    color: str,
    opacity: float,
    bar_count: int,
    gap: int,
) -> None:
    sample_rate = 16_000
    fps = 30
    decoded = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(audio_path),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=7200,
    )
    if decoded.returncode != 0:
        stderr = decoded.stderr.decode("utf-8", errors="ignore")[-1200:]
        raise RuntimeError(f"오디오 분석용 PCM 변환에 실패했습니다. {stderr}")

    raw = decoded.stdout[: len(decoded.stdout) - (len(decoded.stdout) % 4)]
    samples = array.array("f")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        samples = array.array("f", [0.0] * sample_rate)

    audio_duration = len(samples) / sample_rate
    render_duration = max(0.1, float(duration or audio_duration or 0.1))
    frame_count = max(1, int(math.ceil(render_duration * fps)))
    window_size = max(256, sample_rate // 12)
    half_window = window_size // 2
    rms_values: list[float] = []
    for frame in range(frame_count):
        center = int((frame / fps) * sample_rate)
        start = max(0, center - half_window)
        end = min(len(samples), center + half_window)
        if end <= start:
            rms_values.append(0.0)
            continue
        total = 0.0
        for sample in samples[start:end]:
            total += float(sample) * float(sample)
        rms_values.append(math.sqrt(total / (end - start)))

    sorted_rms = sorted(rms_values)
    percentile_index = max(0, min(len(sorted_rms) - 1, int(len(sorted_rms) * 0.95)))
    reference = max(0.0001, sorted_rms[percentile_index])
    bar_count = max(1, min(16, int(bar_count or 5)))
    gap = max(0, min(max(0, width // max(1, bar_count) - 1), int(gap or 0)))
    bar_width = max(1, (width - gap * (bar_count - 1)) // bar_count)
    used_width = bar_width * bar_count + gap * (bar_count - 1)
    left_pad = max(0, (width - used_width) // 2)
    rgb = _hex_color_to_rgb(color)
    alpha = max(0, min(255, int(255 * max(0.0, min(1.0, opacity)))))
    # Fixed visual weights keep all bars alive while still making them feel distinct.
    weights = [0.62, 1.0, 0.74, 0.9, 0.68, 0.82, 0.58, 0.76]
    phases = [0.0, 1.7, 3.1, 4.4, 5.6, 2.4, 0.9, 3.8]

    def fill_rounded_bar(
        pixels: bytearray,
        *,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        radius: int,
    ) -> None:
        radius = max(0, min(radius, (x1 - x0) // 2, (y1 - y0) // 2))
        radius_sq = radius * radius
        for y in range(y0, y1):
            row = y * width * 4
            for x in range(x0, x1):
                if radius:
                    cx = x0 + radius if x < x0 + radius else x1 - radius - 1 if x >= x1 - radius else x
                    cy = y0 + radius if y < y0 + radius else y1 - radius - 1 if y >= y1 - radius else y
                    dx = x - cx
                    dy = y - cy
                    if dx * dx + dy * dy > radius_sq:
                        continue
                pos = row + x * 4
                pixels[pos] = rgb[0]
                pixels[pos + 1] = rgb[1]
                pixels[pos + 2] = rgb[2]
                pixels[pos + 3] = alpha

    output_path.parent.mkdir(parents=True, exist_ok=True)
    encoder = subprocess.Popen(
        [
            ffmpeg,
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "-",
            "-an",
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            str(output_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert encoder.stdin is not None
    previous_level = 0.0
    try:
        for frame, rms in enumerate(rms_values):
            level = min(1.0, (rms / reference) ** 0.45)
            previous_level = previous_level * 0.68 + level * 0.32
            pixels = bytearray(width * height * 4)
            for index in range(bar_count):
                pulse = 0.88 + 0.12 * math.sin(frame * 0.19 + phases[index % len(phases)])
                bar_level = max(0.12, min(1.0, previous_level * weights[index % len(weights)] * pulse))
                bar_height = max(2, int(height * bar_level))
                x0 = left_pad + index * (bar_width + gap)
                x1 = min(width, x0 + bar_width)
                y0 = max(0, height - bar_height)
                fill_rounded_bar(
                    pixels,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=height,
                    radius=max(2, min(10, bar_width // 3)),
                )
            encoder.stdin.write(pixels)
    except BrokenPipeError as exc:
        raise RuntimeError("이퀄라이저 오버레이 인코딩 파이프가 중단되었습니다.") from exc
    finally:
        encoder.stdin.close()
    assert encoder.stderr is not None
    stderr_bytes = encoder.stderr.read()
    encoder.wait(timeout=7200)
    if encoder.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="ignore")[-1200:]
        raise RuntimeError(f"이퀄라이저 오버레이 생성에 실패했습니다. {stderr}")


def run_video_render(job_id: str, album_id: str, request: Any) -> None:
    output_path: Path | None = None
    temporary_visualizer_path: Path | None = None
    try:
        set_job_running(job_id)
        if request.mode != "static_loop":
            raise ValueError("MVP currently supports static_loop only")
        track = db.get_one("tracks", request.track_id)
        image_asset = db.get_one("assets", request.image_asset_id)
        if not track or track["album_id"] != album_id:
            raise ValueError("Track not found in album")
        if not image_asset or image_asset["album_id"] != album_id:
            raise ValueError("Image asset not found in album")
        generation = (
            db.get_one("generations", request.generation_id)
            if request.generation_id
            else db.get_one("generations", track["selected_generation_id"])
            if track.get("selected_generation_id")
            else db.fetch_one(
                """
                SELECT * FROM generations
                 WHERE track_id = ? AND local_audio_path IS NOT NULL
                 ORDER BY created_at DESC LIMIT 1
                """,
                (track["id"],),
            )
        )
        if generation and generation["track_id"] != track["id"]:
            raise ValueError("Generation not found in track")
        if not generation or not generation.get("local_audio_path"):
            raise ValueError("Select or download a generated audio candidate first")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg was not found on PATH")
        ffprobe = shutil.which("ffprobe")
        image_path = db.STORAGE_DIR / image_asset["storage_key"]
        audio_path = db.STORAGE_DIR / generation["local_audio_path"]
        width, height = request.resolution.lower().split("x", 1)
        relative = Path("albums") / album_id / "video" / f"{job_id}.mp4"
        output_path = db.STORAGE_DIR / relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        compose = (image_asset.get("metadata") or {}).get("compose") or {}
        video_filters = [
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        ]
        if request.loop_motion == "slow_zoom":
            video_filters = [
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                "zoompan=z='min(zoom+0.0004,1.08)':d=1:s="
                f"{width}x{height}:fps=30"
            ]
        brightness = float(compose.get("brightness", 0) or 0)
        contrast = 1 + float(compose.get("contrast", 0) or 0)
        saturation = 1 + float(compose.get("saturation", 0) or 0)
        video_filters.append(
            f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}"
        )
        blur = float(compose.get("blur", 0) or 0)
        if blur > 0:
            video_filters.append(f"boxblur={min(20, blur)}")
        overlay_color = str(compose.get("overlay_color") or "#21000f").replace(
            "#", "0x"
        )
        overlay_opacity = float(compose.get("overlay_opacity", 0) or 0)
        if overlay_opacity > 0:
            video_filters.append(
                f"drawbox=x=0:y=0:w=iw:h=ih:color={overlay_color}@{overlay_opacity}:t=fill"
            )
        font_family = str(compose.get("font_family") or "malgun")
        text_color = str(compose.get("text_color") or "#ffffff").replace("#", "0x")
        title_x = _percent_position(compose.get("title_x"), 18)
        title_y = _percent_position(compose.get("title_y"), 82)
        title_size = max(24, min(240, int(compose.get("title_size", 72) or 72)))
        artist_x = _percent_position(compose.get("artist_x"), 18)
        artist_y = _percent_position(compose.get("artist_y"), 88)
        artist_font_family = str(compose.get("artist_font_family") or font_family)
        artist_color = str(compose.get("artist_color") or "#ffffff").replace(
            "#", "0x"
        )
        artist_size = max(12, min(120, int(compose.get("artist_size", 28) or 28)))
        title = str(compose.get("title") or (track["title"] if request.show_title else ""))
        font_path = _video_text_font_path(font_family, title)
        if title and request.show_title:
            safe_title = _ffmpeg_text(title)
            if font_path:
                video_filters.append(
                    "drawtext="
                    f"fontfile='{_ffmpeg_filter_path(font_path)}':"
                    f"text='{safe_title}':fontcolor={text_color}:fontsize={title_size}:"
                    f"x=w*{title_x / 100}-text_w/2:y=h*{title_y / 100}-text_h/2:"
                    "shadowcolor=black@0.7:shadowx=2:shadowy=2"
                )
            else:
                logger.warning(
                    "Video title omitted because no usable font was found job_id=%s",
                    job_id,
                )
        artist_name = str(compose.get("artist_name") or "")
        artist_font_path = _video_text_font_path(artist_font_family, artist_name)
        if artist_name and request.show_title and artist_font_path:
            video_filters.append(
                "drawtext="
                f"fontfile='{_ffmpeg_filter_path(artist_font_path)}':"
                f"text='{_ffmpeg_text(artist_name)}':fontcolor={artist_color}@0.78:"
                f"fontsize={artist_size}:x=w*{artist_x / 100}-text_w/2:"
                f"y=h*{artist_y / 100}-text_h/2:"
                "shadowcolor=black@0.7:shadowx=1:shadowy=1"
            )
        icon = str(compose.get("icon") or "")
        icon_image = str(compose.get("icon_image") or "")
        icon_x = _percent_position(compose.get("icon_x"), 50)
        icon_y = _percent_position(compose.get("icon_y"), 18)
        icon_size = max(16, min(240, int(compose.get("icon_size", 64) or 64)))
        icon_image_path = resolve_video_icon(icon_image) if icon_image else None
        if icon_image and not icon_image_path:
            raise ValueError(
                f"Selected video icon was not found in {VIDEO_ICON_DIR}: {icon_image}"
            )
        temporary_icon_path = None
        if icon_image_path and icon_image_path.suffix.lower() == ".svg":
            temporary_icon_path = output_path.parent / f"{job_id}-icon.png"
            icon_image_path = rasterize_svg_icon(
                icon_image_path,
                temporary_icon_path,
                icon_size,
            )
        icon_font_path = _video_text_font_path(font_family, icon)
        if icon and not icon_image_path and icon_font_path:
            video_filters.append(
                "drawtext="
                f"fontfile='{_ffmpeg_filter_path(icon_font_path)}':"
                f"text='{_ffmpeg_text(icon)}':fontcolor={text_color}:fontsize={icon_size}:"
                f"x=w*{icon_x / 100}-text_w/2:y=h*{icon_y / 100}-text_h/2:"
                "shadowcolor=black@0.6:shadowx=2:shadowy=2"
            )
        duration = None
        if ffprobe:
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(audio_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            try:
                duration = float(probe.stdout.strip())
            except ValueError:
                duration = None
        if request.fade_in_seconds > 0:
            video_filters.append(f"fade=t=in:st=0:d={request.fade_in_seconds}")
        if duration and request.fade_out_seconds > 0:
            video_filters.append(
                f"fade=t=out:st={max(0, duration - request.fade_out_seconds)}:"
                f"d={request.fade_out_seconds}"
            )
        video_filters.append("format=yuv420p")
        base_filter = ",".join(video_filters)
        command = [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(image_path),
            "-i",
            str(audio_path),
        ]
        if icon_image_path:
            command.extend(["-loop", "1", "-i", str(icon_image_path)])
            icon_overlay_x = f"W*{icon_x / 100}-w/2"
            icon_overlay_y = f"H*{icon_y / 100}-h/2"
            background_filter = (
                f"[0:v]{base_filter}[base];"
                f"[2:v]scale={icon_size}:{icon_size}:"
                "force_original_aspect_ratio=decrease,format=rgba[icon];"
                f"[base][icon]overlay={icon_overlay_x}:{icon_overlay_y}:"
                "format=auto:eof_action=repeat[bg]"
            )
        else:
            background_filter = f"[0:v]{base_filter}[bg]"
        if request.show_visualizer:
            visualizer_style = str(
                compose.get("visualizer_style") or request.visualizer_style or "bars"
            )
            visualizer_width = max(
                120,
                int(
                    int(width)
                    * float(
                        compose.get("visualizer_width")
                        or getattr(request, "visualizer_width", 18)
                        or 18
                    )
                    / 100
                ),
            )
            waveform_height = max(
                30,
                min(
                    int(height),
                    int(
                        compose.get("visualizer_height")
                        or getattr(request, "visualizer_height", 90)
                        or 90
                    ),
                ),
            )
            visualizer_x = _percent_position(
                compose.get("visualizer_x"),
                getattr(request, "visualizer_x", 88),
            )
            visualizer_y = _percent_position(
                compose.get("visualizer_y"),
                getattr(request, "visualizer_y", 82),
            )
            visualizer_color = str(
                getattr(request, "visualizer_color", "")
                or compose.get("text_color")
                or "#ffffff"
            ).replace("#", "0x")
            visualizer_opacity = max(
                0.0,
                min(1.0, float(getattr(request, "visualizer_opacity", 0.82) or 0.82)),
            )
            overlay_x = f"W*{visualizer_x / 100}-w/2"
            overlay_y = f"H*{visualizer_y / 100}-h/2"
            if visualizer_style == "wave":
                visualizer_filter = (
                    f"showwaves=s={visualizer_width}x{waveform_height}:"
                    f"mode=line:colors={visualizer_color}@{visualizer_opacity:.3f},"
                    "format=rgba,colorkey=0x000000:0.05:0"
                )
            elif visualizer_style == "dots":
                visualizer_filter = (
                    f"showwaves=s={visualizer_width}x{waveform_height}:"
                    f"mode=point:colors={visualizer_color}@{visualizer_opacity:.3f},"
                    "format=rgba,colorkey=0x000000:0.05:0"
                )
                filter_complex = (
                    f"{background_filter};"
                    f"[1:a]asplit=2[aout][vis];"
                    f"[vis]{visualizer_filter}[wave];"
                    f"[bg][wave]overlay={overlay_x}:{overlay_y}[vout]"
                )
                command.extend(
                    [
                        "-filter_complex",
                        filter_complex,
                        "-map",
                        "[vout]",
                        "-map",
                        "[aout]",
                    ]
                )
            else:
                bar_count = max(
                    1,
                    min(16, int(getattr(request, "visualizer_bar_count", 5) or 5)),
                )
                temporary_visualizer_path = output_path.parent / f"{job_id}-visualizer.mov"
                _render_fake_equalizer_overlay(
                    ffmpeg=ffmpeg,
                    audio_path=audio_path,
                    output_path=temporary_visualizer_path,
                    width=visualizer_width,
                    height=waveform_height,
                    duration=duration,
                    color=visualizer_color,
                    opacity=visualizer_opacity,
                    bar_count=bar_count,
                    gap=int(getattr(request, "visualizer_gap", 0) or 0),
                )
                visualizer_input_index = 3 if icon_image_path else 2
                command.extend(["-stream_loop", "-1", "-i", str(temporary_visualizer_path)])
                filter_complex = (
                    f"{background_filter};"
                    f"[{visualizer_input_index}:v]format=rgba[wave];"
                    f"[bg][wave]overlay={overlay_x}:{overlay_y}:format=auto[vout]"
                )
                command.extend(
                    [
                        "-filter_complex",
                        filter_complex,
                        "-map",
                        "[vout]",
                        "-map",
                        "1:a",
                    ]
                )
        elif icon_image_path:
            command.extend(
                [
                    "-filter_complex",
                    background_filter.replace("[bg]", "[vout]"),
                    "-map",
                    "[vout]",
                    "-map",
                    "1:a",
                ]
            )
        else:
            command.extend(["-vf", base_filter, "-map", "0:v", "-map", "1:a"])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        logger.info(
            "Video rendering started job_id=%s album_id=%s track_id=%s "
            "resolution=%sx%s visualizer=%s motion=%s duration=%s",
            job_id,
            album_id,
            track["id"],
            width,
            height,
            request.show_visualizer,
            request.loop_motion,
            duration,
        )
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=7200,
        )
        if temporary_icon_path:
            temporary_icon_path.unlink(missing_ok=True)
        if temporary_visualizer_path:
            temporary_visualizer_path.unlink(missing_ok=True)
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stderr_tail = stderr[-4000:] if stderr else "FFmpeg did not provide stderr output."
            logger.error(
                "Video rendering FFmpeg failed job_id=%s returncode=%s stderr=%r",
                job_id,
                completed.returncode,
                stderr_tail,
            )
            raise RuntimeError(
                f"FFmpeg 영상 렌더링에 실패했습니다 (종료 코드 {completed.returncode}). "
                f"{stderr_tail[-1200:]}"
            )
        video_metadata = _model_dump(request)
        if duration:
            video_metadata["duration_seconds"] = duration
        asset = create_asset(
            album_id=album_id,
            track_id=track["id"],
            generation_id=generation["id"],
            asset_type="video",
            path=output_path,
            original_name=f"{generation.get('title') or track['title']}.mp4",
            content_type="video/mp4",
            metadata=video_metadata,
        )
        set_job_succeeded(job_id, {"asset_id": asset["id"]})
        logger.info(
            "Video rendering completed job_id=%s asset_id=%s output=%s",
            job_id,
            asset["id"],
            output_path,
        )
    except Exception as exc:
        if output_path and output_path.exists():
            output_path.unlink(missing_ok=True)
        if temporary_visualizer_path and temporary_visualizer_path.exists():
            temporary_visualizer_path.unlink(missing_ok=True)
        logger.exception(
            "Video rendering failed job_id=%s album_id=%s error_type=%s error=%s",
            job_id,
            album_id,
            type(exc).__name__,
            exc,
        )
        set_job_failed(job_id, exc, "VIDEO_RENDER_FAILED")


def run_batch_video_render(
    job_id: str,
    album_id: str,
    request: schemas.BatchVideoRenderRequest,
) -> None:
    try:
        set_job_running(job_id)
        album = db.get_one("albums", album_id)
        if not album:
            raise ValueError("Album not found")
        work_items: list[tuple[str, str | None]] = []
        for generation_id in dict.fromkeys(request.generation_ids):
            generation = db.get_one("generations", generation_id)
            if generation:
                work_items.append((generation["track_id"], generation_id))
        if not work_items:
            work_items = [
                (track_id, None)
                for track_id in dict.fromkeys(request.track_ids)
            ]
        template = (
            db.get_one("video_templates", request.template_id)
            if request.template_id
            else None
        )
        shared_existing = (
            db.get_one("assets", request.shared_image_asset_id)
            if request.shared_image_asset_id
            else None
        )
        if shared_existing and (
            shared_existing["album_id"] != album_id
            or shared_existing["type"] not in ("cover", "composed_image")
        ):
            raise ValueError("Shared image asset not found in album")

        completed: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        activity_tracks = [
            {
                "track_id": track_id,
                "generation_id": generation_id,
                "title": (
                    db.get_one("generations", generation_id) or {}
                ).get("title")
                or (db.get_one("tracks", track_id) or {}).get("title", track_id),
                "status": "waiting",
                "message": "대기 중",
            }
            for track_id, generation_id in work_items
        ]
        shared_generated: dict[str, Any] | None = None

        def publish(index: int, status: str, message: str) -> None:
            activity_tracks[index]["status"] = status
            activity_tracks[index]["message"] = message
            set_job_activity(
                job_id,
                {
                    "current_index": index,
                    "total": len(work_items),
                    "tracks": activity_tracks,
                },
            )

        def latest_track_image(track_id: str) -> dict[str, Any] | None:
            assets = db.fetch_all(
                """
                SELECT * FROM assets
                 WHERE album_id = ? AND track_id = ?
                   AND type IN ('cover', 'composed_image')
                 ORDER BY created_at DESC
                """,
                (album_id, track_id),
            )
            return next(
                (
                    asset
                    for asset in assets
                    if not (asset.get("metadata") or {}).get("render_frame")
                ),
                None,
            )

        def saved_edit_image(track_id: str) -> dict[str, Any] | None:
            assets = db.fetch_all(
                """
                SELECT * FROM assets
                 WHERE album_id = ? AND track_id = ?
                   AND type IN ('cover', 'composed_image')
                 ORDER BY created_at DESC
                """,
                (album_id, track_id),
            )
            return next(
                (
                    asset
                    for asset in assets
                    if isinstance((asset.get("metadata") or {}).get("compose"), dict)
                    and not (asset.get("metadata") or {}).get("render_frame")
                ),
                None,
            )

        def generate_image(
            target_track_id: str | None,
            instruction: str,
            index: int,
        ) -> dict[str, Any]:
            attempts = 2 if request.retry_image_failures else 1
            last_error = "Image generation failed"
            for attempt in range(attempts):
                publish(
                    index,
                    "image_generating",
                    "이미지를 만들고 있어요"
                    if attempt == 0
                    else "이미지 생성을 한 번 더 시도하고 있어요",
                )
                image_job = create_job(
                    "cover_generate",
                    "track" if target_track_id else "album",
                    target_track_id or album_id,
                    {
                        "batch_job_id": job_id,
                        "candidate_count": request.candidate_count,
                    },
                )
                run_image_generation(
                    image_job["id"],
                    album_id,
                    target_track_id,
                    instruction,
                    "16:9",
                    request.candidate_count,
                )
                image_job = db.get_one("jobs", image_job["id"])
                if image_job and image_job["status"] == "succeeded":
                    asset_ids = (image_job.get("result") or {}).get("asset_ids") or []
                    if asset_ids:
                        asset = db.get_one("assets", str(asset_ids[0]))
                        if asset:
                            return asset
                last_error = (
                    (image_job or {}).get("error_message")
                    or "Image generation failed"
                )
            raise RuntimeError(last_error)

        def template_compose(
            track: dict[str, Any],
            generation: dict[str, Any] | None,
        ) -> dict[str, Any]:
            if not template:
                raise ValueError("공통 템플릿이 선택되지 않았습니다.")
            compose = {
                **_model_dump(schemas.ImageComposeRequest()),
                **(template.get("compose") or {}),
            }
            if (template.get("title_source") or "track") == "track":
                compose["title_anchor_text"] = str(compose.get("title") or "")
                compose["title"] = (generation or {}).get("title") or track["title"]
            elif template.get("title_source") == "hidden":
                compose["title"] = ""
            if (template.get("artist_source") or "album") == "album":
                compose["artist_name"] = album.get("artist_name") or ""
            elif template.get("artist_source") == "hidden":
                compose["artist_name"] = ""
            return compose

        for index, (track_id, generation_id) in enumerate(work_items):
            track = db.get_one("tracks", track_id)
            generation = (
                db.get_one("generations", generation_id)
                if generation_id
                else None
            )
            try:
                if not track or track["album_id"] != album_id:
                    raise ValueError("Track not found in album")
                if generation and generation["track_id"] != track_id:
                    raise ValueError("Generation not found in track")
                target_generation_id = generation_id or track.get("selected_generation_id")
                target_title = (generation or {}).get("title") or track["title"]
                existing_video = db.fetch_one(
                    """
                    SELECT * FROM assets
                     WHERE album_id = ? AND track_id = ? AND type = 'video'
                       AND generation_id IS ?
                     ORDER BY created_at DESC LIMIT 1
                    """,
                    (album_id, track_id, target_generation_id),
                )
                if existing_video and not request.overwrite_existing:
                    publish(index, "skipped", "이미 완성된 영상이 있어 건너뛰었어요")
                    skipped.append(
                        {
                            "track_id": track_id,
                            "title": target_title,
                            "reason": "video already exists",
                        }
                    )
                    continue

                saved_asset = saved_edit_image(track_id)
                if request.edit_mode == "template_only":
                    compose = template_compose(track, generation)
                    edit_source = "template"
                elif saved_asset:
                    compose = {
                        **_model_dump(schemas.ImageComposeRequest()),
                        **((saved_asset.get("metadata") or {}).get("compose") or {}),
                    }
                    edit_source = "saved"
                elif request.edit_mode == "saved_only" or request.missing_edit_action == "exclude":
                    publish(index, "skipped", "저장된 편집이 없어 제외했어요")
                    skipped.append(
                        {
                            "track_id": track_id,
                            "title": track["title"],
                            "reason": "saved edit not found",
                        }
                    )
                    continue
                else:
                    compose = template_compose(track, generation)
                    edit_source = "template"

                instructions = "\n".join(
                    value
                    for value in [
                        (template or {}).get("image_instruction") or "",
                        request.image_instruction,
                    ]
                    if value
                )
                if request.image_mode == "shared_existing":
                    if not shared_existing:
                        raise ValueError("공통 기존 이미지가 선택되지 않았습니다.")
                    image_asset = shared_existing
                    image_source = "shared_existing"
                elif request.image_mode == "generate_shared":
                    if shared_generated is None:
                        shared_generated = generate_image(None, instructions, index)
                    image_asset = shared_generated
                    image_source = "shared_generated"
                elif request.image_mode == "selected_then_generate_per_track":
                    image_asset = latest_track_image(track_id)
                    if image_asset:
                        publish(index, "image_ready", "기존 곡 이미지를 사용해요")
                        image_source = "selected"
                    else:
                        image_asset = generate_image(track_id, instructions, index)
                        image_source = "generated"
                else:
                    image_asset = generate_image(track_id, instructions, index)
                    image_source = "generated"

                publish(index, "template_applying", "편집 디자인을 이미지에 적용하고 있어요")
                original_metadata = dict(image_asset.get("metadata") or {})
                metadata = dict(original_metadata)
                metadata["compose"] = compose
                metadata["batch_edit_source"] = edit_source
                if template:
                    metadata["video_template_id"] = template["id"]
                db.update(
                    "assets",
                    image_asset["id"],
                    {"metadata_json": db.encode_json(metadata)},
                )

                frame_relative = (
                    Path("albums")
                    / album_id
                    / "video-frames"
                    / f"{job_id}-{target_generation_id or track_id}.png"
                )
                frame_path = db.STORAGE_DIR / frame_relative
                logger.info(
                    "Batch video frame composition starting batch_job_id=%s "
                    "track_id=%s track_title=%r image_asset_id=%s "
                    "image_storage_key=%s edit_source=%s image_source=%s "
                    "template_id=%s frame_path=%s",
                    job_id,
                    track_id,
                    track.get("title"),
                    image_asset.get("id"),
                    image_asset.get("storage_key"),
                    edit_source,
                    image_source,
                    (template or {}).get("id"),
                    frame_path,
                )
                render_static_video_frame(
                    db.STORAGE_DIR / image_asset["storage_key"],
                    frame_path,
                    compose,
                )
                validate_static_video_frame(
                    db.STORAGE_DIR / image_asset["storage_key"],
                    frame_path,
                )
                frame_asset = create_asset(
                    album_id=album_id,
                    track_id=track_id,
                    generation_id=target_generation_id,
                    asset_type="composed_image",
                    path=frame_path,
                    original_name=f"loop-render-frame-{track_id}.png",
                    content_type="image/png",
                    metadata={
                        "render_frame": True,
                        "source_image_asset_id": image_asset["id"],
                        "batch_job_id": job_id,
                    },
                )
                render_request = schemas.VideoRenderRequest(
                    track_id=track_id,
                    generation_id=target_generation_id,
                    image_asset_id=frame_asset["id"],
                    show_title=False,
                    show_visualizer=bool(compose.get("show_visualizer", True)),
                    visualizer_style=str(
                        compose.get("visualizer_style") or "bars"
                    ),
                    visualizer_x=_percent_position(
                        compose.get("visualizer_x"), 88
                    ),
                    visualizer_y=_percent_position(
                        compose.get("visualizer_y"), 82
                    ),
                    visualizer_width=max(
                        5,
                        min(80, float(compose.get("visualizer_width", 18) or 18)),
                    ),
                    visualizer_height=max(
                        30,
                        min(500, int(compose.get("visualizer_height", 90) or 90)),
                    ),
                    visualizer_color=str(
                        compose.get("text_color") or "#ffffff"
                    ),
                    visualizer_bar_count=5,
                    visualizer_gap=8,
                    visualizer_bars=[7, 18, 11, 15, 9],
                )
                render_job = create_job(
                    "video_render",
                    "track",
                    track_id,
                    {
                        "batch_job_id": job_id,
                        "generation_id": target_generation_id,
                        "template_id": (template or {}).get("id"),
                        "image_asset_id": frame_asset["id"],
                        "source_image_asset_id": image_asset["id"],
                    },
                )
                publish(index, "rendering", "이제 영상을 렌더링하고 있어요")
                run_video_render(
                    render_job["id"],
                    album_id,
                    render_request,
                )
                render_job = db.get_one("jobs", render_job["id"])
                if not render_job or render_job["status"] != "succeeded":
                    raise RuntimeError(
                        (render_job or {}).get("error_message")
                        or "Video rendering failed"
                    )
                video_asset_id = str(
                    (render_job.get("result") or {}).get("asset_id") or ""
                )
                video_asset = db.get_one("assets", video_asset_id)
                if video_asset:
                    video_metadata = dict(video_asset.get("metadata") or {})
                    video_metadata.update(
                        {
                            "source_image_asset_id": image_asset["id"],
                            "compose": compose,
                            "batch_job_id": job_id,
                            "batch_edit_source": edit_source,
                            "video_template_id": (template or {}).get("id"),
                        }
                    )
                    db.update(
                        "assets",
                        video_asset_id,
                        {"metadata_json": db.encode_json(video_metadata)},
                    )
                completed.append(
                    {
                        "track_id": track_id,
                        "generation_id": target_generation_id or "",
                        "title": target_title,
                        "edit_source": edit_source,
                        "image_source": image_source,
                        "image_asset_id": image_asset["id"],
                        "video_asset_id": video_asset_id,
                    }
                )
                publish(index, "completed", "영상이 완성됐어요")
            except Exception as track_exc:
                logger.exception(
                    "Batch video track failed batch_job_id=%s track_id=%s error=%s",
                    job_id,
                    track_id,
                    track_exc,
                )
                failed.append(
                    {
                        "track_id": track_id,
                        "title": (track or {}).get("title") or track_id,
                        "error": str(track_exc),
                    }
                )
                publish(index, "failed", str(track_exc))
                if not request.continue_on_error:
                    raise
            finally:
                set_job_progress(
                    job_id,
                    int(100 * (index + 1) / max(1, len(work_items))),
                )

        set_job_succeeded(
            job_id,
            {
                "completed": completed,
                "failed": failed,
                "skipped": skipped,
            },
        )
    except Exception as exc:
        logger.exception(
            "Batch video rendering failed job_id=%s album_id=%s error=%s",
            job_id,
            album_id,
            exc,
        )
        set_job_failed(job_id, exc, "BATCH_VIDEO_RENDER_FAILED")


def _probe_media_duration(ffprobe: str, path: Path) -> float:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        duration = float((completed.stdout or "").strip())
    except ValueError as exc:
        raise RuntimeError(f"Could not read video duration: {path.name}") from exc
    if completed.returncode != 0 or duration <= 0:
        raise RuntimeError(f"Could not read video duration: {path.name}")
    return duration


def probe_video_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe was not found on PATH")
    if not path.is_file():
        raise FileNotFoundError(f"Video file was not found: {path.name}")
    return _probe_media_duration(ffprobe, path)


def run_album_video_render(
    job_id: str,
    album_id: str,
    request: schemas.AlbumVideoRenderRequest,
) -> None:
    output_path: Path | None = None
    try:
        set_job_running(job_id)
        album = db.get_one("albums", album_id)
        if not album:
            raise ValueError("Album not found")
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise RuntimeError("ffmpeg and ffprobe must be available on PATH")

        assets: list[dict[str, Any]] = []
        paths: list[Path] = []
        durations: list[float] = []
        for index, asset_id in enumerate(request.video_asset_ids):
            asset = db.get_one("assets", asset_id)
            if (
                not asset
                or asset["album_id"] != album_id
                or asset["type"] != "video"
                or not asset.get("track_id")
            ):
                raise ValueError("Track video not found in album")
            path = db.STORAGE_DIR / asset["storage_key"]
            if not path.is_file():
                raise FileNotFoundError(f"Track video file was not found: {asset['original_name']}")
            assets.append(asset)
            paths.append(path)
            durations.append(_probe_media_duration(ffprobe, path))
            set_job_progress(job_id, min(20, round(20 * (index + 1) / len(request.video_asset_ids))))

        if request.repeat_count > 1:
            assets = assets * request.repeat_count
            paths = paths * request.repeat_count
            durations = durations * request.repeat_count

        width_text, height_text = request.resolution.split("x", 1)
        width, height = int(width_text), int(height_text)
        fade_seconds = request.transition_seconds if request.transition == "fade" else 0
        filters: list[str] = []
        concat_inputs: list[str] = []
        for index, duration in enumerate(durations):
            safe_fade = min(fade_seconds, max(0, duration / 3))
            video_filter = (
                f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
                "fps=30,setsar=1,format=yuv420p,setpts=PTS-STARTPTS"
            )
            audio_filter = (
                f"[{index}:a]aresample=48000,"
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                "asetpts=PTS-STARTPTS"
            )
            if safe_fade > 0:
                fade_out_start = max(0, duration - safe_fade)
                video_filter += (
                    f",fade=t=in:st=0:d={safe_fade:.3f},"
                    f"fade=t=out:st={fade_out_start:.3f}:d={safe_fade:.3f}"
                )
                audio_filter += (
                    f",afade=t=in:st=0:d={safe_fade:.3f},"
                    f"afade=t=out:st={fade_out_start:.3f}:d={safe_fade:.3f}"
                )
            filters.extend(
                [
                    f"{video_filter}[v{index}]",
                    f"{audio_filter}[a{index}]",
                ]
            )
            concat_inputs.append(f"[v{index}][a{index}]")
        filters.append(
            "".join(concat_inputs)
            + f"concat=n={len(paths)}:v=1:a=1[vout][aout]"
        )

        relative = Path("albums") / album_id / "album-video" / f"{job_id}.mp4"
        output_path = db.STORAGE_DIR / relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [ffmpeg, "-y"]
        for path in paths:
            command.extend(["-i", str(path)])
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        set_job_progress(job_id, 25)
        logger.info(
            "Album video rendering started job_id=%s album_id=%s tracks=%s transition=%s",
            job_id,
            album_id,
            len(paths),
            request.transition,
        )
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=14400,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise RuntimeError(
                f"FFmpeg album video rendering failed (exit {completed.returncode}). "
                f"{stderr[-1600:]}"
            )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("Album video rendering completed without an output file")

        track_ids = [str(asset["track_id"]) for asset in assets]
        total_duration = sum(durations)
        asset = create_asset(
            album_id=album_id,
            track_id=None,
            generation_id=None,
            asset_type="album_video",
            path=output_path,
            original_name=f"{album['title']}-전체영상.mp4",
            content_type="video/mp4",
            metadata={
                **_model_dump(request),
                "track_ids": track_ids,
                "source_video_asset_ids": list(request.video_asset_ids),
                "duration_seconds": total_duration,
                "repeat_count": request.repeat_count,
            },
        )
        set_job_succeeded(
            job_id,
            {
                "asset_id": asset["id"],
                "track_count": len(track_ids),
                "duration_seconds": total_duration,
            },
        )
        logger.info(
            "Album video rendering completed job_id=%s asset_id=%s output=%s",
            job_id,
            asset["id"],
            output_path,
        )
    except Exception as exc:
        if output_path and output_path.exists():
            output_path.unlink(missing_ok=True)
        logger.exception(
            "Album video rendering failed job_id=%s album_id=%s error=%s",
            job_id,
            album_id,
            exc,
        )
        set_job_failed(job_id, exc, "ALBUM_VIDEO_RENDER_FAILED")
