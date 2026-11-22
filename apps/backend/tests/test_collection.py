"""Failure-mode table rows 1-30 of COLLECTION_WORKER_PLAN.md.

New names are read from the modules inside each test so a missing name fails
only that row instead of the whole module import.
"""
import asyncio
import logging
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import collection  # noqa: E402
import discovery  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
CONFIG_YAML = 'instagram:\n  user_id: "123"\n  access_token: "SECRET"\n  graph_version: "v27.0"\n'
LOOKUP_PATH = "/v27.0/ig_hashtag_search"


def tag_row(tag, *, success=None, error=None):
    return {"tag": tag, "last_success": success, "last_error": error}


def gate_row(*, running_tag=None, limited=False, next_call_at=NOW - timedelta(minutes=1)):
    return {"running_tag": running_tag, "limited": limited, "next_call_at": next_call_at}


def job_row(tag, *, due_at=None, top_due_at=None, requested_at=None, request_id=None, window_hours=None):
    return {
        "tag": tag,
        "due_at": due_at or NOW + timedelta(hours=1),
        "top_due_at": top_due_at or NOW + timedelta(hours=1),
        "requested_at": requested_at,
        "request_id": request_id,
        "window_hours": window_hours,
    }


def reel(media_id, timestamp="2026-11-20T10:00:00+0000"):
    return {
        "id": media_id, "media_type": "VIDEO", "timestamp": timestamp,
        "permalink": f"https://www.instagram.com/reel/{media_id}/",
    }


class FakeMeta:
    """Graph API fake: hashtag lookup plus paged media edges."""

    def __init__(self, pages=None, *, lookup=None, endless=False):
        self.pages = pages or []
        self.lookup = lookup or (200, {"data": [{"id": "9"}]})
        self.endless = endless
        self.lookups = 0
        self.media_requests = []

    def handler(self, request):
        if request.url.path == LOOKUP_PATH:
            self.lookups += 1
            status, body = self.lookup
            return httpx.Response(status, json=body)
        self.media_requests.append(request)
        index = len(self.media_requests) - 1
        base = f"https://graph.facebook.com{request.url.path}"
        if self.endless:
            return httpx.Response(200, json={
                "data": [reel(f"endless{index}")],
                "paging": {"next": f"{base}?after=cursor{index}"},
            })
        status, items, has_next = self.pages[index]
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "boom", "code": 1}})
        body = {"data": items}
        if has_next:
            body["paging"] = {"next": f"{base}?after=cursor{index}"}
        return httpx.Response(200, json=body)


class DiscoverCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = Path(directory.name) / "config.yaml"
        config.write_text(CONFIG_YAML, encoding="utf-8")
        config_patch = patch.object(discovery, "CONFIG_PATH", config)
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def run_discover(self, meta, **kwargs):
        real_client = httpx.AsyncClient

        def client(*args, **options):
            options["transport"] = httpx.MockTransport(meta.handler)
            return real_client(*args, **options)

        with patch.object(discovery.httpx, "AsyncClient", client):
            return asyncio.run(discovery.discover(q="사주", **kwargs))

    def media_paths(self, meta):
        return {request.url.path for request in meta.media_requests}


class WorkerAliveTest(unittest.TestCase):
    def test_row_01_recent_heartbeat_is_alive(self):
        self.assertIs(collection.worker_alive(NOW - timedelta(seconds=30), NOW), True)

    def test_row_02_never_started_worker_is_not_alive(self):
        self.assertIs(collection.worker_alive(None, NOW), False)

    def test_row_03_stale_heartbeat_is_not_alive(self):
        stale = collection.WORKER_STALE_AFTER
        with self.subTest("row 3 past bound"):
            self.assertIs(collection.worker_alive(NOW - stale - timedelta(seconds=1), NOW), False)
        with self.subTest("row 3 at bound"):
            self.assertIs(collection.worker_alive(NOW - stale, NOW), True)


