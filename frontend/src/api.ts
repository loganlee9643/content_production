// 기본값은 Vite 프록시(/api/v1)를 사용해 5173→8000 CORS를 피합니다.
// Backend를 다른 호스트/포트로 직접 호출할 때만 VITE_API_BASE_URL을 설정하세요.
export const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api/v1";

export interface Album {
  id: string;
  title: string;
  artist_name: string | null;
  description: string | null;
  genre: string;
  vocal_style: string;
  tempo: string;
  lyrics_language: string;
  mood: string;
  instruments: string[];
  keywords: string;
  additional_instructions: string;
  style_prompt: string;
  track_count: number;
  status: string;
  selected_cover_asset_id: string | null;
  created_at: string;
  updated_at: string;
  tracks?: Track[];
  assets?: Asset[];
}

export interface Track {
  id: string;
  album_id: string;
  sequence: number;
  title: string;
  concept: string;
  lyrics: string;
  style_prompt: string;
  image_prompt: string;
  negative_tags: string;
  instrumental: boolean;
  model: string;
  status: string;
  selected_generation_id: string | null;
  generations?: Generation[];
  selected_generations?: Generation[];
}

export interface Generation {
  id: string;
  track_id: string;
  job_id: string;
  clip_id: string;
  status: string;
  title: string;
  audio_url: string | null;
  image_url: string | null;
  generated_lyrics: string | null;
  tags: string | null;
  is_selected: boolean;
}

export interface Job {
  id: string;
  type: string;
  resource_type: string;
  resource_id: string;
  status: "pending" | "running" | "succeeded" | "failed";
  progress: number;
  error_code: string | null;
  error_message: string | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
}

