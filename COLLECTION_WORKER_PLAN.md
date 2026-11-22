# 수집 워커 개선 설계

레퍼런스 릴스 자동 수집이 "돌지 않아도 아무도 모르는" 상태를 없애고, 수집 경로를 워커 하나로 모으며, 수집 범위·반응 데이터·토큰 장애 표시를 보강한다.

## 결정 (한 문장씩)

1. **하트비트** — 워커는 루프마다 `collection_gate.worker_seen_at`을 갱신하고, API는 마지막 갱신이 `WORKER_STALE_AFTER`보다 오래됐으면 주제 상태를 `stopped`로 낸다.
2. **수동 수집 일원화** — `POST .../collection-requests`는 요청 행만 넣고 실행은 워커가 한다. 대기 중인 수동 요청이 있으면 호출 간격(`next_call_at`)을 기다리지 않되, Meta 제한(`limited`) 중이면 기다린다.
3. **recent_media 페이지 수집** — 최신 수집도 인기 수집처럼 최대 `MAX_MEDIA_PAGES`(3)페이지를 받고, 한 페이지의 게시물이 모두 그 태그의 직전 성공 시각보다 오래됐으면 멈춘다.
4. **해시태그 ID 저장** — `collection_tags.hashtag_id`에 Meta 해시태그 ID를 저장해 다음 실행부터 `ig_hashtag_search`를 건너뛴다. 저장된 ID로 실행한 수집이 429 이외 오류로 실패하면 ID를 지운다.
5. **인기 게시물 하루 1회** — `collection_tags.top_due_at`이 지난 태그는 `top_media`로 수집하고 `top_due_at = now + TOP_INTERVAL(24h)`로 미룬다. 호출 간격은 하루 전체 실행 수(최신 + 인기)로 다시 나눈다.
6. **토큰 만료 표시** — Meta code 190은 HTTP 401로 올리고, 워커는 401을 `last_error = 'token_expired'`로 저장하며, 주제 상태는 `token_expired`가 된다.
7. **토큰 로그 차단** — 워커 진입점의 `configure_logging()`이 `httpx` 로거를 WARNING으로 올려 요청 URL(access_token 포함)이 로그에 남지 않게 한다.

## 계약

### 백엔드 순수 함수 (`apps/backend/collection.py`)

| 함수 | 입력 | 반환 |
| --- | --- | --- |
| `worker_alive(seen_at, now)` | `datetime \| None`, `datetime` | `seen_at`이 있고 `now - seen_at <= WORKER_STALE_AFTER`이면 `True` |
| `topic_status(states, gate, alive, now)` | 태그 행 목록(`tag`, `last_success`, `last_error`), gate 행, 워커 생존 여부, 현재 시각 | 우선순위 `stopped` > `running` > `token_expired` > `delayed` > `ready` > `partial` > `empty` |
| `call_slot(tag_count, interval_hours)` | 태그 수, 최신 수집 간격(시간) | `timedelta(hours=24) / (tag_count * (24 / interval_hours + 1))` |
| `may_call(gate, has_manual, now)` | gate 행, 대기 중 수동 요청 유무, 현재 시각 | `next_call_at <= now`면 `True`. 아니면 `has_manual and not limited` |
| `pick_job(rows, now)` | 태그별 행(`tag`, `due_at`, `top_due_at`, `requested_at`, `request_id`, `window_hours`) | 아래 선택 규칙의 `{"tag", "mode", "request_id", "hours"}` 또는 `None` |
| `error_label(status_code)` | HTTP 상태 코드 | `401` → `"token_expired"`, 그 밖 → `f"HTTP {status_code}"` |
| `configure_logging()` | 없음 | root INFO, `httpx` 로거 WARNING |

`pick_job` 선택 규칙:
- 대기 중 수동 요청이 있는 태그가 먼저다(`requested_at` 오름차순, 같으면 `tag`). `mode`는 `window_hours > 24`면 `"top"`, 아니면 `"recent"`.
- 수동 요청이 없으면 `due_at`·`top_due_at` 중 `now` 이전인 것 가운데 가장 이른 시각의 태그·모드를 고른다. 같은 시각이면 `recent`, 그다음 `tag` 순이다. 자동 실행은 `request_id = None`, `hours = 24`.
- 지난 시각이 하나도 없으면 `None`.