class TopicStatusTest(unittest.TestCase):
    def status(self, states, gate, alive):
        return collection.topic_status(states, gate, alive, NOW)

    def test_row_04_running_tag_with_dead_worker_is_stopped(self):
        states = [tag_row("A"), tag_row("B")]
        with self.subTest("row 4 dead worker"):
            self.assertEqual(self.status(states, gate_row(running_tag="A"), False), "stopped")
        with self.subTest("row 4 live worker keeps running"):
            self.assertEqual(self.status(states, gate_row(running_tag="A"), True), "running")

    def test_row_05_token_expired_tag(self):
        states = [tag_row("A", success=NOW, error="token_expired"), tag_row("B", success=NOW)]
        with self.subTest("row 5 token expired"):
            self.assertEqual(self.status(states, gate_row(), True), "token_expired")
        with self.subTest("row 5 running outranks token expired"):
            self.assertEqual(self.status(states, gate_row(running_tag="B"), True), "running")
        with self.subTest("row 5 token expired outranks delayed"):
            limited = gate_row(limited=True, next_call_at=NOW + timedelta(minutes=5))
            self.assertEqual(self.status(states, limited, True), "token_expired")

    def test_row_06_dead_worker_outranks_token_expired(self):
        states = [tag_row("A", error="token_expired"), tag_row("B")]
        self.assertEqual(self.status(states, gate_row(), False), "stopped")

    def test_row_07_limited_gate_is_delayed(self):
        states = [tag_row("A", success=NOW), tag_row("B", success=NOW)]
        with self.subTest("row 7 limited"):
            limited = gate_row(limited=True, next_call_at=NOW + timedelta(minutes=5))
            self.assertEqual(self.status(states, limited, True), "delayed")
        with self.subTest("row 7 other http error"):
            failing = [tag_row("A", success=NOW, error="HTTP 502"), tag_row("B", success=NOW)]
            self.assertEqual(self.status(failing, gate_row(), True), "delayed")

    def test_row_08_success_counts(self):
        cases = {
            "ready": [tag_row("A", success=NOW), tag_row("B", success=NOW)],
            "partial": [tag_row("A", success=NOW), tag_row("B")],
            "empty": [tag_row("A"), tag_row("B")],
        }
        for expected, states in cases.items():
            with self.subTest(f"row 8 {expected}"):
                self.assertEqual(self.status(states, gate_row(), True), expected)


class MayCallTest(unittest.TestCase):
    def test_row_09_manual_request_skips_call_interval(self):
        gate = gate_row(next_call_at=NOW + timedelta(minutes=5))
        self.assertIs(collection.may_call(gate, True, NOW), True)

    def test_row_10_manual_request_waits_while_limited(self):
        gate = gate_row(limited=True, next_call_at=NOW + timedelta(minutes=5))
        self.assertIs(collection.may_call(gate, True, NOW), False)

    def test_row_11_automatic_run_waits_for_slot(self):
        gate = gate_row(next_call_at=NOW + timedelta(minutes=5))
        with self.subTest("row 11 waits"):
            self.assertIs(collection.may_call(gate, False, NOW), False)
        with self.subTest("row 11 slot reached"):
            self.assertIs(collection.may_call(gate_row(next_call_at=NOW), False, NOW), True)


class PickJobTest(unittest.TestCase):
    def test_row_12_manual_request_goes_first(self):
        rows = [
            job_row("A", due_at=NOW - timedelta(hours=1)),
            job_row("B", requested_at=NOW - timedelta(minutes=5), request_id="req-B", window_hours=24),
        ]
        with self.subTest("row 12 manual first"):
            self.assertEqual(
                collection.pick_job(rows, NOW),
                {"tag": "B", "mode": "recent", "request_id": "req-B", "hours": 24},
            )
        with self.subTest("row 12 oldest request first"):
            rows.append(job_row("C", requested_at=NOW - timedelta(minutes=9), request_id="req-C", window_hours=24))
            self.assertEqual(collection.pick_job(rows, NOW)["tag"], "C")

    def test_row_13_long_manual_window_uses_top(self):
        rows = [job_row("A", requested_at=NOW, request_id="req-A", window_hours=168)]
        self.assertEqual(
            collection.pick_job(rows, NOW),
            {"tag": "A", "mode": "top", "request_id": "req-A", "hours": 168},
        )

    def test_row_14_due_top_media(self):
        rows = [job_row("A"), job_row("B", top_due_at=NOW - timedelta(minutes=1))]
        self.assertEqual(
            collection.pick_job(rows, NOW),
            {"tag": "B", "mode": "top", "request_id": None, "hours": 24},
        )

    def test_row_15_earliest_due_time_wins(self):
        with self.subTest("row 15 earlier recent"):
            rows = [
                job_row("B", top_due_at=NOW - timedelta(hours=1)),
                job_row("A", due_at=NOW - timedelta(hours=2)),
            ]
            self.assertEqual(
                collection.pick_job(rows, NOW),
                {"tag": "A", "mode": "recent", "request_id": None, "hours": 24},
            )
        with self.subTest("row 15 tie prefers recent"):
            same = NOW - timedelta(hours=1)
            rows = [job_row("A", due_at=same, top_due_at=same)]
            self.assertEqual(collection.pick_job(rows, NOW)["mode"], "recent")
        with self.subTest("row 15 tie orders by tag"):
            same = NOW - timedelta(hours=1)
            rows = [job_row("B", due_at=same), job_row("A", due_at=same)]
            self.assertEqual(collection.pick_job(rows, NOW)["tag"], "A")

    def test_row_16_nothing_due(self):
        with self.subTest("row 16 future only"):
            self.assertIsNone(collection.pick_job([job_row("A"), job_row("B")], NOW))
        with self.subTest("row 16 no rows"):
            self.assertIsNone(collection.pick_job([], NOW))


