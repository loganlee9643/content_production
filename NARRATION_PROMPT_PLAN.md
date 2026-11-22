# 낭독 정확도 개선 설계

클립 생성 시 영상 모델이 낭독 대본의 단어를 빠뜨리거나 바꿔 말하는 문제를 줄인다. 장면 길이(11초 등 균등 분할)는 그대로 두고 (1) 대사 분량, (2) 영상 프롬프트 형식, (3) 스토리보드 대본 작성 규칙을 바꾼다. 생성 모델 특성상 0건을 보장하지는 않는다.

## 결정 (한 문장씩)

1. **대사 분량** — 발화 상한을 초당 6단위에서 `SPOKEN_UNITS_PER_SECOND = 4.5`로, 글자 수 목표를 초당 7자에서 `CHARACTERS_PER_SECOND = 5.25`로 낮추고, 11초 장면의 글자 수 범위를 (73, 70, 75)에서 `ELEVEN_SECOND_RANGE = (57, 54, 60)`으로 바꾼다. 백엔드와 화면 계산이 같은 값을 쓴다.
2. **영상 프롬프트 형식** — `scene_prompt`는 영어 지시문과, 문장마다 시간 구간을 붙인 큰따옴표 한국어 대사로 프롬프트를 만든다. 대사 구간은 0.5초에서 시작해 장면 끝 1초 전에 끝나고, 문장별 `narration_units` 비율로 나눈다.
3. **대본 작성 규칙** — 스토리보드 생성·보정·대본 줄이기 프롬프트에 공통 규칙 `NARRATION_WRITING_RULES`를 넣는다. 숫자·영문·기호는 읽는 그대로 한글로 쓰고, 한 문장은 공백 제외 20자 안팎, 장면당 두 문장 이하, 큰따옴표를 쓰지 않는다.

## 계약

### `apps/backend/narration_timing.py`

| 이름 | 계약 |
| --- | --- |
| `SPOKEN_UNITS_PER_SECOND = 4.5` | 발화 단위 상한 계수 |
| `CHARACTERS_PER_SECOND = 5.25` | 11초 외 장면의 글자 수 목표 계수 |
| `ELEVEN_SECOND_RANGE = (57, 54, 60)` | 11초 장면의 (목표, 최소, 최대) 글자 수 |
| `DIALOGUE_START = 0.5`, `DIALOGUE_TAIL = 1.0` | 대사 구간 시작 시각, 끝에 남기는 무음 시간(초) |
| `narration_budget(seconds)` | `max(1, int(seconds * SPOKEN_UNITS_PER_SECOND))` |
| `narration_character_target(seconds)` | 11초면 `ELEVEN_SECOND_RANGE`. 그 밖은 `target = int(seconds * CHARACTERS_PER_SECOND)`, 허용폭 `seconds // 2 + 2` (기존 허용폭 유지) |
| `dialogue_lines(text, seconds)` | 아래 규칙의 `[(start, end, sentence)]` 목록 |
| `NARRATION_WRITING_RULES` | 결정 3의 한국어 규칙 문장 (모델 지시문) |

`dialogue_lines` 규칙:
- 문장 경계: 줄바꿈, 또는 `.!?。！？…` 뒤의 공백. 앞뒤 공백을 지우고 빈 문장은 버린다. 문장부호는 문장에 남긴다.
- 문장 안의 `"`, `“`, `”`는 `'`로 바꾼다 (프롬프트의 큰따옴표 대사가 깨지지 않게).
- 구간: `DIALOGUE_START`부터 `seconds - DIALOGUE_TAIL`까지를 문장별 `narration_units` 비율로 나눈다. 경계는 누적 비율로 계산해 소수 첫째 자리로 반올림하고, 첫 구간 시작은 `DIALOGUE_START`, 마지막 구간 끝은 `seconds - DIALOGUE_TAIL`이다. 앞 구간의 끝이 다음 구간의 시작이다.
- 문장이 없으면 `[]`.