상수(코드 정의가 기준, 문서·테스트는 상수로 계산한다):
- `WORKER_SLEEP = 10`초 (기존 `run_worker`의 `asyncio.sleep(10)`)
- `MAX_MEDIA_PAGES = 3` (`discovery.py`, 기존 `top_media_pages`의 `range(1, 4)`)
- `WORKER_STALE_AFTER = 2 × (WORKER_SLEEP + LOOKUP_TIMEOUT + MAX_MEDIA_PAGES × MEDIA_TIMEOUT)`초 = 2 × (10 + 8 + 3 × 30) = 216초
- `TOP_INTERVAL = timedelta(hours=24)`

### 수집 함수 (`apps/backend/discovery.py`)

- `discover(q, order="recent", *, since=None, hashtag_ids=None)`
  - `hashtag_ids: dict[str, str]`에 태그가 있으면 `ig_hashtag_search`를 부르지 않고 그 ID를 쓴다.
  - 반환에 `"hashtagIds": {tag: id}`를 더한다(조회했거나 받은 ID).
  - `recent`와 `top` 모두 공통 페이지 함수 `media_pages(client, path, user, token, since)`로 최대 `MAX_MEDIA_PAGES`페이지를 받는다. `recent`는 `timeout=MEDIA_TIMEOUT`, `since`가 있으면 한 페이지의 모든 게시물 `timestamp`가 `since`보다 이르면 다음 페이지를 받지 않는다.
  - Meta error code 190 → `HTTPException(401, 기존 문구 "Meta 액세스 토큰이 유효하지 않거나 만료됐습니다.")`. 다른 코드의 상태 코드는 그대로다.

### API (`GET /api/reference-topics`)

- 응답 필드 이름과 기존 필드는 그대로다. `status` 값에 `"stopped"`, `"token_expired"`가 더해진다.
- `POST /api/reference-topics/{topic}/collection-requests` 응답 형태(`id`, `status`, `newCount`, `completedAt`, `hours`)와 422·404·409 규칙은 그대로다. `BackgroundTasks` 인자와 `run_manual_collection`은 사라진다.

### 화면 (`apps/web/app/references/discovery.tsx`)

- `TopicState.status`에 `"stopped" | "token_expired"`를 더한다.
- `automaticLabel`: `stopped` → `"수집 워커 중지됨"`, `token_expired` → `"Meta 토큰 만료"`, 그 밖은 기존 문구.

### 표로 고정한 예시 (테스트와 같은 값)

| 입력 | 결과 |
| --- | --- |
| `call_slot(14, 6)` | `timedelta(hours=24) / 70` (약 20분 34초) |
| `worker_alive(now - 30s, now)` | `True` |
| `worker_alive(None, now)` | `False` |
| `error_label(401)` / `error_label(502)` | `"token_expired"` / `"HTTP 502"` |

## 미지 정보

1. **테스트 러너** — 워크스페이스에 `.cursor/rules/stack.mdc`도 `.cursor/stacks/`도 없다. 백엔드 venv에 pytest가 없다. 저장소의 기존 백엔드 테스트(`backend/tests/test_album_backend.py`)는 `unittest`다. 제안: `apps/backend/tests/test_collection.py`, 러너 `apps/backend/.venv/Scripts/python.exe -m unittest discover -s tests` (`apps/backend`에서 실행, 설치 불필요). 표 칸은 `subTest`로 나눈다. **Build 전에 확인 필요.**
2. **DB 접착 코드** — Python 프로필 규칙은 단위 테스트가 실제 DB에 붙지 않게 한다. 그래서 결정은 위 순수 함수로 빼서 테스트하고, SQL 배선(컬럼 추가, 하트비트 UPDATE, ID 저장·삭제)은 수락 기준의 grep으로 닫는다. 실제 DB 동작은 자동 테스트로 덮이지 않는다.
3. **저장된 해시태그 ID가 무효가 되는 신호** — Meta가 무효 ID에 어떤 코드를 주는지 문서로 확인하지 못했다. 그래서 "저장된 ID로 실행한 수집이 429 이외 오류로 실패하면 ID를 지운다"로 넓게 잡는다. 다음 실행에서 조회 1회가 더 드는 비용뿐이다.

## 부분 실패 정책