class ScaleTest(unittest.TestCase):
    def test_row_17_call_slot_fills_one_day(self):
        day = timedelta(hours=24)
        with self.subTest("row 17 exact 24h/70"):
            self.assertEqual(collection.call_slot(14, 6), day / (14 * (24 / 6 + 1)))
            self.assertEqual(collection.call_slot(14, 6), day / 70)
        for tag_count, hours in ((1, 24), (14, 6), (14, 1), (7, 5), (3, 168)):
            with self.subTest(f"row 17 ({tag_count}, {hours})"):
                runs = tag_count * (24 / hours + 1)
                # timedelta keeps microseconds, so float division may round by a few microseconds.
                self.assertAlmostEqual(
                    collection.call_slot(tag_count, hours) * runs, day, delta=timedelta(milliseconds=1),
                )

    def test_row_18_stale_after_from_constants(self):
        expected = timedelta(seconds=2 * (
            collection.WORKER_SLEEP + discovery.LOOKUP_TIMEOUT
            + discovery.MAX_MEDIA_PAGES * discovery.MEDIA_TIMEOUT
        ))
        self.assertEqual(collection.WORKER_STALE_AFTER, expected)


class DiscoverPagingTest(DiscoverCase):
    def test_row_19_recent_reads_following_pages(self):
        page1 = [reel("AAA"), {"id": "img", "media_type": "IMAGE", "permalink": "https://www.instagram.com/p/img/"}]
        page2 = [reel("BBB"), {"id": "vid", "media_type": "VIDEO", "permalink": "https://www.instagram.com/p/vid/"}]
        meta = FakeMeta([(200, page1, True), (200, page2, False)])
        result = self.run_discover(meta, order="recent")
        self.assertEqual(sorted(item["id"] for item in result["items"]), ["AAA", "BBB"])
        self.assertEqual(len(meta.media_requests), 2)
        self.assertEqual(self.media_paths(meta), {"/v27.0/9/recent_media"})
        self.assertEqual(parse_qs(meta.media_requests[1].url.query.decode())["after"], ["cursor0"])

    def test_row_20_first_page_failure_fails_run(self):
        meta = FakeMeta([(500, [], False)])
        with self.assertRaises(HTTPException) as caught:
            self.run_discover(meta, order="recent")
        self.assertEqual(caught.exception.status_code, 502)

    def test_row_21_later_page_failure_keeps_first_page(self):
        meta = FakeMeta([(200, [reel("AAA")], True), (500, [], False)])
        result = self.run_discover(meta, order="recent")
        self.assertEqual(len(meta.media_requests), 2)
        self.assertEqual([item["id"] for item in result["items"]], ["AAA"])

    def test_row_22_since_stops_after_old_page(self):
        old = [reel("AAA", "2026-11-19T01:00:00+0000"), reel("BBB", "2026-11-19T02:00:00+0000")]
        meta = FakeMeta([(200, old, True), (200, [reel("CCC")], False)])
        self.run_discover(meta, order="recent", since=datetime(2026, 11, 20, tzinfo=UTC))
        self.assertEqual(len(meta.media_requests), 1)

    def test_row_23_media_requests_capped(self):
        for order in ("recent", "top"):
            with self.subTest(f"row 23 {order}"):
                meta = FakeMeta(endless=True)
                self.run_discover(meta, order=order)
                self.assertEqual(len(meta.media_requests), discovery.MAX_MEDIA_PAGES)


