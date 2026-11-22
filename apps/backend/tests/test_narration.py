"""Failure-mode table rows 1-4 and 14 of NARRATION_PROMPT_PLAN.md and rows 5-13 and 15 of
NARRATION_PROMPT_V2_PLAN.md.

New names are read from the modules inside each test so a missing name fails
only that row instead of the whole module import.
"""
import importlib
import re
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import narration_timing  # noqa: E402

SECONDS = 11
SAVED_56_SYLLABLES = (
    "사주에 살이 있다는 말에 무섭고 불길하게 느껴지셨나요? "
    "하지만 살은 액운이 아니라 남들보다 뛰어나게 타고난 특별한 기질의 표시입니다."
)
SHORT_42_SYLLABLES = "살은 액운이 아니라 타고난 기질의 표시입니다. 오늘은 도화살과 역마살의 진짜 뜻을 알려 드릴게요."
AUDIO_LINE = "Audio: only the main character's voice. No background music. No other voices."
PLANNING = {"age": "30대", "role": "역술가", "tone": "차분함", "staging": "정면 상반신"}


def production():
    return importlib.import_module("production")


def scene(narration):
    return {"scene_direction": "어두운 서재", "motion_direction": "천천히 고개를 든다", "narration": narration}


def draft(*narrations, cta=""):
    return {
        "direction": "사주 상식 숏폼",
        "scenes": [scene(text) for text in narrations],
        "durations": [SECONDS] * len(narrations),
        "cta": cta,
    }


