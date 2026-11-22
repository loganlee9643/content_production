import re

# Spoken units allowed per second of scene; leaves room for the video model to
# read every word at a calm pace.
SPOKEN_UNITS_PER_SECOND = 4.5
# Character target per second for scene lengths other than 11 seconds.
CHARACTERS_PER_SECOND = 5.25
# (target, minimum, maximum) characters for an 11-second scene.
ELEVEN_SECOND_RANGE = (57, 54, 60)

NARRATION_WRITING_RULES = (
    "대본 작성 규칙: 숫자·영문·기호는 소리 내어 읽는 그대로 한글로 쓰세요 "
    "(예: 3월 → 삼월, 10% → 십 퍼센트). "
    "한 문장은 공백을 빼고 20자 안팎으로 쓰고, 장면마다 두 문장 이하로 작성하세요. "
    "큰따옴표는 쓰지 마세요."
)

SENTENCE_BREAK = re.compile(r"\n|(?<=[.!?。！？…])\s+")


def narration_budget(seconds):
    return max(1, int(seconds * SPOKEN_UNITS_PER_SECOND))


def narration_character_target(seconds):
    if seconds == 11:
        return ELEVEN_SECOND_RANGE
    target = int(seconds * CHARACTERS_PER_SECOND)
    return target, target - seconds // 2 - 2, target + seconds // 2 + 2


def narration_characters(text):
    return len(re.sub(r"\s+", " ", text.strip()))


def narration_units(text):
    units = sum(character.isalnum() for character in text)
    units += sum(max(2, len(number)) for number in re.findall(r"[0-9]+", text))
    units += len(re.findall(r"[.!?。！？,，\n]+", text.strip()))
    return units


def spoken_text(draft, index):
    text = draft["scenes"][index]["narration"].strip()
    if index == len(draft["scenes"]) - 1:
        cta = draft["cta"].strip()
        if cta and cta not in text:
            text = text + "\n" + cta
    return text


def dialogue_sentences(text):
    """Split text into stripped, non-empty sentences in order.

    Double quotes inside sentences become single quotes. Blank text returns [].
    """
    return [
        re.sub(r"[\"“”]", "'", part.strip())
        for part in SENTENCE_BREAK.split(text)
        if part.strip()
    ]


def timing_issues(draft, durations, indexes=None):
    issues = []
    for index in range(len(durations)) if indexes is None else indexes:
        units = narration_units(spoken_text(draft, index))
        limit = narration_budget(durations[index])
        if units > limit:
            issues.append((index, units, limit))
    return issues
