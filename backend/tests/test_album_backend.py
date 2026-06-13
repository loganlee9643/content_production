from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

os.environ.setdefault("BASE_URL", "https://studio-api.prod.suno.com")

from album_backend import db, router, schemas, services
import utils
import start_suno_server


class AlbumBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        db.DB_PATH = root / "test.sqlite3"
        db.STORAGE_DIR = root / "storage"
        db.init_db()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_album(self) -> dict:
        response = asyncio.run(
            router.create_album(
                schemas.AlbumCreate(
                    title="비 오는 날의 기억",
                    genre="K-Pop",
                    vocal_style="soft female vocal",
                    tempo="90-110 BPM",
                    lyrics_language="ko",
                    mood="nostalgic",
                    instruments=["synthesizer", "piano"],
                    keywords="비, 친구, 추억",
                    track_count=2,
                )
            )
        )
        return response["data"]

    def test_video_font_uses_korean_font_for_hangul_text(self) -> None:
        korean_font = Path(r"C:\Windows\Fonts\malgun.ttf")
        with (
            patch.object(services, "_korean_video_font_path", return_value=korean_font),
            patch.object(services, "_video_font_path") as selected_font,
        ):
            result = services._video_text_font_path("arial", "첫 빗방울")

        self.assertEqual(result, korean_font)
        selected_font.assert_not_called()

    def test_video_font_keeps_selected_font_for_latin_text(self) -> None:
        arial_font = Path(r"C:\Windows\Fonts\arial.ttf")
        with (
            patch.object(services, "_korean_video_font_path") as korean_font,
            patch.object(services, "_video_font_path", return_value=arial_font) as selected_font,
        ):
            result = services._video_text_font_path("arial", "PLAY LIST")

        self.assertEqual(result, arial_font)
        korean_font.assert_not_called()
        selected_font.assert_called_once_with("arial")

    def test_album_track_and_archive_flow(self) -> None:
        album = self.create_album()
        track_response = asyncio.run(
            router.create_track(
                album["id"],
                schemas.TrackCreate(
                    sequence=1,
                    title="비 오는 창가",
                    lyrics="[Verse]\n비가 내린다",
                    style_prompt="Nostalgic synthpop, soft female vocal",
                ),
            )
        )
        track = track_response["data"]
        style_response = asyncio.run(
            router.save_style(
                track["id"],
                schemas.StyleUpdate(
                    style_prompt="Warm 90s synthpop, female vocal"
                ),
            )
        )
        self.assertTrue(style_response["data"]["style_prompt"].startswith("Warm"))

        archive_response = asyncio.run(router.create_archive(album["id"]))
        asset = archive_response["data"]
        self.assertTrue((db.STORAGE_DIR / asset["storage_key"]).is_file())

    def test_job_and_image_compose_metadata(self) -> None:
        album = self.create_album()
        asset = services.save_uploaded_asset(
            album["id"], b"fake-png", "cover.png", "image/png"
        )
        compose_response = asyncio.run(
            router.compose_image(
                album["id"],
                asset["id"],
                schemas.ImageComposeRequest(
                    title="PLAY LIST",
                    overlay_opacity=0.25,
                    icon_image="music-icon.png",
                ),
            )
        )
        self.assertEqual(
            compose_response["data"]["metadata"]["compose"]["title"],
            "PLAY LIST",
        )
        self.assertEqual(
            compose_response["data"]["metadata"]["compose"]["icon_image"],
            "music-icon.png",
        )

        job = services.create_job("test", "album", album["id"])
        services.set_job_succeeded(job["id"], {"ok": True})
        response = asyncio.run(router.get_job(job["id"]))
        self.assertTrue(response["data"]["result"]["ok"])

    def test_thumbnail_document_renders_png_with_text_layers(self) -> None:
        album = self.create_album()
        background_path = db.STORAGE_DIR / "thumbnail-background.png"
        background_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), "#552244").save(background_path)
        background = services.create_asset(
            album_id=album["id"],
            track_id=None,
            generation_id=None,
            asset_type="thumbnail_background",
            path=background_path,
            original_name="thumbnail-background.png",
            content_type="image/png",
        )
        thumbnail = asyncio.run(
            router.create_thumbnail(
                album["id"],
                schemas.ThumbnailCreate(
                    name="Playlist Thumbnail",
                    background_asset_id=background["id"],
                    design=schemas.ThumbnailDesign(
                        layers=[
                            schemas.ThumbnailTextLayer(
                                id="title",
                                text="PLAY LIST",
                                font_family="arial",
                                font_size=80,
                                x=50,
                                y=50,
                            )
                        ]
                    ),
                ),
            )
        )["data"]

        rendered = asyncio.run(router.render_thumbnail(thumbnail["id"]))["data"]
        output = db.STORAGE_DIR / rendered["storage_key"]

        self.assertEqual(rendered["type"], "thumbnail")
        self.assertTrue(output.is_file())
        with Image.open(output) as image:
            self.assertEqual(image.size, (1280, 720))

    def test_thumbnail_copy_generation_uses_album_context(self) -> None:
        album = self.create_album()
        job = services.create_job(
            "thumbnail_copy_generate",
            "album",
            album["id"],
        )
        response = json.dumps(
            {
                "headline": "오늘은 이 노래",
                "subheadline": "퇴근 후 마음이 풀리는 플레이리스트",
                "accent": "감성 충전",
            },
            ensure_ascii=False,
        )

        with patch.object(services, "_gemini_text", return_value=response) as gemini:
            services.run_thumbnail_copy_generation(
                job["id"],
                album["id"],
                "따뜻한 어쿠스틱 분위기",
            )

        completed = db.get_one("jobs", job["id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"]["headline"], "오늘은 이 노래")
        prompt = gemini.call_args.args[1]
        self.assertIn(album["title"], prompt)
        self.assertIn("따뜻한 어쿠스틱 분위기", prompt)

    def test_video_icon_folder_listing_and_path_validation(self) -> None:
        icon_dir = Path(self.temp_dir.name) / "icons"
        icon_dir.mkdir()
        (icon_dir / "music-note.png").write_bytes(b"png")
        (icon_dir / "speaker.webp").write_bytes(b"webp")
        (icon_dir / "vinyl.svg").write_text("<svg/>", encoding="utf-8")
        (icon_dir / "ignore.txt").write_text("ignore", encoding="utf-8")

        with patch.object(services, "VIDEO_ICON_DIR", icon_dir.resolve()):
            icons = services.list_video_icons()
            self.assertEqual(
                [item["filename"] for item in icons],
                ["music-note.png", "speaker.webp", "vinyl.svg"],
            )
            self.assertEqual(
                services.resolve_video_icon("music-note.png"),
                (icon_dir / "music-note.png").resolve(),
            )
            self.assertIsNone(services.resolve_video_icon("../music-note.png"))
            self.assertIsNone(services.resolve_video_icon("ignore.txt"))
            self.assertEqual(
                services.resolve_video_icon("vinyl.svg"),
                (icon_dir / "vinyl.svg").resolve(),
            )

    def test_video_template_create_assign_and_list(self) -> None:
        album = self.create_album()
        preview = services.save_uploaded_asset(
            album["id"],
            b"preview",
            "template-preview.png",
            "image/png",
            "template_preview",
        )
        track = asyncio.run(
            router.create_track(
                album["id"],
                schemas.TrackCreate(sequence=1, title="첫 번째 트랙"),
            )
        )["data"]
        template = asyncio.run(
            router.create_video_template(
                album["id"],
                schemas.VideoTemplateCreate(
                    name="기본 템플릿",
                    compose=schemas.ImageComposeRequest(
                        title="PLAY LIST",
                        visualizer_style="bars",
                    ),
                    image_instruction="warm rainy cafe",
                    title_source="template",
                    artist_source="hidden",
                    preview_asset_id=preview["id"],
                ),
            )
        )["data"]

        asyncio.run(
            router.set_track_video_template(
                track["id"],
                schemas.TrackVideoTemplateUpdate(template_id=template["id"]),
            )
        )
        templates = asyncio.run(
            router.list_video_templates(album["id"])
        )["data"]
        assignments = asyncio.run(
            router.list_video_template_assignments(album["id"])
        )["data"]
        template_previews = asyncio.run(
            router.list_template_previews(album["id"])
        )["data"]
        covers = asyncio.run(router.list_images(album["id"]))["data"]

        self.assertEqual(templates[0]["name"], "기본 템플릿")
        self.assertEqual(templates[0]["compose"]["visualizer_style"], "bars")
        self.assertEqual(templates[0]["title_source"], "template")
        self.assertEqual(templates[0]["artist_source"], "hidden")
        self.assertEqual(templates[0]["preview_asset_id"], preview["id"])
        self.assertEqual(template_previews[0]["id"], preview["id"])
        self.assertNotIn(preview["id"], {asset["id"] for asset in covers})
        self.assertEqual(assignments[track["id"]], template["id"])

    def test_batch_video_render_applies_template_to_generated_image(self) -> None:
        album = self.create_album()
        track = asyncio.run(
            router.create_track(
                album["id"],
                schemas.TrackCreate(sequence=1, title="자동 영상 트랙"),
            )
        )["data"]
        generation = db.insert(
            "generations",
            {
                "id": db.new_id(),
                "track_id": track["id"],
                "job_id": services.create_job(
                    "track_generate", "track", track["id"]
                )["id"],
                "request_id": None,
                "clip_id": "clip-test",
                "status": "complete",
                "title": track["title"],
                "audio_url": None,
                "image_url": None,
                "local_audio_path": "fake.mp3",
                "generated_lyrics": None,
                "tags": None,
                "raw_response_json": "{}",
                "is_selected": 1,
                "created_at": db.now_iso(),
                "completed_at": db.now_iso(),
            },
        )
        db.update(
            "tracks",
            track["id"],
            {"selected_generation_id": generation["id"]},
        )
        template = asyncio.run(
            router.create_video_template(
                album["id"],
                schemas.VideoTemplateCreate(
                    name="자동 템플릿",
                    compose=schemas.ImageComposeRequest(
                        title="PLACEHOLDER",
                        artist_name="Template Artist",
                        text_color="#ffcc88",
                    ),
                    artist_source="template",
                ),
            )
        )["data"]
        batch_job = services.create_job(
            "video_render_batch", "album", album["id"]
        )

        def fake_image_generation(
            child_job_id,
            target_album_id,
            target_track_id,
            instruction,
            aspect_ratio,
            candidate_count,
        ):
            image_path = db.STORAGE_DIR / "generated.png"
            image_path.write_bytes(b"png")
            asset = services.create_asset(
                album_id=target_album_id,
                track_id=target_track_id,
                generation_id=None,
                asset_type="cover",
                path=image_path,
                original_name="generated.png",
                content_type="image/png",
            )
            services.set_job_succeeded(child_job_id, {"asset_ids": [asset["id"]]})

        captured_compose = {}

        def fake_frame_render(source, destination, compose):
            captured_compose.update(compose)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"composed png")
            return destination

        def fake_video_render(child_job_id, target_album_id, render_request):
            image_asset = db.get_one("assets", render_request.image_asset_id)
            self.assertTrue(image_asset["metadata"]["render_frame"])
            compose = captured_compose
            self.assertEqual(compose["title"], track["title"])
            self.assertEqual(compose["artist_name"], "Template Artist")
            self.assertEqual(compose["text_color"], "#ffcc88")
            self.assertFalse(render_request.show_title)
            services.set_job_succeeded(child_job_id, {"asset_id": "video-test"})

        with (
            patch.object(services, "run_image_generation", fake_image_generation),
            patch.object(services, "render_static_video_frame", fake_frame_render),
            patch.object(services, "validate_static_video_frame"),
            patch.object(services, "run_video_render", fake_video_render),
        ):
            services.run_batch_video_render(
                batch_job["id"],
                album["id"],
                schemas.BatchVideoRenderRequest(
                    track_ids=[track["id"]],
                    template_id=template["id"],
                ),
            )

        completed_job = db.get_one("jobs", batch_job["id"])
        self.assertEqual(completed_job["status"], "succeeded")
        self.assertEqual(
            completed_job["result"]["completed"][0]["video_asset_id"],
            "video-test",
        )

    def test_batch_video_render_reuses_saved_edit_and_track_image(self) -> None:
        album = self.create_album()
        track = asyncio.run(
            router.create_track(
                album["id"],
                schemas.TrackCreate(sequence=1, title="저장 편집 트랙"),
            )
        )["data"]
        generation = db.insert(
            "generations",
            {
                "id": db.new_id(),
                "track_id": track["id"],
                "job_id": services.create_job(
                    "track_generate", "track", track["id"]
                )["id"],
                "request_id": None,
                "clip_id": "clip-saved-edit",
                "status": "complete",
                "title": track["title"],
                "audio_url": None,
                "image_url": None,
                "local_audio_path": "fake.mp3",
                "generated_lyrics": None,
                "tags": None,
                "raw_response_json": "{}",
                "is_selected": 1,
                "created_at": db.now_iso(),
                "completed_at": db.now_iso(),
            },
        )
        db.update(
            "tracks",
            track["id"],
            {"selected_generation_id": generation["id"]},
        )
        image_path = db.STORAGE_DIR / "saved-edit.png"
        image_path.write_bytes(b"png")
        image_asset = services.create_asset(
            album_id=album["id"],
            track_id=track["id"],
            generation_id=None,
            asset_type="cover",
            path=image_path,
            original_name="saved-edit.png",
            content_type="image/png",
            metadata={
                "compose": {
                    **services._model_dump(schemas.ImageComposeRequest()),
                    "title": "SAVED TITLE",
                    "text_color": "#123456",
                }
            },
        )
        batch_job = services.create_job(
            "video_render_batch", "album", album["id"]
        )

        captured_compose = {}

        def fake_frame_render(source, destination, compose):
            captured_compose.update(compose)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"composed png")
            return destination

        def fake_video_render(child_job_id, target_album_id, render_request):
            rendered_image = db.get_one("assets", render_request.image_asset_id)
            self.assertTrue(rendered_image["metadata"]["render_frame"])
            self.assertEqual(
                rendered_image["metadata"]["source_image_asset_id"],
                image_asset["id"],
            )
            self.assertEqual(captured_compose["title"], "SAVED TITLE")
            self.assertFalse(render_request.show_title)
            services.set_job_succeeded(child_job_id, {"asset_id": "video-saved"})

        with (
            patch.object(
                services,
                "run_image_generation",
                side_effect=AssertionError("image generation should not run"),
            ),
            patch.object(services, "render_static_video_frame", fake_frame_render),
            patch.object(services, "validate_static_video_frame"),
            patch.object(services, "run_video_render", fake_video_render),
        ):
            services.run_batch_video_render(
                batch_job["id"],
                album["id"],
                schemas.BatchVideoRenderRequest(
                    track_ids=[track["id"]],
                    edit_mode="saved_then_template",
                    image_mode="selected_then_generate_per_track",
                ),
            )

        completed_job = db.get_one("jobs", batch_job["id"])
        self.assertEqual(completed_job["status"], "succeeded")
        self.assertEqual(
            completed_job["result"]["completed"][0]["edit_source"],
            "saved",
        )
        self.assertEqual(
            completed_job["result"]["completed"][0]["image_source"],
            "selected",
        )
        self.assertEqual(
            completed_job["payload"]["activity"]["tracks"][0]["status"],
            "completed",
        )

    def test_album_video_render_combines_track_videos_in_requested_order(self) -> None:
        album = self.create_album()
        tracks = [
            asyncio.run(
                router.create_track(
                    album["id"],
                    schemas.TrackCreate(sequence=index, title=f"Track {index}"),
                )
            )["data"]
            for index in (1, 2)
        ]
        assets = []
        for track in tracks:
            path = db.STORAGE_DIR / f"{track['id']}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
            assets.append(
                services.create_asset(
                    album_id=album["id"],
                    track_id=track["id"],
                    generation_id=None,
                    asset_type="video",
                    path=path,
                    original_name=f"{track['title']}.mp4",
                    content_type="video/mp4",
                )
            )
        job = services.create_job("album_video_render", "album", album["id"])
        captured_command = []

        def fake_run(command, **kwargs):
            captured_command.extend(command)
            Path(command[-1]).write_bytes(b"combined video")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(services.shutil, "which", side_effect=lambda name: name),
            patch.object(services, "_probe_media_duration", side_effect=[12.0, 18.0]),
            patch.object(services.subprocess, "run", fake_run),
        ):
            services.run_album_video_render(
                job["id"],
                album["id"],
                schemas.AlbumVideoRenderRequest(
                    video_asset_ids=[assets[1]["id"], assets[0]["id"]],
                    transition="fade",
                    transition_seconds=1,
                ),
            )

        completed = db.get_one("jobs", job["id"])
        result_asset = db.get_one("assets", completed["result"]["asset_id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(result_asset["type"], "album_video")
        self.assertEqual(
            result_asset["metadata"]["source_video_asset_ids"],
            [assets[1]["id"], assets[0]["id"]],
        )
        self.assertEqual(result_asset["metadata"]["duration_seconds"], 30.0)
        self.assertTrue(
            any("concat=n=2:v=1:a=1[vout][aout]" in value for value in captured_command)
        )
        self.assertTrue(any("fade=t=in" in value for value in captured_command))

    def test_suno_prompt_guidance_covers_custom_mode_metadata(self) -> None:
        album_prompt = services.SUNO_ALBUM_PLAN_SYSTEM
        lyrics_prompt = services.SUNO_LYRICS_SYSTEM

        for expected in (
            "comma-separated",
            "BPM",
            "time signature",
            "rhythm",
            "instruments",
            "mix",
            "[Verse 1]",
            "[Chorus]",
            "[Bridge]",
            "[Final Chorus]",
            "[Instrumental Solo]",
            "[Ad-lib]",
        ):
            self.assertIn(expected, album_prompt)
        self.assertIn("regenerate_style", lyrics_prompt)
        self.assertIn("[Spoken Word]", lyrics_prompt)

    def test_gemini_text_uses_standalone_rest_client(self) -> None:
        response = BytesIO(
            json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": '{"title":"Test Album"}'}]
                            }
                        }
                    ]
                }
            ).encode("utf-8")
        )

        with (
            patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model"},
            ),
            patch.object(services.urllib.request, "urlopen", return_value=response) as urlopen,
        ):
            result = services._gemini_text("system", "user")

        self.assertEqual(result, '{"title":"Test Album"}')
        request = urlopen.call_args.args[0]
        self.assertIn("/models/test-model:generateContent", request.full_url)
        payload = json.loads(request.data)
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "system")
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "user")

    def test_gemini_image_decodes_inline_data(self) -> None:
        image_bytes = b"fake-image"
        response = BytesIO(
            json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "inlineData": {
                                            "mimeType": "image/png",
                                            "data": base64.b64encode(image_bytes).decode("ascii"),
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")
        )

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch.object(services.urllib.request, "urlopen", return_value=response),
        ):
            raw, mime = services._gemini_image("cover art", "16:9")

        self.assertEqual(raw, image_bytes)
        self.assertEqual(mime, "image/png")

    def test_suno_generation_classifies_browser_verification_failure(self) -> None:
        first_error = services.SunoAPIError(
            422,
            "POST",
            "https://studio-api.prod.suno.com/api/generate/v2/",
            '{"error_type":"token_validation_failed"}',
        )
        with (
            patch.object(services, "update_token") as refresh,
            patch.object(services, "_suno_token", return_value="token-1"),
            patch.object(
                services,
                "generate_music",
                new=AsyncMock(side_effect=first_error),
            ) as generate,
        ):
            with self.assertRaises(services.SunoGenerationVerificationError):
                asyncio.run(services._submit_suno_generation({"prompt": "test"}))

        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(generate.await_count, 1)

    def test_fenix_generation_uses_web_transport(self) -> None:
        with patch.object(utils, "fetch", new=AsyncMock(return_value={"clips": []})) as fetch:
            asyncio.run(
                utils.generate_music(
                    {"mv": "chirp-fenix", "prompt": "lyrics"},
                    "token",
                    "__client=client-token",
                )
            )

        self.assertEqual(
            fetch.await_args.args[0],
            "https://studio-api.prod.suno.com/api/generate/v2-web/",
        )
        headers = fetch.await_args.args[1]
        self.assertEqual(headers["Cookie"], "__client=client-token")

    def test_legacy_model_keeps_legacy_endpoint(self) -> None:
        with patch.object(utils, "fetch", new=AsyncMock(return_value={"clips": []})) as fetch:
            asyncio.run(
                utils.generate_music(
                    {"mv": "chirp-v3-0", "prompt": ""},
                    "token",
                    "__client=client-token",
                )
            )

        self.assertEqual(
            fetch.await_args.args[0],
            "https://studio-api.prod.suno.com/api/generate/v2/",
        )
        legacy_headers = fetch.await_args.args[1]
        self.assertEqual(
            legacy_headers["Content-Type"],
            "text/plain;charset=UTF-8",
        )
        self.assertNotIn("Cookie", legacy_headers)
        self.assertNotIn("Accept", legacy_headers)
        self.assertFalse(fetch.await_args.kwargs["merge_common_headers"])

    def test_web_transport_requires_explicit_endpoint_override(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"SUNO_GENERATE_PATH": "/api/generate/v2-web/"},
            ),
            patch.object(
                utils,
                "fetch",
                new=AsyncMock(return_value={"clips": []}),
            ) as fetch,
        ):
            asyncio.run(
                utils.generate_music(
                    {"mv": "chirp-fenix", "prompt": "lyrics"},
                    "token",
                    "__client=client-token; session=value",
                )
            )

        self.assertEqual(
            fetch.await_args.args[0],
            "https://studio-api.prod.suno.com/api/generate/v2-web/",
        )
        self.assertEqual(
            fetch.await_args.args[1]["Cookie"],
            "__client=client-token; session=value",
        )

    def test_suno_payload_compacts_oversized_style_at_tag_boundary(self) -> None:
        prepared = services._prepare_suno_payload(
            {
                "mv": "chirp-fenix",
                "prompt": "lyrics",
                "title": "title",
                "tags": ", ".join(f"tag-{index:02d}" for index in range(40)),
                "negative_tags": "",
            }
        )

        self.assertLessEqual(len(prepared["tags"]), services.SUNO_MAX_STYLE_CHARS)
        self.assertFalse(prepared["tags"].endswith(","))
        self.assertIn("tag-00", prepared["tags"])

    def test_server_prefers_valid_env_auth_over_saved_auth(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "SESSION_ID": "session-from-env",
                    "COOKIE": "__client=env-client",
                },
            ),
            patch.object(start_suno_server, "load_dotenv"),
            patch.object(start_suno_server, "validate_auth", return_value=True),
            patch.object(start_suno_server, "load_auth") as load_auth,
        ):
            auth = start_suno_server._load_or_capture_auth(False, 30, "auto")

        self.assertEqual(auth.session_id, "session-from-env")
        self.assertEqual(auth.cookie, "__client=env-client")
        load_auth.assert_not_called()

    def test_server_can_explicitly_use_saved_auth(self) -> None:
        saved = start_suno_server.SunoAuth(
            session_id="session-from-saved",
            cookie="__client=saved-client",
            captured_at=1.0,
        )
        with (
            patch.object(start_suno_server, "load_dotenv"),
            patch.object(start_suno_server, "load_auth", return_value=saved),
            patch.object(start_suno_server, "validate_auth", return_value=True),
        ):
            auth = start_suno_server._load_or_capture_auth(False, 30, "saved")

        self.assertEqual(auth.session_id, "session-from-saved")
        self.assertEqual(auth.cookie, "__client=saved-client")

    def test_server_force_login_waits_for_browser_close(self) -> None:
        captured = start_suno_server.SunoAuth(
            session_id="session-from-browser",
            cookie="__client=browser-client",
            captured_at=1.0,
        )
        with (
            patch.object(
                start_suno_server,
                "capture_auth_with_browser",
                return_value=captured,
            ) as capture,
            patch.object(start_suno_server, "validate_auth", return_value=True),
            patch.object(start_suno_server, "save_auth"),
        ):
            auth = start_suno_server._load_or_capture_auth(
                force_login=True,
                timeout=30,
                auth_source="auto",
            )

        self.assertEqual(auth, captured)
        capture.assert_called_once_with(
            profile_dir=start_suno_server.PROFILE_DIR,
            timeout_sec=30,
            wait_for_browser_close=True,
        )

if __name__ == "__main__":
    unittest.main()