export interface Asset {
  id: string;
  album_id: string | null;
  track_id: string | null;
  generation_id: string | null;
  type: string;
  original_name: string;
  content_type: string;
  size_bytes: number;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface SunoStatus {
  connected: boolean;
  credits_left?: number;
  monthly_limit?: number;
  monthly_usage?: number;
  error?: string;
}

export interface VideoIcon {
  filename: string;
  label: string;
}

export interface VideoTemplate {
  id: string;
  album_id: string;
  name: string;
  compose: Record<string, unknown>;
  image_instruction: string;
  title_source: "track" | "template" | "hidden";
  artist_source: "album" | "template" | "hidden";
  preview_asset_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ThumbnailTextLayer {
  id: string;
  type: "text";
  text: string;
  x: number;
  y: number;
  width: number;
  font_family: string;
  font_size: number;
  color: string;
  align: "left" | "center" | "right";
  stroke_color: string;
  stroke_width: number;
  shadow: boolean;
  background_color: string;
  background_opacity: number;
  padding: number;
  rotation: number;
  opacity: number;
}

export interface ThumbnailIconLayer {
  id: string;
  type: "icon";
  icon_image: string;
  icon: string;
  x: number;
  y: number;
  size: number;
  color: string;
  rotation: number;
  opacity: number;
}

export type ThumbnailLayer = ThumbnailTextLayer | ThumbnailIconLayer;

export interface ThumbnailDesign {
  width: number;
  height: number;
  brightness: number;
  contrast: number;
  saturation: number;
  blur: number;
  overlay_color: string;
  overlay_opacity: number;
  layers: ThumbnailLayer[];
}

export interface Thumbnail {
  id: string;
  album_id: string;
  name: string;
  background_asset_id: string | null;
  design: ThumbnailDesign;
  rendered_asset_id: string | null;
  created_at: string;
  updated_at: string;
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && typeof options.body === "string") {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    let message = `요청에 실패했습니다. (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || body.error?.message || message;
    } catch {
      // Keep the generic message for non-JSON failures.
    }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  const body = await response.json();
  return body.data as T;
}

const json = (value: unknown) => JSON.stringify(value);

export const api = {
  listAlbums: () => request<Album[]>("/albums"),
  getAlbum: (id: string) => request<Album>(`/albums/${id}`),
  createAlbum: (payload: Partial<Album>) =>
    request<Album>("/albums", { method: "POST", body: json(payload) }),
  updateAlbum: (id: string, payload: Partial<Album>) =>
    request<Album>(`/albums/${id}`, { method: "PATCH", body: json(payload) }),
  deleteAlbum: (id: string) =>
    request<void>(`/albums/${id}`, { method: "DELETE" }),
  planAlbum: (id: string) =>
    request<{ job_id: string; status: string }>(`/albums/${id}/plan`, {
      method: "POST",
    }),
  listTracks: (albumId: string) =>
    request<Track[]>(`/albums/${albumId}/tracks`),
  updateTrack: (id: string, payload: Partial<Track>) =>
    request<Track>(`/tracks/${id}`, {
      method: "PATCH",
      body: json(payload),
    }),
  saveLyrics: (id: string, lyrics: string) =>
    request<Track>(`/tracks/${id}/lyrics`, {
      method: "PUT",
      body: json({ lyrics }),
    }),
  saveStyle: (id: string, style_prompt: string) =>
    request<Track>(`/tracks/${id}/style`, {
      method: "PUT",
      body: json({ style_prompt }),
    }),
  regenerateLyrics: (id: string, instruction: string, regenerateStyle = false) =>
    request<{ job_id: string; status: string }>(
      `/tracks/${id}/lyrics/regenerate`,
      {
        method: "POST",
        body: json({
          instruction,
          regenerate_style: regenerateStyle,
        }),
      },
    ),
  generateTrack: (id: string) =>
    request<{ job_id: string; status: string }>(`/tracks/${id}/generate`, {
      method: "POST",
      body: json({
        mode: "custom",
        download_audio: true,
        timeout_seconds: 600,
        poll_interval_seconds: 10,
      }),
    }),
  generateAlbum: (albumId: string, trackIds: string[]) =>
    request<{ job_id: string; status: string }>(
      `/albums/${albumId}/generate`,
      {
        method: "POST",
        body: json({ track_ids: trackIds, download_audio: true }),
      },
    ),
  listGenerations: (trackId: string) =>
    request<Generation[]>(`/tracks/${trackId}/generations`),
  selectGeneration: (trackId: string, generationId: string) =>
    request<Generation>(
      `/tracks/${trackId}/generations/${generationId}/select`,
      { method: "POST" },
    ),
  updateGeneration: (generationId: string, title: string) =>
    request<Generation>(`/generations/${generationId}`, {
      method: "PATCH",
      body: json({ title }),
    }),
  getJob: (id: string) => request<Job>(`/jobs/${id}`),
  sunoStatus: () => request<SunoStatus>("/system/suno-status"),
  listVideoIcons: () => request<VideoIcon[]>("/system/video-icons"),
  listVideoTemplates: (albumId: string) =>
    request<VideoTemplate[]>(`/albums/${albumId}/video-templates`),
  createVideoTemplate: (
    albumId: string,
    payload: {
      name: string;
      compose: Record<string, unknown>;
      image_instruction: string;
      title_source: "track" | "template" | "hidden";
      artist_source: "album" | "template" | "hidden";
      preview_asset_id?: string | null;
    },
  ) =>
    request<VideoTemplate>(`/albums/${albumId}/video-templates`, {
      method: "POST",
      body: json(payload),
    }),
  updateVideoTemplate: (
    templateId: string,
    payload: Partial<{
      name: string;
      compose: Record<string, unknown>;
      image_instruction: string;
      title_source: "track" | "template" | "hidden";
      artist_source: "album" | "template" | "hidden";
      preview_asset_id: string | null;
    }>,
  ) =>
    request<VideoTemplate>(`/video-templates/${templateId}`, {
      method: "PATCH",
      body: json(payload),
    }),
  deleteVideoTemplate: (templateId: string) =>
    request<void>(`/video-templates/${templateId}`, { method: "DELETE" }),
  listVideoTemplateAssignments: (albumId: string) =>
    request<Record<string, string>>(
      `/albums/${albumId}/video-template-assignments`,
    ),
  setTrackVideoTemplate: (trackId: string, templateId: string) =>
    request<{ track_id: string; template_id: string }>(
      `/tracks/${trackId}/video-template`,
      { method: "PUT", body: json({ template_id: templateId }) },
    ),
  renderVideosBatch: (
    albumId: string,
    payload: {
      track_ids?: string[];
      generation_ids?: string[];
      template_id?: string | null;
      edit_mode?: "saved_then_template" | "template_only" | "saved_only";
      missing_edit_action?: "template" | "exclude";
      image_mode?: "generate_per_track" | "generate_shared" | "selected_then_generate_per_track" | "shared_existing";
      shared_image_asset_id?: string | null;
      image_instruction?: string;
      candidate_count?: number;
      retry_image_failures?: boolean;
      overwrite_existing?: boolean;
      continue_on_error?: boolean;
    },
  ) =>
    request<{ job_id: string; status: string }>(
      `/albums/${albumId}/videos/render-batch`,
      { method: "POST", body: json(payload) },
    ),
  combineAlbumVideos: (
    albumId: string,
    payload: {
      video_asset_ids: string[];
      transition: "none" | "fade";
      transition_seconds: number;
      resolution: "1920x1080" | "1280x720";
      repeat_count: number;
    },
  ) =>
    request<{ job_id: string; status: string }>(
      `/albums/${albumId}/videos/combine`,
      { method: "POST", body: json(payload) },
    ),
  getVideoDurations: (albumId: string, videoAssetIds: string[]) =>
    request<Record<string, number>>(`/albums/${albumId}/videos/durations`, {
      method: "POST",
      body: json({ video_asset_ids: videoAssetIds }),
    }),
  generateCovers: (
    albumId: string,
    payload: {
      track_id: string | null;
      instruction: string;
      aspect_ratio: string;
      candidate_count: number;
    },
  ) =>
    request<{ job_id: string; status: string }>(
      `/albums/${albumId}/covers/generate`,
      { method: "POST", body: json(payload) },
    ),
  uploadCover: async (albumId: string, file: File) => {
    const response = await fetch(
      `${API_BASE}/albums/${albumId}/covers/upload?filename=${encodeURIComponent(file.name)}`,
      {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      },
    );
    if (!response.ok) throw new ApiError(response.status, await response.text());
    return (await response.json()).data as Asset;
  },
  listCovers: (albumId: string) =>
    request<Asset[]>(`/albums/${albumId}/covers`),
  generateTemplatePreviews: (
    albumId: string,
    payload: {
      instruction: string;
      aspect_ratio: string;
      candidate_count: number;
    },
  ) =>
    request<{ job_id: string; status: string }>(
      `/albums/${albumId}/template-previews/generate`,
      { method: "POST", body: json({ ...payload, track_id: null }) },
    ),
  uploadTemplatePreview: async (albumId: string, file: File) => {
    const response = await fetch(
      `${API_BASE}/albums/${albumId}/template-previews/upload?filename=${encodeURIComponent(file.name)}`,
      {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      },
    );
    if (!response.ok) throw new ApiError(response.status, await response.text());
    return (await response.json()).data as Asset;
  },
  listTemplatePreviews: (albumId: string) =>
    request<Asset[]>(`/albums/${albumId}/template-previews`),
  listThumbnailBackgrounds: (albumId: string) =>
    request<Asset[]>(`/albums/${albumId}/thumbnail-backgrounds`),
  generateThumbnailBackgrounds: (
    albumId: string,
    payload: {
      instruction: string;
      aspect_ratio: string;
      candidate_count: number;
    },
  ) =>
    request<{ job_id: string; status: string }>(
      `/albums/${albumId}/thumbnail-backgrounds/generate`,
      { method: "POST", body: json({ ...payload, track_id: null }) },
    ),
  generateThumbnailCopy: (albumId: string, instruction: string) =>
    request<{ job_id: string; status: string }>(
      `/albums/${albumId}/thumbnail-copy/generate`,
      { method: "POST", body: json({ instruction }) },
    ),
  uploadThumbnailBackground: async (albumId: string, file: File) => {
    const response = await fetch(
      `${API_BASE}/albums/${albumId}/thumbnail-backgrounds/upload?filename=${encodeURIComponent(file.name)}`,
      {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      },
    );
    if (!response.ok) throw new ApiError(response.status, await response.text());
    return (await response.json()).data as Asset;
  },
  listThumbnails: (albumId: string) =>
    request<Thumbnail[]>(`/albums/${albumId}/thumbnails`),
  createThumbnail: (
    albumId: string,
    payload: {
      name: string;
      background_asset_id: string | null;
      design: ThumbnailDesign;
    },
  ) =>
    request<Thumbnail>(`/albums/${albumId}/thumbnails`, {
      method: "POST",
      body: json(payload),
    }),
  updateThumbnail: (
    thumbnailId: string,
    payload: Partial<{
      name: string;
      background_asset_id: string;
      design: ThumbnailDesign;
    }>,
  ) =>
    request<Thumbnail>(`/thumbnails/${thumbnailId}`, {
      method: "PATCH",
      body: json(payload),
    }),
  deleteThumbnail: (thumbnailId: string) =>
    request<void>(`/thumbnails/${thumbnailId}`, { method: "DELETE" }),
  renderThumbnail: (thumbnailId: string) =>
    request<Asset>(`/thumbnails/${thumbnailId}/render`, { method: "POST" }),
  selectCover: (albumId: string, assetId: string) =>
    request<Asset>(`/albums/${albumId}/covers/${assetId}/select`, {
      method: "POST",
    }),
  composeImage: (
    albumId: string,
    assetId: string,
    payload: Record<string, unknown>,
  ) =>
    request<Asset>(`/albums/${albumId}/images/${assetId}/compose`, {
      method: "POST",
      body: json(payload),
    }),
  renderVideo: (
    albumId: string,
    payload: Record<string, unknown>,
  ) =>
    request<{ job_id: string; status: string }>(
      `/albums/${albumId}/videos/render`,
      { method: "POST", body: json(payload) },
    ),
  createArchive: (albumId: string) =>
    request<Asset>(`/albums/${albumId}/archive`, { method: "POST" }),
};

export const assetUrl = (assetId: string) =>
  `${API_BASE}/assets/${assetId}/download`;

export const iconAssetUrl = (filename: string) =>
  `${API_BASE}/system/video-icons/${encodeURIComponent(filename)}`;

export const lyricsUrl = (trackId: string) =>
  `${API_BASE}/tracks/${trackId}/lyrics/download`;