class NarrationBudgetTest(unittest.TestCase):
    def test_row_01_budget_uses_spoken_units_per_second(self):
        self.assertEqual(narration_timing.SPOKEN_UNITS_PER_SECOND, 4.5)
        for seconds, literal in ((11, 49), (10, 45), (12, 54)):
            with self.subTest(row=1, seconds=seconds):
                expected = max(1, int(seconds * narration_timing.SPOKEN_UNITS_PER_SECOND))
                self.assertEqual(expected, literal)
                self.assertEqual(narration_timing.narration_budget(seconds), literal)

    def test_row_02_character_target_uses_constants(self):
        self.assertEqual(narration_timing.ELEVEN_SECOND_RANGE, (57, 54, 60))
        self.assertEqual(narration_timing.CHARACTERS_PER_SECOND, 5.25)
        with self.subTest(row=2, seconds=11):
            self.assertEqual(narration_timing.narration_character_target(11), narration_timing.ELEVEN_SECOND_RANGE)
            self.assertEqual(narration_timing.narration_character_target(11), (57, 54, 60))
        with self.subTest(row=2, seconds=10):
            target = int(10 * narration_timing.CHARACTERS_PER_SECOND)
            computed = (target, target - 10 // 2 - 2, target + 10 // 2 + 2)
            self.assertEqual(computed, (52, 45, 59))
            self.assertEqual(narration_timing.narration_character_target(10), (52, 45, 59))

    def test_row_03_saved_56_syllable_script_is_reported_over_budget(self):
        saved = draft(SAVED_56_SYLLABLES)
        issues = narration_timing.timing_issues(saved, saved["durations"])
        self.assertEqual([issue[0] for issue in issues], [0])
        index, units, limit = issues[0]
        self.assertEqual(units, narration_timing.narration_units(SAVED_56_SYLLABLES))
        self.assertEqual(limit, narration_timing.narration_budget(SECONDS))
        self.assertGreater(units, limit)

    def test_row_04_short_42_syllable_script_fits(self):
        short = draft(SHORT_42_SYLLABLES)
        self.assertEqual(narration_timing.timing_issues(short, short["durations"]), [])
        _, minimum, maximum = narration_timing.narration_character_target(SECONDS)
        self.assertEqual((minimum, maximum), (54, 60))
        characters = narration_timing.narration_characters(SHORT_42_SYLLABLES)
        self.assertGreaterEqual(characters, minimum)
        self.assertLessEqual(characters, maximum)


class DialogueSentencesTest(unittest.TestCase):
    def test_row_05_two_sentences_are_split(self):
        self.assertEqual(
            narration_timing.dialogue_sentences("가나다. 라마바사아자차카."), ["가나다.", "라마바사아자차카."]
        )

    def test_row_06_newline_cta_is_its_own_sentence(self):
        self.assertEqual(
            narration_timing.dialogue_sentences("문장 하나.\n저장해 두세요."), ["문장 하나.", "저장해 두세요."]
        )

    def test_row_07_blank_text_has_no_sentences(self):
        for text in ("", "   ", "\n \n"):
            with self.subTest(row=7, text=repr(text)):
                self.assertEqual(narration_timing.dialogue_sentences(text), [])

    def test_row_08_double_quotes_become_single_quotes(self):
        cases = (
            ('그는 "괜찮다"고 했다.', ["그는 '괜찮다'고 했다."]),
            ("“좋아”", ["'좋아'"]),
            ("그는 “좋아”라고 했다. 다음 문장.", ["그는 '좋아'라고 했다.", "다음 문장."]),
        )
        for text, expected in cases:
            with self.subTest(row=8, text=text):
                sentences = narration_timing.dialogue_sentences(text)
                self.assertEqual(sentences, expected)
                for mark in ('"', "“", "”"):
                    self.assertFalse(any(mark in sentence for sentence in sentences))

    def test_row_09_thirty_sentences_keep_order(self):
        sentences = [f"{chr(0xAC00 + index)}." for index in range(30)]
        result = narration_timing.dialogue_sentences(" ".join(sentences))
        self.assertEqual(len(result), 30)
        self.assertEqual(result, sentences)


DIALOGUE_HEADER = (
    "Dialogue: the main character speaks Korean and says exactly the quoted lines below, in order, "
    "word for word, including every particle, and nothing else. Number of lines: {n}."
)
DIALOGUE_PACE = "Speak naturally and clearly, at a conversational pace."
ONLY_SPEECH_LINE = (
    "These quoted lines are the only speech in this video. Do not add, skip, reorder, replace, "
    "or repeat any word. No ad-libs and no extra sentences."
)
AFTER_LAST_LINE = (
    "After reading all the quoted lines, the main character stays silent "
    "for the rest of the clip and keeps a natural expression."
)
SILENT_LINE = "The main character stays silent for the whole clip and keeps a natural expression."
OLD_TIMED_SPAN = re.compile(r"\[\d+\.\d-\d+\.\ds\]")


class ScenePromptTest(unittest.TestCase):
    def head(self, current, index):
        return [
            "Vertical 9:16 short-form video, one scene.",
            f"Overall direction: {current['direction']}",
            f"Main character and staging: {PLANNING['age']} {PLANNING['role']}, {PLANNING['tone']}, {PLANNING['staging']}.",
            f"Scene: {current['scenes'][index]['scene_direction']}",
            f"Motion: {current['scenes'][index]['motion_direction']}",
            f"Length: {current['durations'][index]} seconds.",
        ]

    def expected_prompt(self, current, index, sentences):
        body = self.head(current, index)
        body.append(DIALOGUE_HEADER.format(n=len(sentences)))
        body.append(DIALOGUE_PACE)
        body += [f'{number}. "{sentence}"' for number, sentence in enumerate(sentences, 1)]
        body.append(ONLY_SPEECH_LINE)
        body.append(AFTER_LAST_LINE)
        body.append(AUDIO_LINE)
        return body

    def test_row_10_two_sentence_prompt_format(self):
        sentences = ["살은 액운이 아니라 타고난 기질의 표시입니다.", "오늘은 진짜 뜻을 알려 드릴게요."]
        current = draft(" ".join(sentences))
        prompt = production().scene_prompt(current, PLANNING, 0)
        self.assertIn("Number of lines: 2.", prompt)
        self.assertNotIn("from 0.5s to 10.0s", prompt)
        self.assertNotIn("until 11.0s", prompt)
        for number, sentence in enumerate(sentences, 1):
            with self.subTest(row=10, sentence=sentence):
                self.assertEqual(prompt.count(f'"{sentence}"'), 1)
                self.assertIn(f'{number}. "{sentence}"', prompt.splitlines())
        self.assertEqual(prompt.splitlines(), self.expected_prompt(current, 0, sentences))

    def test_row_11_old_formats_are_gone(self):
        current = draft("살은 액운이 아니라 타고난 기질의 표시입니다. 오늘은 진짜 뜻을 알려 드릴게요.")
        prompt = production().scene_prompt(current, PLANNING, 0)
        for old in ("<낭독대본>", "낭독 규칙", "The main character says:"):
            with self.subTest(row=11, old=old):
                self.assertNotIn(old, prompt)
        with self.subTest(row=11, old="timed span"):
            self.assertIsNone(OLD_TIMED_SPAN.search(prompt))

    def test_row_12_last_scene_cta_is_a_numbered_line(self):
        cta = "저장해 두세요."
        current = draft("첫 장면입니다.", "마지막 장면입니다.", cta=cta)
        prompt = production().scene_prompt(current, PLANNING, 1)
        lines = prompt.splitlines()
        self.assertIn("Number of lines: 2.", prompt)
        self.assertIn(f'2. "{cta}"', lines)
        numbered = [line.split(". ", 1)[1][1:-1] for line in lines if re.match(r'^\d+\. "', line)]
        self.assertEqual(numbered, ["마지막 장면입니다.", cta])
        self.assertEqual(lines, self.expected_prompt(current, 1, ["마지막 장면입니다.", cta]))

    def test_row_13_blank_script_is_a_silent_scene(self):
        for label, current, index in (
            ("blank middle scene", draft("   ", "다음 장면입니다."), 0),
            ("blank last scene without cta", draft("첫 장면입니다.", " \n "), 1),
        ):
            with self.subTest(row=13, case=label):
                lines = production().scene_prompt(current, PLANNING, index).splitlines()
                self.assertFalse(any(line.startswith("Dialogue:") for line in lines))
                self.assertFalse(any(re.match(r"^\d+\. ", line) for line in lines))
                self.assertEqual(lines[:6], self.head(current, index))
                self.assertEqual(lines[6:], [SILENT_LINE, AUDIO_LINE])


class OldContractRemovedTest(unittest.TestCase):
    def test_row_15_dialogue_lines_is_removed(self):
        self.assertFalse(hasattr(narration_timing, "dialogue_lines"))


class WritingRulesTest(unittest.TestCase):
    def test_row_14_writing_rules_name_the_contract(self):
        rules = narration_timing.NARRATION_WRITING_RULES
        self.assertTrue(rules.strip())
        for phrase in ("한글", "두 문장", "큰따옴표"):
            with self.subTest(row=14, phrase=phrase):
                self.assertIn(phrase, rules)


if __name__ == "__main__":
    unittest.main()