- (b) **한 태그 실패는 그 태그만 잃는다.** 그 태그에 `last_error`를 남기고 `due_at`을 미루며, 워커는 다음 태그를 계속 처리한다(기존과 같다).
- (b) **2페이지 이후 실패는 그 페이지만 잃는다.** 앞 페이지에서 받은 항목은 반환한다(기존 `top_media_pages` 규칙을 recent에도 적용).
- **1페이지 실패와 해시태그 조회 실패는 그 태그 실행 전체의 실패다.** `HTTPException`을 그대로 올린다.

## 불변식

- API 프로세스는 Meta를 호출하지 않는다. Meta 호출은 워커 프로세스에서만 일어난다.
- 워커가 `WORKER_STALE_AFTER` 넘게 하트비트를 남기지 않으면, `running_tag`가 남아 있어도 주제 상태는 `running`이 아니라 `stopped`다.
- 하루 동안 예정된 실행 수 × `call_slot` = 24시간. 인기 수집을 더해도 최신 수집이 밀리지 않는다.
- 한 번의 수집 호출이 보내는 미디어 요청은 `MAX_MEDIA_PAGES`를 넘지 않는다.
- 워커 로그에 `access_token` 값이 남지 않는다.

## 실패 모드 표

| # | 칸 | 입력 | 기대 |
| --- | --- | --- | --- |
| 1 | 값 | `worker_alive(now - 30s, now)` | `True` |
| 2 | 실패 | `worker_alive(None, now)` | `False` (워커를 한 번도 띄우지 않음) |
| 3 | 실패 | `worker_alive(now - WORKER_STALE_AFTER - 1s, now)` | `False` |
| 4 | 함께 | `topic_status`: `running_tag`가 이 주제 태그, `alive=False` | `"stopped"` |
| 5 | 함께 | `topic_status`: 한 태그 `last_error="token_expired"`, `alive=True` | `"token_expired"` |
| 6 | 함께 | `topic_status`: `alive=False` + `token_expired` 행 | `"stopped"` |
| 7 | 함께 | `topic_status`: `alive=True`, `limited=True`, `next_call_at > now` | `"delayed"` (기존) |
| 8 | 함께 | `topic_status`: `alive=True`, 오류 없음, 모든 태그 성공 / 일부 / 없음 | `"ready"` / `"partial"` / `"empty"` (기존) |
| 9 | 값 | `may_call`: `next_call_at > now`, `limited=False`, `has_manual=True` | `True` |
| 10 | 실패 | `may_call`: `next_call_at > now`, `limited=True`, `has_manual=True` | `False` |
| 11 | 실패 | `may_call`: `next_call_at > now`, `has_manual=False` | `False` |
| 12 | 값 | `pick_job`: 태그 A `due_at` 지남, 태그 B에 수동 요청(`window_hours=24`) | B, `"recent"`, 요청 ID |
| 13 | 값 | `pick_job`: 수동 요청 `window_hours=168` | `"top"`, `hours=168` |
| 14 | 값 | `pick_job`: `top_due_at`만 지남 | 그 태그, `"top"`, `request_id=None` |
| 15 | 함께 | `pick_job`: 태그 A `due_at`이 B `top_due_at`보다 이르게 지남 | A, `"recent"` |
| 16 | 실패 | `pick_job`: 지난 시각 없음, 수동 요청 없음 | `None` |
| 17 | 규모 | `call_slot(14, 6)`, 그리고 임의 `(n, h)`에서 `call_slot(n, h) * n * (24/h + 1) == 24h` | `24h / 70`, 등식 성립 |
| 18 | 규모 | `WORKER_STALE_AFTER`를 `WORKER_SLEEP`, `LOOKUP_TIMEOUT`, `MAX_MEDIA_PAGES`, `MEDIA_TIMEOUT`로 계산한 값과 비교 | 같다 (리터럴 216 고정 금지) |
| 19 | 값 | `discover(order="recent")`: 1페이지(`next` 있음) + 2페이지(`next` 없음), httpx MockTransport | 두 페이지의 릴스(`media_type="VIDEO"`, `/reel/` 링크)만 반환 |
| 20 | 실패 | `discover(order="recent")`: 1페이지 HTTP 500 | `HTTPException(502)` |
| 21 | 실패 | `discover(order="recent")`: 1페이지 성공, 2페이지 HTTP 500 | 1페이지 항목 반환 |
| 22 | 함께 | `discover(order="recent", since=T)`: 1페이지 항목이 모두 `T` 이전, `next` 있음 | 미디어 요청 1회로 끝남 |
| 23 | 규모 | `discover`: 모든 페이지에 새 `next` 커서 | 미디어 요청 수 == `MAX_MEDIA_PAGES` |
| 24 | 값 | `discover(hashtag_ids={"사주": "1"})` | `ig_hashtag_search` 요청 0회, `hashtagIds == {"사주": "1"}` |
| 25 | 값 | `discover(hashtag_ids=None)`, 조회 결과 ID `"9"` | `hashtagIds == {"사주": "9"}` |
| 26 | 실패 | `discover`: 해시태그 조회 응답 Meta error code 190 | `HTTPException(401)` |
| 27 | 함께 | `discover`: Meta error code 100 | 기존대로 `HTTPException(502)` (401로 바뀌지 않음) |
| 28 | 값 | `error_label(401)` / `error_label(502)` | `"token_expired"` / `"HTTP 502"` |
| 29 | 실패 | `configure_logging()` 뒤 MockTransport로 `access_token=SECRET` URL 요청 | `httpx` 로거에 INFO 기록 없음, 기록된 메시지에 `SECRET` 없음 |
| 30 | 함께 | `configure_logging()` 뒤 `collection` 로거 | INFO 활성 (수집 결과 로그는 남는다) |

