"""Failure-mode table rows 1-5 of CLIP_REQUEST_LOG_PLAN.md.

redacted_payload is read from the module inside each test so a missing name
fails only those rows instead of the whole module import.
"""
import copy
import importlib
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

PROMPT = "A dancer turns toward the camera under neon light."


def redacted_payload(payload):
    return importlib.import_module("production").redacted_payload(payload)


def seedance_payload():
    return {
        "model": "dreamina-seedance-2-0-260128",
        "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "role": "first_frame",
             "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        "ratio": "9:16", "resolution": "720p", "duration": 8,
        "generate_audio": True,
    }


def gemini_payload():
    return {
        "model": "veo",
        "input": [
            {"type": "image", "data": "BBBBBB", "mime_type": "image/jpeg"},
            {"type": "text", "text": PROMPT},
        ],
        "background": True,
        "response_format": {"type": "video", "aspect_ratio": "9:16", "resolution": "720p", "duration": "8s"},
    }


class ClipLogRedactionTest(unittest.TestCase):
    def test_row_01_seedance_data_url_is_summarized(self):
        result = redacted_payload(seedance_payload())
        expected = seedance_payload()
        expected["content"][1]["image_url"]["url"] = "<data:image/png, 4 base64 chars>"
        self.assertEqual(result, expected)

    def test_row_02_gemini_image_data_is_summarized(self):
        result = redacted_payload(gemini_payload())
        expected = gemini_payload()
        expected["input"][0]["data"] = "<image/jpeg, 6 base64 chars>"
        self.assertEqual(result, expected)
        self.assertEqual(result["input"][0]["mime_type"], "image/jpeg")
        self.assertEqual(result["input"][1], {"type": "text", "text": PROMPT})

    def test_row_03_originals_are_not_mutated(self):
        for label, payload in (("row 3 seedance", seedance_payload()), ("row 3 gemini", gemini_payload())):
            with self.subTest(label):
                snapshot = copy.deepcopy(payload)
                inner = payload["content"] if "content" in payload else payload["input"]
                inner_snapshot = copy.deepcopy(inner)
                result = redacted_payload(payload)
                self.assertEqual(payload, snapshot)
                self.assertEqual(inner, inner_snapshot)
                self.assertIsNot(result, payload)

    def test_row_04_urls_and_plain_input_are_kept(self):
        cases = {
            "row 4 https": {
                "model": "dreamina-seedance-2-0-260128",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": "https://example.com/frame.png"}},
                ],
                "duration": 8,
            },
            "row 4 asset": {
                "model": "dreamina-seedance-2-0-260128",
                "content": [{"type": "image_url", "image_url": {"url": "asset://asset-1"}}],
                "generate_audio": True,
            },
            "row 4 gemini text input": {
                "model": "veo", "input": PROMPT, "background": True,
                "response_format": {"type": "video", "duration": "8s"},
            },
        }
        for label, payload in cases.items():
            with self.subTest(label):
                self.assertEqual(redacted_payload(payload), copy.deepcopy(payload))

    def test_row_05_data_without_mime_type_is_kept(self):
        payload = {"model": "veo", "input": [{"data": "x"}, {"type": "text", "text": PROMPT}]}
        self.assertEqual(redacted_payload(payload), copy.deepcopy(payload))


if __name__ == "__main__":
    unittest.main()