### `apps/backend/production.py`

- `scene_prompt(draft, planning, index)` 반환 문자열 형식(줄 순서 고정):
  ```
  Vertical 9:16 short-form video, one scene.
  Overall direction: {draft.direction}
  Main character and staging: {age} {role}, {tone}, {staging}.
  Scene: {scene_direction}
  Motion: {motion_direction}
  Length: {seconds} seconds.
  Dialogue: the main character speaks Korean. Say each quoted line exactly as written, word for word, including every particle. Do not add, skip, reorder, or replace any word. Speak clearly at a calm, even pace.
  [{start:.1f}-{end:.1f}s] The main character says: "{sentence}"
  ...
  [{last_end:.1f}-{seconds:.1f}s] The main character stops speaking and keeps a natural expression.
  Audio: only the main character's voice. No background music. No other voices.
  ```
- 대사 문장이 없으면 `Dialogue:` 줄과 구간 줄 대신 `[0.0-{seconds:.1f}s] The main character stays silent and keeps a natural expression.` 한 줄을 둔다.
- `submit_clips`의 캐릭터 이미지 앞머리 문장과 이어 생성(두 이미지) 안내 문장도 같은 의미의 영어로 바꾼다.
  - 캐릭터: `Use the person in the attached image as the main character of this scene. Keep the face, hairstyle, age group, and outfit; direct only the scene's motion and composition.`
  - 이어 생성: `The first image is the reference for the character's face, hair, and outfit. The second image is a frame from the previous scene, for continuing pose, lighting, and composition. Do not copy captions or promotional text from the reference images into the new scene. Do not interpolate between the two images.`

### `apps/backend/video_analysis.py`

- 스토리보드 생성 프롬프트, 그 보정(correction) 프롬프트, `shorten_narration` 프롬프트가 `NARRATION_WRITING_RULES`를 포함한다.
- 생성 프롬프트의 "장면마다 짧은 한두 문장으로 작성하세요."와 "숫자는 읽는 데 더 오래 걸립니다."는 규칙 상수와 겹치므로 지운다.
- 11초 범위 실패 메시지는 리터럴 `70~75자` 대신 `narration_character_target(11)`의 최소·최대로 만든다: `f"11초 장면의 대본을 {minimum}~{maximum}자로 맞추지 못했습니다. 다시 생성해 주세요."` (기존 한국어 사용자 문구 형식 유지, 숫자만 상수에서).

### `apps/web/app/projects/[projectId]/analysis/storyboard.tsx`

- `timing()`의 상수를 백엔드와 같게: 11초 `57 / 54 / 60`, 그 밖 `Math.floor(seconds * 5.25)`, 발화 상한 `Math.max(1, Math.floor(seconds * 4.5))`. 계수는 파일 상단 이름 있는 상수로 둔다.

### 표로 고정한 예시 (테스트와 같은 값)

| 입력 | 결과 |
| --- | --- |
| `narration_budget(11)` / `(10)` / `(12)` | `49` / `45` / `54` |
| `narration_character_target(11)` / `(10)` | `(57, 54, 60)` / `(52, 45, 59)` |
| `dialogue_lines("가나다. 라마바사아자차카.", 11)` | 두 구간, 첫 시작 `0.5`, 마지막 끝 `10.0`, 첫 구간 끝 = 0.5 + 9.5 × units(문장1)/units(전체) (소수 첫째 자리 반올림) |

## 미지 정보

- 초당 4.5단위·5.25자는 실측 근거가 아니라 현재 대본(초당 4.8~5.1음절)을 약 20% 줄인 값이다. 효과는 생성 결과로만 확인된다. 계수를 상수로 두어 조정할 수 있게 한다.
- 시간 구간·영어 지시문이 한국어 낭독 정확도를 높인다는 공식 근거는 없다(Gemini Omni 문서는 장면 진행용 구간 표기만 소개한다). 프롬프트 효과는 자동 테스트 대상이 아니다.

## 부분 실패 정책

