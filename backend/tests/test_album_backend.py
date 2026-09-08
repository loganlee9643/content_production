from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_video_font_keeps_selected_hangul_capable_font(self) -> None:
        selected_font = Path("frontend/public/fonts/NotoSerifKR.ttf")
        with (
            patch.object(services, "_korean_video_font_path") as korean_font,
            patch.object(services, "_video_font_path", return_value=selected_font) as video_font,
        ):
            result = services._video_text_font_path("noto_serif_kr", "첫 빗방울")

        self.assertEqual(result, selected_font)
        korean_font.assert_not_called()
        video_font.assert_called_once_with("noto_serif_kr")

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

    def test_image_selection_is_saved_per_track_generation(self) -> None:
        album = self.create_album()
        track = asyncio.run(
            router.create_track(
                album["id"],
                schemas.TrackCreate(sequence=1, title="Rain Track"),
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
        first = services.save_uploaded_asset(
            album["id"], b"first", "first.png", "image/png"
        )
        second = services.save_uploaded_asset(
            album["id"], b"second", "second.png", "image/png"
        )

        asyncio.run(
            router.select_image_for_track(
                album["id"],
                first["id"],
                schemas.ImageSelectionRequest(
                    track_id=track["id"],
                    generation_id=generation["id"],
                ),
            )
        )
        updated = asyncio.run(
            router.select_image_for_track(
                album["id"],
                second["id"],
                schemas.ImageSelectionRequest(
                    track_id=track["id"],
                    generation_id=generation["id"],
                ),
            )
        )["data"]

        self.assertEqual(updated["metadata"]["selected_for_track_id"], track["id"])
        self.assertEqual(
            updated["metadata"]["selected_for_generation_id"], generation["id"]
        )
        self.assertNotIn(
            "selected_for_generation_id",
            db.get_one("assets", first["id"])["metadata"],
        )

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

    def test_thumbnail_background_prompt_uses_album_plan_context(self) -> None:
        album = self.create_album()
        db.update(
            "albums",
            album["id"],
            {
                "style_prompt": "rainy city pop, warm analog synth",
                "visual_concept": "two people under an old umbrella at night",
                "thumbnail_image_prompt": (
                    "Rainy Seoul street at night, old yellow umbrella, "
                    "warm reflections, nostalgic romantic mood"
                ),
            },
        )
        track = asyncio.run(
            router.create_track(
                album["id"],
                schemas.TrackCreate(
                    sequence=1,
                    title="Old Umbrella",
                    concept="A quiet promise under rain",
                ),
            )
        )["data"]

        prompt = services._album_thumbnail_prompt(
            db.get_one("albums", album["id"]), [track]
        )

        self.assertIn("old yellow umbrella", prompt)
        self.assertIn("Old Umbrella", prompt)
        self.assertIn("A quiet promise under rain", prompt)
        self.assertIn("no words", prompt)
        self.assertIn("negative space", prompt)

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
        template_album = self.create_album()
        preview = services.save_uploaded_asset(
            template_album["id"],
            b"preview",
            "template-preview.png",
            "image/png",
            "template_preview",
        )
        album = self.create_album()
        track = asyncio.run(
            router.create_track(
                album["id"],
                schemas.TrackCreate(sequence=1, title="첫 번째 트랙"),
            )
        )["data"]
        template = asyncio.run(
            router.create_video_template(
                template_album["id"],
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
            router.list_template_previews(template_album["id"])
        )["data"]
        covers = asyncio.run(router.list_images(template_album["id"]))["data"]

        self.assertEqual(templates[0]["name"], "기본 템플릿")
        self.assertEqual(templates[0]["compose"]["visualizer_style"], "bars")
        self.assertEqual(templates[0]["title_source"], "template")
        self.assertEqual(templates[0]["artist_source"], "hidden")
        self.assertEqual(templates[0]["preview_asset_id"], preview["id"])
        self.assertEqual(template_previews[0]["id"], preview["id"])
        self.assertNotIn(preview["id"], {asset["id"] for asset in covers})
        self.assertEqual(assignments[track["id"]], template["id"])
        self.assertNotEqual(template["album_id"], album["id"])

    def test_generations_support_multiple_selection_and_title_update(self) -> None:
        album = self.create_album()
        track = asyncio.run(
            router.create_track(
                album["id"],
                schemas.TrackCreate(sequence=1, title="복수 선택 트랙"),
            )
        )["data"]
        generations = [
            db.insert(
                "generations",
                {
                    "id": db.new_id(),
                    "track_id": track["id"],
                    "job_id": services.create_job(
                        "track_generate", "track", track["id"]
                    )["id"],
                    "request_id": None,
                    "clip_id": f"clip-{index}",
                    "status": "complete",
                    "title": f"후보 {index}",
                    "audio_url": None,
                    "image_url": None,
                    "local_audio_path": None,
                    "generated_lyrics": None,
                    "tags": None,
                    "raw_response_json": "{}",
                    "is_selected": 0,
                    "created_at": db.now_iso(),
                    "completed_at": db.now_iso(),
                },
            )
            for index in range(1, 3)
        ]

        for generation in generations:
            asyncio.run(router.select_generation(track["id"], generation["id"]))

        selected = asyncio.run(router.list_generations(track["id"]))["data"]
        self.assertEqual(sum(item["is_selected"] for item in selected), 2)
        listed_track = asyncio.run(router.list_tracks(album["id"]))["data"][0]
        self.assertEqual(
            [item["id"] for item in listed_track["selected_generations"]],
            [item["id"] for item in generations],
        )
        self.assertEqual(
            db.get_one("tracks", track["id"])["selected_generation_id"],
            generations[-1]["id"],
        )

        asyncio.run(router.select_generation(track["id"], generations[-1]["id"]))
        self.assertEqual(
            db.get_one("tracks", track["id"])["selected_generation_id"],
            generations[0]["id"],
        )

        updated = asyncio.run(
            router.update_generation(
                generations[0]["id"],
                schemas.GenerationUpdate(title="변경된 후보 제목"),
            )
        )["data"]
        self.assertEqual(updated["title"], "변경된 후보 제목")

    def test_batch_video_render_applies_template_to_generated_image(self) -> None:
        album = self.create_album()
        template_album = self.create_album()
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
                template_album["id"],
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
            self.assertEqual(render_request.generation_id, generation["id"])
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
                    generation_ids=[generation["id"]],
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
                    repeat_count=2,
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
        self.assertEqual(result_asset["metadata"]["duration_seconds"], 60.0)
        self.assertEqual(result_asset["metadata"]["repeat_count"], 2)
        self.assertTrue(
            any("concat=n=4:v=1:a=1[vout][aout]" in value for value in captured_command)
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

    def test_suno_clip_ready_requires_complete_status(self) -> None:
        url = "https://cdn1.suno.ai/clip.mp3"
        self.assertFalse(services._suno_clip_ready("submitted", url))
        self.assertFalse(services._suno_clip_ready("queued", url))
        self.assertFalse(services._suno_clip_ready("streaming", url))
        self.assertTrue(services._suno_clip_ready("complete", ""))
        self.assertTrue(services._suno_clip_ready("complete_success", url))

    def test_suno_audio_source_skips_encrypted_opus_and_uses_mp3(self) -> None:
        mp3_url = "https://cdn1.suno.ai/example.mp3"
        url, content_type, suffix = services._suno_audio_source(
            {
                "id": "example",
                "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
                "media_urls": [
                    {
                        "url": "https://d2lwuy8qc234o3.cloudfront.net/1/clip/example.m4a",
                        "content_type": "m4a-opus",
                    },
                    {"url": mp3_url, "content_type": "mp3"},
                ],
            }
        )
        self.assertEqual(url, mp3_url)
        self.assertEqual(content_type, "audio/mpeg")
        self.assertEqual(suffix, "mp3")

    def test_browser_token_wraps_timestamp(self) -> None:
        token = utils.browser_token(now_ms=1_700_000_000_000)
        outer = json.loads(token)
        inner = json.loads(base64.b64decode(outer["token"]))
        self.assertEqual(inner["timestamp"], 1_700_000_000_000)

    def test_device_id_prefers_suno_device_id_cookie(self) -> None:
        path = Path(self.temp_dir.name) / "suno-device-id"
        with (
            patch.object(utils, "DEVICE_ID_FILE", path),
            patch.dict(os.environ, {"SUNO_DEVICE_ID": ""}, clear=False),
        ):
            os.environ.pop("SUNO_DEVICE_ID", None)
            value = utils.device_id("__client=abc; suno_device_id=web-device")
        self.assertEqual(value, "web-device")
        self.assertEqual(path.read_text(encoding="utf-8").strip(), "web-device")

    def test_device_id_persists_across_reads(self) -> None:
        path = Path(self.temp_dir.name) / "suno-device-id"
        with (
            patch.object(utils, "DEVICE_ID_FILE", path),
            patch.dict(os.environ, {"SUNO_DEVICE_ID": ""}, clear=False),
        ):
            os.environ.pop("SUNO_DEVICE_ID", None)
            first = utils.device_id()
            second = utils.device_id()
        self.assertEqual(first, second)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), first)

    def test_get_feed_sends_web_client_headers(self) -> None:
        with (
            patch.object(utils, "device_id", return_value="device-uuid"),
            patch.object(utils, "browser_token", return_value='{"token":"abc"}'),
            patch.object(utils, "fetch", new=AsyncMock(return_value=[])) as fetch,
        ):
            asyncio.run(
                utils.get_feed("clip-1", "jwt-token", cookie="__client=abc")
            )

        headers = fetch.await_args.args[1]
        self.assertEqual(headers["Authorization"], "Bearer jwt-token")
        self.assertEqual(headers["Cookie"], "__client=abc")
        self.assertEqual(headers["device-id"], "device-uuid")
        self.assertEqual(headers["browser-token"], '{"token":"abc"}')

    def test_suno_audio_source_empty_when_only_locked_stream_exists(self) -> None:
        url, content_type, suffix = services._suno_audio_source(
            {
                "id": "clip-locked",
                "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
                "media_urls": [
                    {
                        "url": "https://d2lwuy8qc234o3.cloudfront.net/1/clip/clip-locked.m4a",
                        "content_type": "m4a-opus",
                    }
                ],
            }
        )
        self.assertEqual(url, "")
        self.assertEqual(content_type, "audio/mpeg")
        self.assertEqual(suffix, "mp3")
        self.assertTrue(
            services._suno_clip_ready(
                "complete",
                {
                    "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
                    "media_urls": [
                        {
                            "url": "https://d2lwuy8qc234o3.cloudfront.net/1/clip/clip-locked.m4a",
                            "content_type": "m4a-opus",
                        }
                    ],
                },
            )
        )

    def test_looks_like_audio_rejects_encrypted_stream(self) -> None:
        self.assertTrue(services._looks_like_audio(b"ID3" + b"\x00" * 64))
        self.assertTrue(services._looks_like_audio(b"\x00\x00\x00\x20ftyp" + b"\x00" * 64))
        self.assertFalse(services._looks_like_audio(b"E\x1d\xbe\x0f" + b"\x00" * 64))

    def test_download_uses_suno_browser_headers(self) -> None:
        destination = db.STORAGE_DIR / "clip.mp3"
        response = MagicMock()
        response.read.return_value = b"ID3fake" + b"\x00" * 64
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with (
            patch.object(services.suno_auth, "get_cookie", return_value="__client=abc"),
            patch.object(
                services.urllib.request, "urlopen", return_value=response
            ) as urlopen,
        ):
            services._download("https://cdn1.suno.ai/clip.mp3", destination)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), services.SUNO_USER_AGENT)
        self.assertEqual(request.get_header("Referer"), "https://suno.com/")
        self.assertEqual(request.get_header("Origin"), "https://suno.com")
        self.assertEqual(request.get_header("Cookie"), "__client=abc")
        self.assertTrue(destination.read_bytes().startswith(b"ID3fake"))

    def test_download_sends_device_id_and_browser_token(self) -> None:
        destination = db.STORAGE_DIR / "clip-headers.mp3"
        response = MagicMock()
        response.read.return_value = b"ID3fake" + b"\x00" * 64
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with (
            patch.object(services.suno_auth, "get_cookie", return_value="__client=abc"),
            patch.object(
                services, "web_client_headers", return_value={"device-id": "dev", "browser-token": '{"token":"x"}'}
            ),
            patch.object(
                services.urllib.request, "urlopen", return_value=response
            ) as urlopen,
        ):
            services._download("https://cdn1.suno.ai/clip.mp3", destination)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Device-id"), "dev")
        self.assertEqual(request.get_header("Browser-token"), '{"token":"x"}')

    def test_generation_audio_source_falls_back_to_cdn_mp3(self) -> None:
        url, content_type, suffix = services._generation_audio_source(
            {
                "clip_id": "clip-locked",
                "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
                "raw_response": {
                    "id": "clip-locked",
                    "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
                    "media_urls": [
                        {
                            "url": "https://d2lwuy8qc234o3.cloudfront.net/1/clip/clip-locked.m4a",
                            "content_type": "m4a-opus",
                        }
                    ],
                },
            }
        )
        self.assertEqual(url, "https://cdn1.suno.ai/clip-locked.mp3")
        self.assertEqual(content_type, "audio/mpeg")
        self.assertEqual(suffix, "mp3")

    def test_suno_media_url_accepts_cloudfront_and_suno_hosts(self) -> None:
        self.assertEqual(
            services._suno_media_url("https://cdn1.suno.ai/clip.wav"),
            "https://cdn1.suno.ai/clip.wav",
        )
        self.assertEqual(
            services._suno_media_url(
                "https://d2lwuy8qc234o3.cloudfront.net/1/clip/clip.wav"
            ),
            "https://d2lwuy8qc234o3.cloudfront.net/1/clip/clip.wav",
        )
        self.assertEqual(
            services._suno_media_url("https://audiopipe.suno.ai/clip.wav"),
            "https://audiopipe.suno.ai/clip.wav",
        )
        self.assertEqual(
            services._suno_media_url(
                "https://suno-data-uploads.s3.amazonaws.com/studio/uploads/clip.wav"
            ),
            "https://suno-data-uploads.s3.amazonaws.com/studio/uploads/clip.wav",
        )
        self.assertEqual(
            services._suno_media_url("https://studio-api.prod.suno.com/api/forbidden"),
            "",
        )
        self.assertEqual(services._suno_media_url("https://example.com/clip.wav"), "")

    def test_wav_file_url_reads_ready_and_pending(self) -> None:
        self.assertEqual(
            utils.wav_file_url({"wav_file_url": "https://cdn1.suno.ai/z.wav"}),
            "https://cdn1.suno.ai/z.wav",
        )
        self.assertEqual(utils.wav_file_url({}), "")
        self.assertEqual(utils.wav_file_url({"wav_file_url": ""}), "")
        self.assertEqual(utils.wav_file_url(None), "")
        self.assertEqual(
            utils.wav_file_url({"wav_file_url": {"url": "https://cdn1.suno.ai/z.wav"}}),
            "https://cdn1.suno.ai/z.wav",
        )

    def test_request_wav_convert_posts_web_download_path(self) -> None:
        path = Path(self.temp_dir.name) / "suno-device-id"
        with (
            patch.object(utils, "DEVICE_ID_FILE", path),
            patch.dict(os.environ, {"SUNO_DEVICE_ID": ""}, clear=False),
            patch.object(utils, "fetch", new=AsyncMock(return_value=None)) as fetch,
        ):
            os.environ.pop("SUNO_DEVICE_ID", None)
            asyncio.run(
                utils.request_wav_convert(
                    "clip-1",
                    "jwt-token",
                    cookie="__client=abc; suno_device_id=web-device",
                )
            )
        self.assertEqual(
            fetch.await_args.args[0],
            "https://studio-api.prod.suno.com/api/gen/clip-1/convert_wav/",
        )
        self.assertEqual(fetch.await_args.kwargs["method"], "POST")
        headers = fetch.await_args.args[1]
        self.assertEqual(headers["Cookie"], "__client=abc; suno_device_id=web-device")
        self.assertEqual(headers["device-id"], "web-device")
        self.assertIn("browser-token", headers)

    def test_suno_wav_download_url_polls_until_ready(self) -> None:
        with (
            patch.object(services, "request_wav_convert", new=AsyncMock()),
            patch.object(
                services,
                "get_wav_file",
                new=AsyncMock(
                    side_effect=[{}, {"wav_file_url": "https://cdn1.suno.ai/clip-1.wav"}]
                ),
            ),
            patch.object(services.asyncio, "sleep", new=AsyncMock()),
            patch.object(services.suno_auth, "get_cookie", return_value="__client=abc"),
        ):
            url = asyncio.run(services._suno_wav_download_url("clip-1", "jwt-token"))
        self.assertEqual(url, "https://cdn1.suno.ai/clip-1.wav")

    def test_store_generation_audio_falls_back_to_wav_after_mp3_forbidden(self) -> None:
        album = self.create_album()
        track = asyncio.run(
            router.create_track(
                album["id"],
                schemas.TrackCreate(sequence=1, title="Rain Track"),
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
                "clip_id": "clip-locked",
                "status": "complete",
                "title": track["title"],
                "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
                "image_url": None,
                "local_audio_path": None,
                "generated_lyrics": None,
                "tags": None,
                "raw_response_json": db.encode_json(
                    {
                        "id": "clip-locked",
                        "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
                        "media_urls": [
                            {
                                "url": "https://d2lwuy8qc234o3.cloudfront.net/1/clip/clip-locked.m4a",
                                "content_type": "m4a-opus",
                            }
                        ],
                    }
                ),
                "is_selected": 0,
                "created_at": db.now_iso(),
                "completed_at": db.now_iso(),
            },
        )

        def fake_download(url: str, destination: Path) -> None:
            if url.endswith(".mp3"):
                raise urllib.error.HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"RIFF" + b"\x00" * 64)

        with (
            patch.object(services, "_download", side_effect=fake_download),
            patch.object(
                services,
                "_suno_wav_download_url",
                new=AsyncMock(return_value="https://cdn1.suno.ai/clip-locked.wav"),
            ),
        ):
            stored = asyncio.run(services.store_generation_audio(generation["id"], "jwt"))

        self.assertTrue(str(stored["local_audio_path"]).endswith(".wav"))
        self.assertEqual(stored["audio_url"], "https://cdn1.suno.ai/clip-locked.wav")

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
        self.assertNotIn("device-id", headers)
        self.assertNotIn("browser-token", headers)

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