해당 없음: 없음. 네 칸 모두 위 행으로 닫는다.

## 기준 선례

| 트리 | 선례 파일 | 근거 |
| --- | --- | --- |
| `apps/backend` | `apps/backend/collection.py`, `apps/backend/discovery.py` | 워크스페이스 규칙 파일 없음(`.cursor/` 없음). 같은 모듈의 기존 구조를 따른다. |
| `apps/web` | `apps/web/app/references/discovery.tsx` | 같음. 상태 문구는 기존 `automaticLabel`에서 분기한다. |
| 테스트 | `backend/tests/test_album_backend.py` (`unittest`) | **선례와 다름:** 다른 트리(`backend/`)의 선례라 `apps/backend/tests/`를 새로 만든다. 미지 정보 1과 함께 확인 필요. |

## 범위 밖

- `apps/backend/main.py`의 프로젝트·레퍼런스 API, `apps/web/app/references/saved/`, 프로젝트 분석 화면.
- 수동 수집 기간 옵션(6·12·24·168·720시간)과 그 검증.
- `X-App-Usage` 80% 임계값, 429 지연 규칙(`RATE_DELAY`, 1시간 상향).
- `discovery.py`의 `router`(`/api/references/instagram/discover`)는 `main.py`에 등록되어 있지 않은 죽은 경로다. 지우지 않고 보고만 한다.
- 해시태그 조회 결과가 0건일 때 빈 결과를 내는 기존 동작.
- 토큰 자동 갱신.

## 단계 (순서 고정, 병렬 금지)

한 대화에서 테스트+구현이나 구현+평가를 같이 닫지 않는다. Build는 `run-agent-plan`이 테스트 → 구현 → 평가 순서로 돌린다.

### 1. 테스트 todo (테스트 Agent)

- `apps/backend/tests/test_collection.py`에 실패 모드 표 1~30을 단언한다. 표 칸은 `subTest`로 나눈다.
- Meta는 `httpx.MockTransport`로 대체하고, `discovery.CONFIG_PATH`는 임시 YAML로 바꾼다. 실제 DB와 Meta에 붙지 않는다.
- 18행과 17행은 상수로 계산해 비교하고 리터럴 216을 고정하지 않는다.
- 문서 테스트: `DEVELOPMENT.md`에 새 문장("수동 수집도 워커가 실행한다", "수집 워커 중지됨", "인기 게시물을 하루 한 번")이 있고, 옛 문장 "최대 1분 동안 실행권을 기다린다"가 없는지 단언한다.
- 프로덕션 파일을 만들거나 고치지 않는다. 작성 직후 현재 코드에서 실패해야 하며, 기준선(실패 목록)을 기록한다.

### 2. 구현 todo (구현 Agent, 테스트가 red인 뒤)

