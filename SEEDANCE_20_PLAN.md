# Seedance 2.0 전환 설계

현재 BytePlus 계정은 `seedance-1-5-pro-251215`에 접근할 수 없습니다(404 `InvalidEndpointOrModel.NotFound`). 그래서 Seedance 경로의 모델을 계정에서 조회되는 `dreamina-seedance-2-0-260128`로 바꿉니다. 2.0을 고른 이유는 세 가지입니다. 첫 프레임 이미지와 `ratio: "9:16"`을 함께 쓸 수 있고, 11초 길이와 `generate_audio`를 지원하며, 720p를 지원하기 때문입니다. 2.5 계열은 첫 프레임을 쓸 때 `ratio`를 `adaptive`로만 받습니다.

## 결정

1. `SEEDANCE_MODEL = "dreamina-seedance-2-0-260128"`로 바꾸고, 표시 이름은 `"Seedance 2.0"`으로 한다.
2. 장면 길이 범위는 순수 함수 `clip_duration_range(model)`로 뺀다. Gemini는 `(3, 15)`, Seedance 2.0은 `(4, 15)`다. 문서 범위는 `[4, 15]`다.
3. Seedance 비용 추정은 `usage.completion_tokens × SEEDANCE_USD_PER_MILLION_TOKENS(7.0) / 1,000,000`으로 한다. 단가는 가격표의 2.0 온라인 480p·720p 값이다. 2.0 가격표에는 음성 유무에 따른 단가 구분이 없고, flex 등급도 지원하지 않는다. 그래서 `generate_audio`와 `service_tier` 분기를 지운다.
4. 요청 본문은 바꾸지 않는다. `ratio "9:16"`, `resolution "720p"`, `duration`, `generate_audio: True`, `first_frame` 이미지를 그대로 보낸다.
5. `DEVELOPMENT.md`가 지원 모델로 소개하는 `seedance-1-5-pro-251215`를 새 ID로 바꾼다.

## 계약

| 이름 | 계약 |
| --- | --- |
| `SEEDANCE_MODEL` | `"dreamina-seedance-2-0-260128"` |
| `MODEL_LABELS[SEEDANCE_MODEL]` | `"Seedance 2.0"` |
| `SEEDANCE_USD_PER_MILLION_TOKENS` | `7.0` |
| `clip_duration_range(model)` | `GEMINI_MODEL` → `(3, 15)`, `SEEDANCE_MODEL` → `(4, 15)` |
| `generation_usage(task)` | `task["model"] == SEEDANCE_MODEL`이고 `usage.completion_tokens`가 int면 `{"totalTokens": n, "estimatedUsd": round(n * 7.0 / 1e6, 8)}`. 그 밖의 Seedance 응답은 `None`. Gemini 분기는 그대로 둔다. |

`submit_clips`는 `minimum, maximum = clip_duration_range(model)`을 쓴다. 범위 밖 오류 문구는 기존 형식(`{MODEL_LABELS[model]}는 장면당 {minimum}~{maximum}초를 지원합니다.`)을 유지한다.

## 미지 정보

- 2.x는 실사 인물 얼굴이 들어간 참조 이미지를 받지 않는다. 이 제한이 첫 프레임 이미지에도 적용되는지와 그때의 오류는 문서에 없다. 거절되면 기존 4xx 처리("…가 영상 생성 요청을 거절했습니다. (HTTP n)")로 보인다.
- 한국어 대사 정확도는 생성 결과로만 확인할 수 있다.

## 부분 실패 정책

해당 없음. 행 단위로 순회하다 실패하는 경로가 없다.

## 실패 모드 표

| # | 칸 | 입력 | 기대 |
| --- | --- | --- | --- |
| 1 | 값 | `SEEDANCE_MODEL`, `MODEL_LABELS` | `"dreamina-seedance-2-0-260128"`, `"Seedance 2.0"`. 옛 ID `seedance-1-5-pro-251215`는 `MODEL_LABELS`에 없다. |
| 2 | 값 | `clip_duration_range(GEMINI_MODEL)`, `clip_duration_range(SEEDANCE_MODEL)` | `(3, 15)`, `(4, 15)`. 11이 두 범위 안에 든다. |
| 3 | 값 | `generation_usage({"model": SEEDANCE_MODEL, "usage": {"completion_tokens": 237600}})` | `{"totalTokens": 237600, "estimatedUsd": 1.6632}`. 상수 식으로 계산한 값과 같다. |
| 4 | 실패 | Seedance 응답에 `usage`가 없음, `usage`가 dict가 아님, `completion_tokens`가 int가 아님 | `None` |
| 5 | 함께 | `generate_audio: False` 또는 `service_tier: "flex"`가 붙은 Seedance 응답 | 단가가 같다(7.0). 옛 분기가 없다. |
| 6 | 함께 | 옛 모델 ID(`seedance-1-5-pro-251215`) 응답 | `None` |
| 7 | 함께 | Gemini 응답 예시(`total_tokens`, 입력·출력 토큰) | 기존 식 결과와 같다(회귀 잠금) |
| 8 | 함께 | `DEVELOPMENT.md` | `dreamina-seedance-2-0-260128`이 있고 `seedance-1-5-pro-251215`가 없다 |

규모 칸은 해당 없음이다. 입력 크기에 비례하는 반복이 없다.

## 범위 밖

- 요청 본문 형식, 상태 조회, 다운로드 허용 도메인, 취소 처리.
- 이어 생성(Gemini 전용 유지)과 화면 코드. 화면은 서버가 보내는 모델 이름을 표시만 한다.
- `config.yaml` 값 변경. 키가 들어 있는 사용자 파일이므로 구현 단계가 손대지 않는다.
- 낭독 프롬프트와 수집 코드.

## 단계 (순서 고정, 병렬 금지)

1. **테스트 (테스트 Agent):**
   - `apps/backend/tests/test_seedance.py`에 실패 모드 표의 행 1~8을 단언한다.
   - 프로덕션 코드는 수정하지 않는다.
   - 작성 직후 테스트가 현재 코드에서 실패해야 한다.
2. **구현 (구현 Agent):**
   - `production.py`와 `DEVELOPMENT.md`만 바꾼다.
   - 테스트 산출물은 수정하지 않는다.
3. **평가 (평가 Agent):** 아래 수락 기준만 대조한다.

## 수락 기준

1. `apps/backend`에서 `.venv/Scripts/python.exe -m unittest discover -s tests`를 실행해 모두 통과한다.
2. 실패 모드 표의 행 1~8이 `test_seedance.py`의 테스트 이름 또는 `subTest` 라벨에 대응한다.
3. 주석을 뺀 `production.py` 코드가 다음을 만족한다.
   - `seedance-1-5-pro-251215`, `rate = 1.2`, `service_tier`가 없다.
   - `(3, 15) if model == GEMINI_MODEL else (4, 12)`가 없다.
   - `submit_clips`가 `clip_duration_range(model)`를 부른다.
4. `production.py`의 Seedance 요청 본문에 `"ratio": "9:16"`, `"resolution": "720p"`, `"generate_audio": True`, `"role": "first_frame"`이 그대로 있다.
5. 이 작업에서 `narration_timing.py`, `video_analysis.py`, `collection.py`, `discovery.py`, `main.py`, `config.yaml`, 화면 파일이 바뀌지 않았다. 수정 시각으로 확인한다.
6. 테스트 파일 생성 시각이 구현 파일 수정 시각보다 앞선다.
