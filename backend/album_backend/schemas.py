from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, Field


class AlbumCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    artist_name: str | None = None
    description: str | None = None
    genre: str = ""
    vocal_style: str = ""
    tempo: str = ""
    lyrics_language: str = "ko"
    mood: str = ""
    instruments: list[str] = Field(default_factory=list)
    keywords: str = ""
    additional_instructions: str = ""
    track_count: int = Field(default=10, ge=1, le=30)


class AlbumUpdate(BaseModel):
    title: str | None = None
    artist_name: str | None = None
    description: str | None = None
    genre: str | None = None
    vocal_style: str | None = None
    tempo: str | None = None
    lyrics_language: str | None = None
    mood: str | None = None
    instruments: list[str] | None = None
    keywords: str | None = None
    additional_instructions: str | None = None
    track_count: int | None = Field(default=None, ge=1, le=30)


class TrackCreate(BaseModel):
    sequence: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    concept: str = ""
    lyrics: str = ""
    style_prompt: str = ""
    image_prompt: str = ""
    negative_tags: str = ""
    instrumental: bool = False
    model: str = Field(
        default_factory=lambda: os.getenv("SUNO_MODEL", "chirp-fenix")
    )


class TrackUpdate(BaseModel):
    sequence: int | None = Field(default=None, ge=1)
    title: str | None = None
    concept: str | None = None
    lyrics: str | None = None
    style_prompt: str | None = None
    image_prompt: str | None = None
    negative_tags: str | None = None
    instrumental: bool | None = None
    model: str | None = None


class GenerationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class LyricsUpdate(BaseModel):
    lyrics: str


class StyleUpdate(BaseModel):
    style_prompt: str


class RegenerateLyricsRequest(BaseModel):
    instruction: str = ""
    regenerate_style: bool = False


class GenerateTrackRequest(BaseModel):
    mode: Literal["custom", "description"] = "custom"
    model: str | None = None
    download_audio: bool = True
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    poll_interval_seconds: int = Field(default=10, ge=2, le=60)


class GenerateAlbumRequest(BaseModel):
    track_ids: list[str] | None = None
    download_audio: bool = True


class ImageGenerateRequest(BaseModel):
    track_id: str | None = None
    instruction: str = ""
    aspect_ratio: str = "16:9"
    candidate_count: int = Field(default=1, ge=1, le=4)


class ImageComposeRequest(BaseModel):
    crop: str = "fill"
    brightness: float = 0
    contrast: float = 0
    saturation: float = 0
    blur: float = 0
    overlay_color: str = "#21000f"
    overlay_opacity: float = Field(default=0.15, ge=0, le=1)
    title: str = ""
    artist_name: str = ""
    title_position: str = "bottom-left"
    title_x: float = Field(default=18, ge=0, le=100)
    title_y: float = Field(default=82, ge=0, le=100)
    title_anchor_text: str = ""
    font_family: str = "malgun"
    text_color: str = "#ffffff"
    title_size: int = Field(default=72, ge=24, le=240)
    artist_x: float = Field(default=18, ge=0, le=100)
    artist_y: float = Field(default=88, ge=0, le=100)
    artist_font_family: str = "malgun"
    artist_color: str = "#ffffff"
    artist_size: int = Field(default=28, ge=12, le=120)
    icon: str = ""
    icon_image: str = ""
    icon_x: float = Field(default=50, ge=0, le=100)
    icon_y: float = Field(default=18, ge=0, le=100)
    icon_size: int = Field(default=64, ge=16, le=240)
    show_visualizer: bool = True
    visualizer_x: float = Field(default=88, ge=0, le=100)
    visualizer_y: float = Field(default=82, ge=0, le=100)
    visualizer_width: float = Field(default=18, ge=5, le=80)
    visualizer_height: int = Field(default=90, ge=30, le=500)
    visualizer_style: Literal["bars", "wave", "dots"] = "bars"


class ThumbnailTextLayer(BaseModel):
    id: str
    type: Literal["text"] = "text"
    text: str = ""
    x: float = Field(default=50, ge=0, le=100)
    y: float = Field(default=50, ge=0, le=100)
    width: float = Field(default=70, ge=5, le=100)
    font_family: str = "malgun"
    font_size: int = Field(default=72, ge=12, le=240)
    color: str = "#ffffff"
    align: Literal["left", "center", "right"] = "center"
    stroke_color: str = "#000000"
    stroke_width: int = Field(default=3, ge=0, le=20)
    shadow: bool = True
    background_color: str = "#000000"
    background_opacity: float = Field(default=0, ge=0, le=1)
    padding: int = Field(default=12, ge=0, le=80)
    rotation: float = Field(default=0, ge=-180, le=180)
    opacity: float = Field(default=1, ge=0, le=1)