- `apps/backend/collection.py`
  - 스키마: `collection_gate.worker_seen_at timestamptz`, `collection_tags.hashtag_id text`, `collection_tags.top_due_at timestamptz NOT NULL DEFAULT now()`를 `ALTER ... ADD COLUMN IF NOT EXISTS`로 추가.
  - 계약의 순수 함수와 상수를 추가하고, `collection_topics`·`collect_one`·`run_worker`가 그 함수를 쓰게 바꾼다.
  - `collect_one`: `request_id` 인자를 없애고 `pick_job`으로 태그·모드를 고른다. 슬롯은 `call_slot`. 저장된 `hashtag_id`를 `discover`에 넘기고 반환 ID를 저장한다. 429 이외 실패면 `error_label`로 `last_error`를 저장하고 `hashtag_id`를 지운다. 인기 자동 수집 성공 시 `top_due_at = now + TOP_INTERVAL`. 최신 수집은 `since = last_success`를 넘긴다.
  - `run_worker`: 루프마다 `worker_seen_at = now()` 갱신 후 수집, `WORKER_SLEEP`초 대기.
  - `enqueue_collection`: `BackgroundTasks` 제거. `run_manual_collection` 삭제.
  - `__main__`: `configure_logging()` 호출.
- `apps/backend/discovery.py`: `media_pages`로 recent·top 공통화, `MAX_MEDIA_PAGES`, `since`, `hashtag_ids`, `hashtagIds` 반환, code 190 → 401.
- `apps/web/app/references/discovery.tsx`: `status` 유니온과 `automaticLabel` 문구.
- `DEVELOPMENT.md`: 수동 수집도 워커가 실행한다는 문장, 워커가 멈추면 화면에 "수집 워커 중지됨"이 보인다는 문장, 인기 게시물을 하루 한 번 수집한다는 문장, 최신 수집 페이지 상한. 옛 문장 "다른 수집이 진행 중이면 최대 1분 동안 실행권을 기다린다." 삭제.
- 테스트 산출물을 추가·수정하지 않는다. 한 분기의 실패 정책을 바꾸면 같은 함수의 형제 분기·문서를 같은 패스에서 맞춘다.

### 3. 평가 todo (평가 Agent, 구현 뒤)

아래 수락 기준만 grep과 러너 실행으로 대조한다. 코드·테스트를 쓰지 않는다.

## 수락 기준

1. 러너(`apps/backend`에서 `.venv/Scripts/python.exe -m unittest discover -s tests`): 테스트 단계가 기록한 기준선 실패 외에 새 실패가 없고, 이 설계가 추가한 테스트가 모두 통과한다.
2. 실패 모드 표 1~30의 각 행이 `test_collection.py`의 테스트 이름 또는 `subTest` 라벨과 대응한다.
3. 주석과 테스트 산출물을 뺀 `apps/backend/collection.py`에 `BackgroundTasks`, `run_manual_collection`, `request_id=request_id`가 없다.
4. 주석과 테스트 산출물을 뺀 `apps/backend/collection.py`에 `worker_seen_at`, `hashtag_id`, `top_due_at` 컬럼 추가문이 `ADD COLUMN IF NOT EXISTS`로 있다.
5. `run_worker`가 루프 안에서 `worker_seen_at`을 갱신하고 `asyncio.sleep(WORKER_SLEEP)`을 부른다.
6. `collect_one`이 `call_slot(`, `pick_job(`, `may_call(`, `error_label(`을 부르고, `discover(`에 `hashtag_ids=`와 `since=`를 넘긴다.
7. `collection_topics`가 `topic_status(`와 `worker_alive(`를 부른다.
8. `discovery.py`에 `MAX_MEDIA_PAGES = 3`이 있고, 미디어 요청 반복이 이 상수를 쓴다. `range(1, 4)` 리터럴이 없다.
9. `__main__` 블록이 `configure_logging()`을 부르고, `configure_logging` 안에 `getLogger("httpx").setLevel(logging.WARNING)`이 있다.
10. `discovery.tsx`의 `TopicState.status`에 `"stopped"`와 `"token_expired"`가 있고 `automaticLabel`에 `"수집 워커 중지됨"`, `"Meta 토큰 만료"`가 있다. `apps/web`에서 `npm run typecheck`가 통과한다.
11. `DEVELOPMENT.md`에 "최대 1분 동안 실행권을 기다린다"가 없고, 문서 테스트가 단언한 새 문장이 있다.
12. 범위 밖 파일(`apps/backend/main.py`, `apps/web/app/references/saved/`, `apps/web/app/projects/`)에 변경이 없다(`git diff --stat`).
13. 테스트 단계 기록(기준선 실패)이 구현 변경보다 앞선다.
