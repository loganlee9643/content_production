# 낭독 프롬프트 보강 설계 (2차)

1차 변경(`NARRATION_PROMPT_PLAN.md`) 뒤 생성한 클립을 받아쓰기한 결과, 모델은 대본 두 문장을 정확히 읽었습니다. 다만 문장별 시간 구간을 무시하고 6초 만에 다 읽은 뒤, 남은 시간을 대본에 없는 문장("낯섦과 변화, 이에 부응하는 에너지입니다.")으로 채웠습니다. 이번 변경은 문장별 시간 구간을 없애는 대신, 따옴표 안 문장만이 유일한 발화라는 것과 마지막 문장 뒤의 침묵을 명시합니다. 대사를 낭독 구간 전체에 천천히 나눠 읽으라는 지시도 넣습니다. 확률적 생성이라 0건을 보장하지는 않습니다.

## 결정 (한 문장)

`scene_prompt`는 문장별 시간 구간 줄 대신, 번호를 붙인 따옴표 대사 목록, 문장 수, 낭독 구간(`DIALOGUE_START` ~ `seconds - DIALOGUE_TAIL`)에 천천히 나눠 읽으라는 지시, "따옴표 문장이 유일한 발화이며 즉흥 대사 금지", "마지막 문장 뒤 끝까지 침묵"을 넣는다. 시각 계산이 필요 없어지므로 `dialogue_lines`는 문장만 돌려주는 `dialogue_sentences`로 바꾼다.

## 계약

### `apps/backend/narration_timing.py`

- `dialogue_sentences(text) -> list[str]`
  - 문장 경계: 줄바꿈, 또는 `.!?。！？…` 뒤의 공백.
  - 앞뒤 공백을 지우고 빈 문장은 버린다.
  - `"`, `“`, `”`는 `'`로 바꾼다.
  - 문장이 없으면 `[]`.
- `dialogue_lines`는 삭제한다. 호출부는 `scene_prompt` 하나뿐이다.
- `DIALOGUE_START = 0.5`, `DIALOGUE_TAIL = 1.0`은 유지한다. 이제 낭독 구간 문구에만 쓰인다.
- 나머지 상수와 함수(`narration_budget`, `narration_character_target`, `NARRATION_WRITING_RULES` 등)는 바꾸지 않는다.

### `apps/backend/production.py` — `scene_prompt(draft, planning, index)`

줄 순서를 고정하고, 줄 사이에 빈 줄을 두지 않는다. 앞 6줄(`Vertical…`부터 `Length: {seconds} seconds.`까지)은 1차와 같다.

대사가 있으면(`n = len(sentences)`, `seconds = durations[index]`) 아래 줄을 잇는다.

```
Dialogue: the main character speaks Korean and says exactly the quoted lines below, in order, word for word, including every particle, and nothing else. Number of lines: {n}.
Speak slowly and clearly, spreading the lines evenly from {DIALOGUE_START:.1f}s to {seconds - DIALOGUE_TAIL:.1f}s.
1. "{sentence 1}"
2. "{sentence 2}"
...
These quoted lines are the only speech in this video. Do not add, skip, reorder, replace, or repeat any word. No ad-libs and no extra sentences.
After the last line, the main character stays silent until {seconds:.1f}s and keeps a natural expression.
Audio: only the main character's voice. No background music. No other voices.
```

대사가 없으면 앞 6줄 뒤에 아래 두 줄을 둔다.

```
The main character stays silent for the whole clip and keeps a natural expression.
Audio: only the main character's voice. No background music. No other voices.
```

`submit_clips`가 앞에 붙이는 캐릭터·이어 생성 문장은 바꾸지 않는다.

### 표로 고정한 예시

| 입력 | 결과 |
| --- | --- |
| `dialogue_sentences("가나다. 라마바사아자차카.")` | `["가나다.", "라마바사아자차카."]` |
| 11초, 2문장 | `Number of lines: 2.`, `from 0.5s to 10.0s`, `stays silent until 11.0s` |

## 미지 정보

- 이 지시가 즉흥 대사와 배경음악을 실제로 줄이는지는 생성 결과로만 확인할 수 있다. 자동 테스트는 프롬프트 형식만 검증한다.

## 부분 실패 정책