class ThumbnailIconLayer(BaseModel):
    id: str
    type: Literal["icon"] = "icon"
    icon_image: str = ""
    icon: str = ""
    x: float = Field(default=50, ge=0, le=100)
    y: float = Field(default=50, ge=0, le=100)
    size: int = Field(default=96, ge=16, le=500)
    color: str = "#ffffff"
    rotation: float = Field(default=0, ge=-180, le=180)
    opacity: float = Field(default=1, ge=0, le=1)


ThumbnailLayer = ThumbnailTextLayer | ThumbnailIconLayer


class ThumbnailDesign(BaseModel):
    width: int = Field(default=1280, ge=320, le=3840)
    height: int = Field(default=720, ge=180, le=2160)
    brightness: float = Field(default=0, ge=-100, le=100)
    contrast: float = Field(default=0, ge=-100, le=100)
    saturation: float = Field(default=0, ge=-100, le=100)
    blur: float = Field(default=0, ge=0, le=30)
    overlay_color: str = "#000000"
    overlay_opacity: float = Field(default=0.15, ge=0, le=1)
    layers: list[ThumbnailLayer] = Field(default_factory=list)


class ThumbnailCreate(BaseModel):
    name: str = Field(default="새 썸네일", min_length=1, max_length=100)
    background_asset_id: str | None = None
    design: ThumbnailDesign = Field(default_factory=ThumbnailDesign)


class ThumbnailUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    background_asset_id: str | None = None
    design: ThumbnailDesign | None = None


class ThumbnailCopyGenerateRequest(BaseModel):
    instruction: str = Field(default="", max_length=1000)


class VideoRenderRequest(BaseModel):
    mode: Literal["static_loop", "animated_image", "album_mix"] = "static_loop"
    track_id: str
    generation_id: str | None = None
    image_asset_id: str
    resolution: str = "1920x1080"
    show_title: bool = True
    show_lyrics: bool = False
    show_visualizer: bool = True
    visualizer_style: str = "bars"
    visualizer_x: float = Field(default=88, ge=0, le=100)
    visualizer_y: float = Field(default=82, ge=0, le=100)
    visualizer_width: float = Field(default=18, ge=5, le=80)
    visualizer_height: int = Field(default=90, ge=30, le=500)
    visualizer_color: str = "#ffffff"
    visualizer_opacity: float = Field(default=0.82, ge=0, le=1)
    visualizer_background_color: str = "transparent"
    visualizer_background_opacity: float = Field(default=0, ge=0, le=1)
    visualizer_show_background: bool = False
    visualizer_bar_count: int = Field(default=5, ge=1, le=32)
    visualizer_gap: int = Field(default=10, ge=0, le=80)
    visualizer_bars: list[int] = Field(default_factory=list)
    loop_motion: str = "slow_zoom"
    fade_in_seconds: float = Field(default=1.0, ge=0, le=10)
    fade_out_seconds: float = Field(default=1.0, ge=0, le=10)


class VideoTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    compose: ImageComposeRequest
    image_instruction: str = ""
    title_source: Literal["track", "template", "hidden"] = "track"
    artist_source: Literal["album", "template", "hidden"] = "album"
    preview_asset_id: str | None = None


class VideoTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    compose: ImageComposeRequest | None = None
    image_instruction: str | None = None
    title_source: Literal["track", "template", "hidden"] | None = None
    artist_source: Literal["album", "template", "hidden"] | None = None
    preview_asset_id: str | None = None


class TrackVideoTemplateUpdate(BaseModel):
    template_id: str


class BatchVideoRenderRequest(BaseModel):
    track_ids: list[str] = Field(default_factory=list)
    generation_ids: list[str] = Field(default_factory=list)
    template_id: str | None = None
    edit_mode: Literal[
        "saved_then_template",
        "template_only",
        "saved_only",
    ] = "saved_then_template"
    missing_edit_action: Literal["template", "exclude"] = "template"
    image_mode: Literal[
        "generate_per_track",
        "generate_shared",
        "selected_then_generate_per_track",
        "shared_existing",
    ] = "selected_then_generate_per_track"
    shared_image_asset_id: str | None = None
    image_instruction: str = ""
    candidate_count: int = Field(default=1, ge=1, le=4)
    retry_image_failures: bool = True
    overwrite_existing: bool = False
    continue_on_error: bool = True


class AlbumVideoRenderRequest(BaseModel):
    video_asset_ids: list[str] = Field(min_length=1)
    transition: Literal["none", "fade"] = "fade"
    transition_seconds: float = Field(default=1.0, ge=0, le=5)
    resolution: Literal["1920x1080", "1280x720"] = "1920x1080"


class JobAccepted(BaseModel):
    job_id: str
    status: str = "pending"


class ApiResponse(BaseModel):
    data: Any
