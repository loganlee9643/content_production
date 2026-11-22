# 클립 생성 요청 로그 설계

클립을 만들 때 영상 모델에 보내는 지시문 전체와 파라미터를 API 서버 콘솔 로그에 남긴다. 공급사가 요청을 거절하면 응답 본문의 오류 코드와 메시지도 남긴다. 이번처럼 400의 원인을 추측할 일이 없게 하기 위해서다.

## 결정

1. `submit_clips`는 공급사에 요청을 보내기 직전에 INFO 로그 한 줄을 남긴다. 로그에는 프로젝트, 리비전, 장면 번호, 모델, 엔드포인트, `redacted_payload(payload)`의 JSON(`ensure_ascii=False`, `indent=2`)이 들어간다.
2. `redacted_payload(payload)`는 원본을 바꾸지 않은 복사본을 돌려준다. 바꾸는 값은 두 가지뿐이다.
   - `data:`로 시작하는 문자열은 `<data:{mime}, {n} base64 chars>`로 바꾼다.
   - `mime_type` 키가 함께 있는 dict의 `data` 문자열은 `<{mime_type}, {n} base64 chars>`로 바꾼다.
   - 그 밖의 값(지시문, `https://`·`asset://` URL, 숫자, 불리언)은 그대로 둔다.
3. 응답이 성공하면 INFO 로그에 작업 ID를 남긴다. 4xx 응답이면 WARNING 로그에 HTTP 상태와 응답 본문 앞 1,000자를 남긴다. 요청 헤더(API 키)는 어떤 로그에도 넣지 않는다.
4. 로거는 `discovery.py`와 같은 `logging.getLogger("uvicorn.error")`를 쓴다. 그래야 uvicorn 콘솔에 INFO가 보인다.
5. 화면에 보이는 오류 문구와 DB에 저장하는 값은 바꾸지 않는다.

## 실패 모드 표

| # | 칸 | 입력 | 기대 |
| --- | --- | --- | --- |
| 1 | 값 | Seedance 본문. 텍스트 하나와 `image_url.url = "data:image/png;base64,AAAA"`를 담는다. | URL이 `<data:image/png, 4 base64 chars>`가 된다. 텍스트와 `ratio`, `duration` 등은 그대로다. |
| 2 | 값 | Gemini 본문. `input`은 `[{"type":"image","data":"BBBBBB","mime_type":"image/jpeg"}, {"type":"text","text":"..."}]`이다. | `data`가 `<image/jpeg, 6 base64 chars>`가 된다. `mime_type`과 텍스트 항목은 그대로다. |
| 3 | 함께 | 1번과 2번의 입력 | 호출 뒤에도 원본 dict와 list가 바뀌지 않는다. 깊은 비교로 확인한다. |
| 4 | 함께 | `https://…` URL, `asset://asset-1` URL, 문자열 `input`(Gemini, 이미지 없음) | 그대로 유지된다. |
| 5 | 실패 | `mime_type` 없이 `data` 키만 있는 dict, 예 `{"data": "x"}` | 그대로 유지된다. 이미지 데이터 규칙에 해당하지 않는다. |

규모 칸은 해당 없음이다. 장면 하나당 본문 하나를 한 번 순회하며, 반복 상한이 따로 필요 없다.

## 범위 밖

- 화면 오류 문구, DB에 저장하는 오류 값, 요청 본문 자체.
- 상태 조회(reconcile)와 다운로드의 로그.
- 다른 모듈의 로그.

## 단계 (순서 고정)

1. **테스트:** `apps/backend/tests/test_clip_log.py`에 실패 모드 표의 행 1~5를 단언한다. 프로덕션 코드는 수정하지 않는다. 새로 쓴 테스트는 현재 코드에서 실패해야 한다.
2. **구현:** `production.py`만 바꾼다. 테스트 산출물은 수정하지 않는다.
3. **평가:** 아래 수락 기준만 대조한다.

## 수락 기준

1. `apps/backend`에서 `.venv/Scripts/python.exe -m unittest discover -s tests`를 실행해 모두 통과한다.
2. 실패 모드 표의 행 1~5가 `test_clip_log.py`의 테스트 이름 또는 `subTest` 라벨에 대응한다.
3. `production.py`에 `logger = logging.getLogger("uvicorn.error")`가 있다.
4. `submit_clips`는 공급사에 POST하기 전에 `redacted_payload(payload)`를 넣은 `logger.info`를 호출한다. 4xx 분기에서는 `logger.warning`으로 `response.text[:1000]`를 남긴다.
5. 주석을 뺀 `production.py`의 로그 호출 인자에 `headers`, `key`, `Authorization`, `image_data`가 없다.
6. 화면 오류 문구 `가 영상 생성 요청을 거절했습니다. (HTTP {response.status_code})`가 그대로 있다.
7. 테스트 파일 생성 시각이 `production.py` 수정 시각보다 앞선다.