해당 없음. 행 단위로 순회하다 실패하는 경로가 없다.

## 불변식

- 번호를 붙인 대사 줄의 따옴표 안 문장을 차례로 모으면, `spoken_text`를 문장으로 나눈 결과(큰따옴표 치환 후)와 같다.
- 프롬프트에 문장별 시간 구간 표기(`[x.x-y.ys]`)가 없다.

## 실패 모드 표

| # | 칸 | 입력 | 기대 |
| --- | --- | --- | --- |
| 1~4, 14 | — | 1차 행 그대로 | 1차와 같은 단언. 분량 상수와 작성 규칙은 바뀌지 않는다. |
| 5 | 값 | `dialogue_sentences("가나다. 라마바사아자차카.")` | `["가나다.", "라마바사아자차카."]` |
| 6 | 값 | `"문장 하나.\n저장해 두세요."` | 두 문장 |
| 7 | 실패 | `""`, `"   "` | `[]` |
| 8 | 실패 | 문장 안 큰따옴표(`"`, `“`, `”`) | `'`로 바뀐다 |
| 9 | 규모 | 문장 30개 | 30개가 순서대로 나온다 |
| 10 | 값 | `scene_prompt`, 11초 2문장 | 줄 목록이 계약 템플릿 전체와 같다(번호 줄, `Number of lines: 2.`, `from 0.5s to 10.0s`, `until 11.0s`) |
| 11 | 함께 | `scene_prompt` | `<낭독대본>`, `낭독 규칙`, `The main character says:`, 정규식 `\[\d+\.\d-\d+\.\ds\]`이 없다 |
| 12 | 함께 | 마지막 장면 + CTA | CTA 문장도 번호 붙은 대사 줄이고, `Number of lines`에 포함된다 |
| 13 | 실패 | 대본이 공백뿐 | `Dialogue:` 줄과 번호 줄이 없고 `stays silent for the whole clip` 줄과 Audio 줄만 있다 |
| 15 | 함께 | `narration_timing` 모듈 | `dialogue_lines` 속성이 없다(옛 계약 제거) |

해당 없음: 없음.

## 범위 밖

- 분량 상수, 스토리보드 생성·줄이기 프롬프트, 화면 코드.
- `submit_clips`의 캐릭터·이어 생성 문장.
- 생성 후 자동 받아쓰기 검사(별도 기능).
- 수집 코드와 `main.py`.

## 단계 (순서 고정, 병렬 금지)

1. **테스트 (테스트 Agent):**
   - `apps/backend/tests/test_narration.py`의 5~13행을 이 표로 고친다. 15행을 추가하고, 1~4행과 14행은 유지한다.
   - 프로덕션 코드는 수정하지 않는다.
   - 고친 직후 테스트가 현재 코드에서 실패해야 하며, 기준선을 기록한다.
2. **구현 (구현 Agent):**
   - `narration_timing.py`와 `production.py`를 계약대로 바꾼다.
   - 테스트 산출물은 수정하지 않는다.
3. **평가 (평가 Agent):** 아래 수락 기준만 대조한다.

## 수락 기준

1. `apps/backend`에서 `.venv/Scripts/python.exe -m unittest discover -s tests`를 실행해 모두 통과한다. 테스트 단계가 기록한 기준선 실패 외에 새 실패가 없다.
2. 실패 모드 표의 행 1~15가 `test_narration.py`의 테스트 이름 또는 `subTest` 라벨에 대응한다.
3. 주석을 뺀 `apps/backend` 프로덕션 코드에 `dialogue_lines`가 없다. 대상은 `narration_timing.py`와 `production.py`다.
4. `scene_prompt`가 `dialogue_sentences(`를 부른다. `production.py`에 `The main character says:`가 없다.
5. `narration_timing.py`의 `SPOKEN_UNITS_PER_SECOND`, `CHARACTERS_PER_SECOND`, `ELEVEN_SECOND_RANGE`, `NARRATION_WRITING_RULES` 값이 1차와 같다.
6. 이 작업에서 `video_analysis.py`, `storyboard.tsx`, `collection.py`, `discovery.py`, `main.py`가 바뀌지 않았다. 수정 시각으로 확인한다.
7. 테스트 파일 수정 시각이 구현 파일 수정 시각보다 앞선다.