class DiscoverHashtagIdTest(DiscoverCase):
    def test_row_24_stored_hashtag_id_skips_lookup(self):
        meta = FakeMeta([(200, [reel("AAA")], False)])
        result = self.run_discover(meta, order="recent", hashtag_ids={"사주": "1"})
        self.assertEqual(meta.lookups, 0)
        self.assertEqual(self.media_paths(meta), {"/v27.0/1/recent_media"})
        self.assertEqual(result["hashtagIds"], {"사주": "1"})

    def test_row_25_looked_up_hashtag_id_is_returned(self):
        meta = FakeMeta([(200, [reel("AAA")], False)])
        result = self.run_discover(meta, order="recent", hashtag_ids=None)
        self.assertEqual(meta.lookups, 1)
        self.assertEqual(result["hashtagIds"], {"사주": "9"})

    def test_row_26_token_error_is_401(self):
        meta = FakeMeta(lookup=(400, {"error": {"message": "Error validating access token", "code": 190}}))
        with self.assertRaises(HTTPException) as caught:
            self.run_discover(meta, order="recent")
        self.assertEqual(caught.exception.status_code, 401)
        self.assertIn("Meta 액세스 토큰이 유효하지 않거나 만료됐습니다.", caught.exception.detail)

    def test_row_27_other_meta_error_stays_502(self):
        meta = FakeMeta(lookup=(400, {"error": {"message": "Invalid parameter", "code": 100}}))
        with self.assertRaises(HTTPException) as caught:
            self.run_discover(meta, order="recent")
        self.assertEqual(caught.exception.status_code, 502)


class ErrorLabelTest(unittest.TestCase):
    def test_row_28_error_label(self):
        for status, expected in ((401, "token_expired"), (502, "HTTP 502"), (500, "HTTP 500")):
            with self.subTest(f"row 28 {status}"):
                self.assertEqual(collection.error_label(status), expected)


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class ConfigureLoggingTest(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        httpx_logger = logging.getLogger("httpx")
        saved = (root.level, list(root.handlers), httpx_logger.level)

        def restore():
            root.setLevel(saved[0])
            root.handlers[:] = saved[1]
            httpx_logger.setLevel(saved[2])

        self.addCleanup(restore)

    def test_row_29_httpx_request_url_not_logged(self):
        collection.configure_logging()
        handler = RecordingHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)

        async def request():
            transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"data": []}))
            async with httpx.AsyncClient(transport=transport) as client:
                await client.get("https://graph.facebook.com/v27.0/ig_hashtag_search?access_token=SECRET")

        asyncio.run(request())
        with self.subTest("row 29 no httpx info"):
            self.assertEqual(
                [r.getMessage() for r in handler.records if r.name.startswith("httpx") and r.levelno <= logging.INFO],
                [],
            )
        with self.subTest("row 29 no token"):
            self.assertFalse([r for r in handler.records if "SECRET" in r.getMessage()])

    def test_row_30_collection_logger_keeps_info(self):
        collection.configure_logging()
        self.assertTrue(logging.getLogger(collection.__name__).isEnabledFor(logging.INFO))


class DevelopmentDocTest(unittest.TestCase):
    NEW_SENTENCES = ("수동 수집도 워커가 실행한다", "수집 워커 중지됨", "인기 게시물을 하루 한 번")
    OLD_SENTENCE = "최대 1분 동안 실행권을 기다린다"

    def test_development_md_describes_worker_collection(self):
        text = (REPO_ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
        for sentence in self.NEW_SENTENCES:
            with self.subTest(f"doc has {sentence}"):
                self.assertTrue(sentence in text, f"DEVELOPMENT.md lacks: {sentence}")
        with self.subTest("doc drops old wait sentence"):
            self.assertFalse(self.OLD_SENTENCE in text, f"DEVELOPMENT.md still has: {self.OLD_SENTENCE}")


if __name__ == "__main__":
    unittest.main()