- 해당 없음(행 단위 순회 실패가 없다). `dialogue_lines`는 문장이 없을 때 빈 목록을 내고, `scene_prompt`는 그 경우 침묵 장면으로 명시한다. 공백뿐인 대사를 "말하는 장면"으로 꾸며내지 않는다.

## 불변식

- `scene_prompt`의 대사 줄을 이어 붙이면, 큰따옴표 치환을 뺀 원래 `spoken_text`의 문장들과 순서·내용이 같다. 단어를 더하거나 빼지 않는다.
- 대사 구간은 겹치지 않고 연속이며, `DIALOGUE_START`에서 시작해 `seconds - DIALOGUE_TAIL`에서 끝난다.
- 화면의 분량 계산과 백엔드의 분량 계산이 같은 상한을 낸다.

## 실패 모드 표

| # | 칸 | 입력 | 기대 |
| --- | --- | --- | --- |
| 1 | 값 | `narration_budget(11/10/12)` | `49/45/54`, 그리고 `int(s * SPOKEN_UNITS_PER_SECOND)`와 같다 |
| 2 | 값 | `narration_character_target(11)`, `(10)` | `ELEVEN_SECOND_RANGE == (57,54,60)`, `(52,45,59)` |
| 3 | 함께 | 기존 저장 대본(56음절, 11초): "사주에 살이 있다는 말에 무섭고 불길하게 느껴지셨나요? 하지만 살은 액운이 아니라 남들보다 뛰어나게 타고난 특별한 기질의 표시입니다." | `timing_issues`가 그 장면을 초과로 보고한다 (기존 저장 스토리보드는 줄이기 전까지 클립 생성이 막힌다) |
| 4 | 값 | 약 42음절 11초 대본 (예: "살은 액운이 아니라 타고난 기질의 표시입니다. 오늘은 도화살과 역마살의 진짜 뜻을 알려 드릴게요.") | `timing_issues` 없음, 글자 수가 54~60 안 |
| 5 | 값 | `dialogue_lines("가나다. 라마바사아자차카.", 11)` | 두 구간, `(0.5, x, "가나다.")`, `(x, 10.0, "라마바사아자차카.")`, `x`는 예시 표의 식 |
| 6 | 값 | 줄바꿈으로 붙은 CTA (`"문장 하나.\n저장해 두세요."`) | 두 문장으로 나뉜다 |
| 7 | 실패 | `dialogue_lines("   ", 11)`, `dialogue_lines("", 11)` | `[]` |
| 8 | 실패 | 문장 안 큰따옴표 (`그는 "괜찮다"고 했다.`, `“좋아”`) | 문장에 `"`·`“`·`”`가 없고 `'`로 바뀐다 |
| 9 | 규모 | 문장 30개(각 한 단어) 11초 | 구간 30개, 시작·끝이 단조 증가하고 연속, 첫 시작 0.5, 마지막 끝 10.0 |
| 10 | 값 | `scene_prompt` (2문장 대본) | 각 문장이 `The main character says: "{문장}"` 형식으로 정확히 한 번, `Length: 11 seconds.`, `[0.5-`, `No background music.` 포함 |
| 11 | 함께 | `scene_prompt` | `<낭독대본>`, `낭독 규칙` 문자열이 없다 (옛 형식 제거) |
| 12 | 함께 | `scene_prompt`, 마지막 장면 + CTA | CTA 문장도 대사 줄로 들어간다 (`spoken_text`와 같은 대사) |
| 13 | 실패 | `scene_prompt`, 대본이 공백뿐 | `says:` 없음, `stays silent` 줄 하나 |
| 14 | 함께 | `NARRATION_WRITING_RULES` | 비어 있지 않고 "한글", "두 문장", "큰따옴표"를 포함한다 |

해당 없음: 없음.

## 기준 선례

