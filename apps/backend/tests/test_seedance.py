"""Failure-mode table rows 1-8 of SEEDANCE_20_PLAN.md.

New names are read from the module inside each test so a missing name fails
only that row instead of the whole module import.
"""
import importlib
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

REPO_ROOT = BACKEND.parents[1]
NEW_ID = "dreamina-seedance-2-0-260128"
OLD_ID = "seedance-1-5-pro-251215"
TOKENS = 237600
EXPECTED_USD = 1.6632


def production():
    return importlib.import_module("production")


class SeedanceTest(unittest.TestCase):
    def test_row_01_model_id_and_label(self):
        module = production()
        self.assertEqual(module.SEEDANCE_MODEL, NEW_ID)
        self.assertEqual(module.MODEL_LABELS[NEW_ID], "Seedance 2.0")
        self.assertNotIn(OLD_ID, module.MODEL_LABELS)

    def test_row_02_clip_duration_range(self):
        module = production()
        gemini = module.clip_duration_range(module.GEMINI_MODEL)
        seedance = module.clip_duration_range(module.SEEDANCE_MODEL)
        self.assertEqual(gemini, (3, 15))
        self.assertEqual(seedance, (4, 15))
        for minimum, maximum in (gemini, seedance):
            self.assertTrue(minimum <= 11 <= maximum)

    def test_row_03_seedance_usage_estimate(self):
        module = production()
        self.assertEqual(module.SEEDANCE_USD_PER_MILLION_TOKENS, 7.0)
        result = module.generation_usage({"model": module.SEEDANCE_MODEL, "usage": {"completion_tokens": TOKENS}})
        self.assertEqual(result, {"totalTokens": TOKENS, "estimatedUsd": EXPECTED_USD})
        self.assertEqual(
            result["estimatedUsd"],
            round(TOKENS * module.SEEDANCE_USD_PER_MILLION_TOKENS / 1_000_000, 8),
        )

    def test_row_04_seedance_usage_unreadable_is_none(self):
        module = production()
        cases = {
            "usage missing": {"model": NEW_ID},
            "usage not dict": {"model": NEW_ID, "usage": [TOKENS]},
            "completion_tokens str": {"model": NEW_ID, "usage": {"completion_tokens": str(TOKENS)}},
            "completion_tokens float": {"model": NEW_ID, "usage": {"completion_tokens": float(TOKENS)}},
            "completion_tokens missing": {"model": NEW_ID, "usage": {"total_tokens": TOKENS}},
        }
        for label, task in cases.items():
            with self.subTest(f"row 4: {label}"):
                self.assertIsNone(module.generation_usage(task))

    def test_row_05_audio_and_tier_do_not_change_rate(self):
        module = production()
        for label, extra in {
            "generate_audio False": {"generate_audio": False},
            "service_tier flex": {"service_tier": "flex"},
            "both": {"generate_audio": False, "service_tier": "flex"},
        }.items():
            with self.subTest(f"row 5: {label}"):
                task = {"model": module.SEEDANCE_MODEL, "usage": {"completion_tokens": TOKENS}, **extra}
                self.assertEqual(
                    module.generation_usage(task),
                    {"totalTokens": TOKENS, "estimatedUsd": EXPECTED_USD},
                )

    def test_row_06_old_model_id_usage_is_none(self):
        module = production()
        task = {"model": OLD_ID, "usage": {"completion_tokens": TOKENS}}
        self.assertIsNone(module.generation_usage(task))

    def test_row_07_gemini_usage_unchanged(self):
        module = production()
        task = {
            "model": module.GEMINI_MODEL,
            "usage": {
                "total_tokens": 10500,
                "total_input_tokens": 1000,
                "total_output_tokens": 9000,
                "total_thought_tokens": 500,
                "output_tokens_by_modality": [
                    {"modality": "video", "tokens": 8000},
                    {"modality": "audio", "tokens": 1000},
                ],
            },
        }
        # (1000 * 1.5 + 8000 * 12 + (9000 - 8000 + 500) * 9) / 1e6 = 0.111
        self.assertEqual(module.generation_usage(task), {"totalTokens": 10500, "estimatedUsd": 0.111})

    def test_row_08_development_doc_names_new_model(self):
        text = (REPO_ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
        self.assertTrue(NEW_ID in text, f"DEVELOPMENT.md does not mention {NEW_ID}")
        self.assertFalse(OLD_ID in text, f"DEVELOPMENT.md still mentions {OLD_ID}")


if __name__ == "__main__":
    unittest.main()