| 트리 | 선례 파일 | 근거 |
| --- | --- | --- |
| `apps/backend` | `narration_timing.py`(순수 계산), `production.py`의 `scene_prompt` | 워크스페이스 규칙 파일 없음. 같은 모듈의 기존 구조를 따른다. |
| `apps/web` | `storyboard.tsx`의 `timing()` | 같음. 백엔드와 같은 계산을 화면에서 중복하는 기존 구조를 유지한다(구조 변경은 범위 밖). |
| 테스트 | `apps/backend/tests/test_collection.py` (`unittest`) | 직전 작업에서 정한 러너와 위치. |

## 범위 밖

- 장면 길이 배분(균등 분할 유지), 영상 모델 교체, TTS·립싱크.
- 사용자가 직접 편집 저장한 대본의 숫자·영문 자동 변환이나 거절.
- 기존 저장 스토리보드의 일괄 재작성 (줄이기 버튼으로 장면별 처리).
- 레퍼런스 수집 코드, `main.py`, 화면의 다른 컴포넌트.
- 화면과 백엔드의 분량 계산 중복 제거.

## 단계 (순서 고정, 병렬 금지)

### 1. 테스트 todo (테스트 Agent)
- `apps/backend/tests/test_narration.py`에 실패 모드 표 1~14를 단언한다(`unittest`, `subTest`, 행 번호를 이름이나 라벨에 둔다). 실제 DB·외부 API에 붙지 않는다.
- 1·2·5·9행은 상수로 계산해 비교하고, 예시 표의 값도 같이 단언한다.
- 프로덕션 파일을 만들거나 고치지 않는다. 작성 직후 현재 코드에서 실패해야 하며 기준선을 기록한다.

### 2. 구현 todo (구현 Agent, 테스트가 red인 뒤)
- `narration_timing.py`, `production.py`, `video_analysis.py`, `storyboard.tsx`를 계약대로 바꾼다.
- 테스트 산출물을 추가·수정하지 않는다. 옛 형식(`<낭독대본>`, `70~75자` 리터럴, `seconds * 6`, `seconds * 7`, `73`)을 같은 패스에서 지운다.

### 3. 평가 todo (평가 Agent, 구현 뒤)
- 아래 수락 기준만 grep과 러너 실행으로 대조한다.

## 수락 기준

1. `apps/backend`에서 `.venv/Scripts/python.exe -m unittest discover -s tests`: 테스트 단계 기준선 실패 외 새 실패가 없고, 이 설계가 추가한 테스트가 모두 통과한다(기존 `test_collection.py` 포함).
2. 실패 모드 표 1~14의 각 행이 `test_narration.py`의 테스트 이름 또는 `subTest` 라벨에 대응한다.
3. 주석과 테스트 산출물을 뺀 `apps/backend/production.py`에 `<낭독대본>`, `낭독 규칙`, `첨부 이미지의 인물을`, `첫 이미지는 인물의`가 없다.
4. `scene_prompt`가 `dialogue_lines(`를 부른다.
5. `apps/backend/video_analysis.py`에서 `NARRATION_WRITING_RULES`가 세 번 이상 쓰인다(생성, 보정, 줄이기 프롬프트), `70~75` 리터럴이 없고, "숫자는 읽는 데 더 오래 걸립니다"가 없다.
6. `narration_timing.py`에 `SPOKEN_UNITS_PER_SECOND = 4.5`, `CHARACTERS_PER_SECOND = 5.25`, `ELEVEN_SECOND_RANGE = (57, 54, 60)`가 있고 `seconds * 6`, `seconds * 7`, `73, 70, 75`가 없다.
7. `storyboard.tsx`의 `timing()`에 `57`, `54`, `60`, `5.25`, `4.5`가 있고 `73`, `seconds * 7`, `seconds * 6`이 없다. `apps/web`에서 `npm run typecheck`가 통과한다.
8. 범위 밖 파일(`collection.py`, `discovery.py`, `main.py`)이 이 작업에서 바뀌지 않았다(수정 시각).
9. 테스트 파일 생성 시각이 구현 파일 수정 시각보다 앞선다.
