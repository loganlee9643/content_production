# 앨범 제작 서비스 Frontend 설계

## 1. 문서 목적

이 문서는 루트의 `backend` 통합 Backend를 사용하는 React Frontend의 화면 구조, 사용자 흐름, 상태 관리, API 연동, 컴포넌트 및 구현 원칙을 정의한다.

Frontend는 다음 제작 과정을 하나의 앨범 프로젝트 안에서 연결한다.

1. 음악 스타일을 설정하고 Gemini로 앨범과 수록곡을 기획한다.
2. 생성된 가사와 영문 스타일을 편집한 뒤 Suno로 음원을 만든다.
3. Gemini 이미지와 선택한 음원을 사용해 플레이 루프 영상을 만든다.
4. 완성된 음원, 가사, 이미지, 영상을 확인하고 다운로드한다.

Backend Base URL:

```text
http://127.0.0.1:8000/api/v1
```

개발 환경은 Backend와 Frontend 두 프로세스로 실행한다.

```text
Process 1: backend 통합 FastAPI Backend
Process 2: React Frontend
```

## 2. 목표 범위

### 2.1 MVP 화면

| 화면 | 주요 기능 |
|---|---|
| 앨범 목록 | 앨범 조회, 검색, 신규 앨범 생성, 최근 작업 재진입 |
| 앨범 만들기 | 음악 스타일 입력, 앨범 생성, Gemini 기획 실행, 가사와 스타일 검토 |
| 노래 만들기 | 트랙별 가사와 스타일 편집, Suno 생성, 후보 재생 및 최종 선택 |
| 플레이 루프 영상 만들기 | 이미지 생성/업로드, 이미지 꾸미기, 음원 선택, 영상 렌더링 |
| 결과 및 내보내기 | 에셋 다운로드, 앨범 ZIP 생성 및 다운로드 |

### 2.2 MVP 제외 범위

1. 회원가입과 다중 사용자 권한
2. 실시간 협업 편집
3. SSE/WebSocket 기반 실시간 갱신
4. 모바일 전용 편집 UX
5. `animated_image`, `album_mix` 영상 모드
6. 브라우저 내 고급 영상 타임라인 편집

## 3. 기술 스택

| 영역 | 선택 |
|---|---|
| Framework | React 19 + TypeScript |
| Build | Vite |
| Routing | React Router |
| Server State | TanStack Query |
| Form | React Hook Form |
| Validation | Zod |
| UI State | Zustand 또는 React Context |
| Styling | Tailwind CSS |
| Icons | Lucide React |
| HTTP | Fetch 기반 API Client |
| Test | Vitest + React Testing Library + MSW |
| E2E | Playwright |

서버 데이터는 TanStack Query로 관리하고, 선택된 탭, 사이드바 상태, 현재 재생 중인 음원처럼 서버에 저장할 필요가 없는 UI 상태만 전역 UI Store에서 관리한다.

## 4. 정보 구조

```text
/
└─ /albums
   ├─ 앨범 목록
   ├─ /new
   │  └─ 신규 앨범 설정
   └─ /:albumId
      ├─ /plan       앨범 만들기
      ├─ /tracks     노래 만들기
      ├─ /video      플레이 루프 영상 만들기
      └─ /export     결과 및 내보내기
```

`/:albumId` 아래 화면은 동일한 작업 공간 레이아웃을 공유한다.

```text
┌──────────────┬───────────────────────────────────┐
│ Sidebar      │ Header                            │
│              ├───────────────────────────────────┤
│ 앨범 만들기  │ Album Workspace                   │
│ 노래 만들기  │                                   │
│ 영상 만들기  │                                   │
│ 내보내기     │                                   │
└──────────────┴───────────────────────────────────┘
```

## 5. 공통 레이아웃

### 5.1 Sidebar

표시 항목:

1. 서비스 로고와 이름
2. 앨범 목록
3. 현재 앨범의 제작 단계 메뉴
4. Backend/Suno 연결 상태
5. 남은 Suno 크레딧

현재 메뉴와 작업 중인 앨범을 명확히 강조한다. 제작 단계에는 다음 상태를 표시한다.

```text
미시작
진행 중
완료
오류
```

### 5.2 Header

표시 항목:

- 현재 앨범 제목
- 자동 저장 상태
- 실행 중인 Job 수
- Suno 크레딧
- 전체 ZIP 내보내기 버튼

### 5.3 Global Job Indicator

Backend 작업은 요청 직후 완료되지 않으므로 전역 작업 표시가 필요하다.

- `pending`: 대기 중
- `running`: 진행률과 함께 표시
- `succeeded`: 성공 토스트 후 관련 Query 갱신
- `failed`: 오류 메시지와 재시도 안내

MVP에서는 `GET /jobs/{job_id}`를 3초 간격으로 polling한다. 완료 상태가 되면 polling을 중단한다.

## 6. 화면 설계

## 6.1 앨범 목록

### 목적

기존 작업을 조회하거나 새 앨범 제작을 시작한다.

### UI 구성

- 제목 검색
- 상태 필터
- 앨범 카드 목록
- 새 앨범 만들기 버튼
- 최근 수정 시각과 제작 진행 상태

### API

| 동작 | API |
|---|---|
| 목록 조회 | `GET /albums` |
| 상세 이동 전 조회 | `GET /albums/{album_id}` |
| 삭제 | `DELETE /albums/{album_id}` |

삭제는 확인 Dialog를 거치고, 성공하면 목록 Query를 무효화한다.

## 6.2 앨범 만들기

### 목적

사용자가 음악 방향을 설정하고 Gemini가 앨범 공통 스타일, 트랙 제목, 가사, Suno용 영문 스타일을 생성하도록 한다.

### 입력 영역

| 필드 | UI |
|---|---|
| 앨범 제목 | Text Input |
| 아티스트명 | Text Input |
| 장르 | Select + 직접 입력 |
| 보컬 스타일 | Select + 직접 입력 |
| 템포 | Select |
| 가사 언어 | Select |
| 분위기 | Select + 다중 키워드 |
| 악기 | Multi Select |
| 주제/키워드 | Textarea |
| 추가 요청사항 | Textarea |
| 트랙 수 | Number Input, 1~30 |

### 사용자 흐름

```text
입력
 -> 앨범 초안 생성
 -> Gemini 기획 요청
 -> Job 진행 표시
 -> 트랙 목록과 공통 영문 스타일 표시
 -> 트랙별 가사 검토 및 수정
```

신규 화면에서는 `POST /albums` 완료 후 `/albums/{id}/plan`으로 이동한다. 이미 생성된 앨범에서는 `PATCH /albums/{id}`로 설정을 저장한다.

### 트랙 기획 결과

각 트랙은 Accordion으로 표시한다.

- 순번과 제목
- 곡 콘셉트
- 가사 편집기
- Suno 영문 스타일 편집기
- 이미지 프롬프트
- 가사 재생성
- 저장 상태

가사와 스타일은 서로 독립적으로 저장한다. 편집 중에는 로컬 Draft를 유지하고, 명시적 저장 또는 800ms debounce 자동 저장을 적용한다.

### API

| 동작 | API |
|---|---|
| 앨범 생성 | `POST /albums` |
| 앨범 설정 저장 | `PATCH /albums/{album_id}` |
| 앨범 기획 | `POST /albums/{album_id}/plan` |
| 트랙 목록 | `GET /albums/{album_id}/tracks` |
| 트랙 수정 | `PATCH /tracks/{track_id}` |
| 가사 저장 | `PUT /tracks/{track_id}/lyrics` |
| 스타일 저장 | `PUT /tracks/{track_id}/style` |
| 가사 재생성 | `POST /tracks/{track_id}/lyrics/regenerate` |
| 가사 다운로드 | `GET /tracks/{track_id}/lyrics/download` |

## 6.3 노래 만들기

### 목적

검토가 끝난 가사와 영문 스타일을 Suno Custom Mode에 전달하고, 생성된 음원 후보를 비교해 최종 버전을 선택한다.

### 트랙 목록 UI

각 트랙 행에 다음 정보를 표시한다.

- 순번과 제목
- 가사 준비 상태
- 음원 생성 상태
- 후보 수
- 최종 후보 선택 여부
- 노래 만들기 버튼

트랙을 펼치면 가사, 스타일, 생성 후보가 나타난다.

### 생성 전 검증

다음 조건을 만족해야 생성 버튼을 활성화한다.

1. 제목이 존재한다.
2. `instrumental=false`이면 가사가 존재한다.
3. 영문 `style_prompt`가 존재한다.
4. 동일 트랙의 생성 Job이 실행 중이지 않다.
5. Suno 연결 상태가 정상이다.

### 음원 후보

후보 카드에는 다음 정보를 표시한다.

- Suno 결과 제목
- 커버 이미지
- HTML Audio Player
- 생성 상태
- 스타일 태그
- MP3 다운로드
- 최종 선택 버튼

하나의 트랙에서 한 후보만 최종 선택할 수 있다. 다른 후보를 선택하면 이전 선택 UI는 즉시 해제한다.

### 전체 생성

앨범 전체 생성 시 준비된 트랙을 체크박스로 선택한다. Backend가 순차 처리하므로 Frontend는 앨범 Job과 각 트랙 상태를 함께 갱신한다.

### API

| 동작 | API |
|---|---|
| 단일 트랙 생성 | `POST /tracks/{track_id}/generate` |
| 앨범 선택 트랙 생성 | `POST /albums/{album_id}/generate` |
| 후보 목록 | `GET /tracks/{track_id}/generations` |
| 최종 후보 선택 | `POST /tracks/{track_id}/generations/{generation_id}/select` |
| MP3 다운로드 | `GET /assets/{asset_id}/download` |

생성 요청의 기본값:

```json
{
  "mode": "custom",
  "download_audio": true,
  "timeout_seconds": 600,
  "poll_interval_seconds": 10
}
```

## 6.4 플레이 루프 영상 만들기

### 목적

선택한 트랙에 어울리는 16:9 이미지를 만들거나 업로드하고, 시각 효과를 설정한 뒤 MP4 루프 영상을 생성한다.

### 단계 구성

```text
1. 트랙/음원 선택
2. 이미지 생성 또는 업로드
3. 이미지 후보 선택
4. 이미지 꾸미기
5. 영상 옵션 설정
6. 렌더링
7. 미리보기 및 다운로드
```

### 이미지 생성

입력:

- 대상 트랙
- 추가 이미지 지시사항
- 화면 비율
- 후보 수

결과는 가로 스크롤 가능한 후보 Gallery로 표시한다. 선택한 이미지는 강조 테두리와 선택 배지를 표시한다.

### 이미지 업로드

업로드 API는 `multipart/form-data`가 아니라 파일 원본을 Request Body로 전송한다.

```http
POST /albums/{album_id}/covers/upload?filename=cover.png
Content-Type: image/png

<binary body>
```

클라이언트에서 20MB 이하 이미지인지 먼저 검증한다.

### 이미지 꾸미기

설정 항목:

- 채우기 방식
- 밝기
- 대비
- 채도
- 블러
- 오버레이 색상과 투명도
- 제목
- 아티스트명
- 제목 위치
- 비주얼라이저 표시 여부

Frontend 미리보기는 CSS filter와 overlay를 이용해 즉시 표현한다. Backend에는 최종 설정만 저장하며 실제 합성은 영상 렌더 단계에서 수행한다.

### 영상 옵션

MVP에서는 `static_loop`만 사용자에게 노출한다.

- 해상도
- 제목 표시
- 가사 표시
- 비주얼라이저 표시
- 비주얼라이저 스타일
- 반복 모션
- Fade In/Out

### API

| 동작 | API |
|---|---|
| 이미지 생성 | `POST /albums/{album_id}/covers/generate` |
| 이미지 업로드 | `POST /albums/{album_id}/covers/upload` |
| 이미지 목록 | `GET /albums/{album_id}/covers` |
| 이미지 선택 | `POST /albums/{album_id}/covers/{asset_id}/select` |
| 꾸미기 설정 저장 | `POST /albums/{album_id}/images/{asset_id}/compose` |
| 영상 렌더링 | `POST /albums/{album_id}/videos/render` |
| 영상 다운로드 | `GET /assets/{asset_id}/download` |

## 6.5 결과 및 내보내기

### UI 구성

- 선택된 앨범 커버
- 트랙별 최종 음원
- 가사 TXT 다운로드
- 생성된 영상 목록
- 앨범 ZIP 생성
- 개별 에셋 다운로드

### API

| 동작 | API |
|---|---|
| 앨범 상세와 에셋 조회 | `GET /albums/{album_id}` |
| 앨범 ZIP 생성 | `POST /albums/{album_id}/archive` |
| ZIP 다운로드 | `GET /assets/{asset_id}/download` |

## 6.6 화면별 상세 UI 설계

본 절의 와이어프레임은 1440px 이상 데스크톱 화면을 기준으로 한다. 공통 Sidebar는 240px, Header는 64px, 본문 최대 너비는 1600px로 설계한다.

### 6.6.1 공통 작업 공간

```text
┌──────────────────────┬──────────────────────────────────────────────────────┐
│ ① Logo / 서비스명   │ ② 앨범 제목       저장됨     Credits 2,220    내보내기 │
├──────────────────────┼──────────────────────────────────────────────────────┤
│ ③ 앨범 목록          │                                                      │
│                      │ ⑤ Page Header                                        │
│ ④ 제작 메뉴          │ ┌──────────────────────────────────────────────────┐ │
│   앨범 만들기         │ │                                                  │ │
│   노래 만들기         │ │ ⑥ Page Content                                  │ │
│   영상 만들기         │ │                                                  │ │
│   결과/내보내기       │ └──────────────────────────────────────────────────┘ │
│                      │                                                      │
│                      │ ⑦ Job Drawer / Toast                                │
├──────────────────────┤                                                      │
│ Backend ●            │                                                      │
│ Suno ●               │                                                      │
└──────────────────────┴──────────────────────────────────────────────────────┘
```

| 번호 | 영역 | 동작 |
|---:|---|---|
| ① | Logo | 클릭하면 앨범 목록으로 이동 |
| ② | Global Header | 앨범 제목, 저장 상태, 크레딧, ZIP 내보내기 표시 |
| ③ | 앨범 목록 | 최근 앨범 5개와 전체 보기 제공 |
| ④ | 제작 메뉴 | 현재 앨범의 제작 단계 이동 |
| ⑤ | Page Header | 화면 제목, 설명, 주요 Action 표시 |
| ⑥ | Page Content | 화면별 편집 영역 |
| ⑦ | Job UI | 실행 중 작업과 완료/실패 알림 표시 |

Sidebar의 제작 메뉴에는 상태 아이콘을 표시한다.

```text
○ 미시작
◐ 진행 중
● 완료
! 오류
```

페이지를 이동해도 실행 중인 Job은 중단하지 않는다. Header의 작업 표시를 클릭하면 오른쪽 Job Drawer가 열린다.

### 6.6.2 앨범 목록 화면

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 앨범 프로젝트                                      [+ 새 앨범 만들기]       │
│ 제작 중이거나 완성된 앨범을 관리합니다.                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│ [제목 또는 아티스트 검색________________] [전체 상태 ▼] [최근 수정순 ▼]     │
├─────────────────────────────────────────────────────────────────────────────┤
│ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐              │
│ │ Cover            │ │ Cover            │ │ Cover            │              │
│ │                  │ │                  │ │                  │              │
│ ├──────────────────┤ ├──────────────────┤ ├──────────────────┤              │
│ │ 비 오는 날의 기억│ │ 새벽 드라이브    │ │ 재즈 카페        │              │
│ │ 가사 준비 10/10  │ │ 생성 중 4/8      │ │ 완성 12/12       │              │
│ │ 2시간 전   [⋮]   │ │ 어제       [⋮]   │ │ 3일 전     [⋮]   │              │
│ └──────────────────┘ └──────────────────┘ └──────────────────┘              │
└─────────────────────────────────────────────────────────────────────────────┘
```

카드 구성:

- 선택된 커버 또는 기본 Gradient Thumbnail
- 앨범 제목과 아티스트명
- 앨범 상태 Badge
- 트랙 완료 수
- 최근 수정 시각
- 더보기 메뉴: 열기, ZIP 생성, 삭제

상태별 UI:

| 상태 | 표시 |
|---|---|
| Loading | 3열 Skeleton Card 6개 |
| Empty | 설명과 `첫 앨범 만들기` 버튼 |
| Search Empty | 검색어 초기화 버튼 |
| Error | 오류 메시지와 다시 불러오기 버튼 |

`새 앨범 만들기`를 누르면 `/albums/new`로 이동한다. 카드 본문을 누르면 앨범 상태에 맞는 마지막 작업 화면으로 이동한다.

### 6.6.3 신규 앨범 설정 화면

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 새 앨범 만들기                                      [취소] [초안 만들기]    │
├───────────────────────────────────────────┬─────────────────────────────────┤
│ ① 기본 정보                              │ ④ 앨범 요약                     │
│ 앨범 제목 [____________________________] │ 제목: 비 오는 날의 기억         │
│ 아티스트   [____________________________] │ 장르: K-Pop                     │
│ 설명       [____________________________] │ 보컬: 여성 솔로                 │
│            [____________________________] │ 트랙: 10곡                      │
│                                           │                                 │
│ ② 음악 스타일                            │ ⑤ 생성 예상                     │
│ 장르       [K-Pop                     ▼] │ Gemini가 다음 항목을 만듭니다. │
│ 보컬       [여성 솔로                 ▼] │ · 앨범 공통 영문 스타일         │
│ 템포       [중간 90-110 BPM           ▼] │ · 트랙별 제목과 콘셉트          │
│ 가사 언어  [한국어                    ▼] │ · 트랙별 가사                   │
│ 분위기     [차분함] [향수] [+]          │ · Suno용 영문 스타일            │
│ 악기       [신시사이저] [피아노] [+]     │                                 │
│                                           │                                 │
│ ③ 기획 요청                              │                                 │
│ 주제/키워드 [__________________________] │                                 │
│ 추가 요청   [__________________________] │                                 │
│ 트랙 수     [-] 10 [+]                   │                                 │
└───────────────────────────────────────────┴─────────────────────────────────┘
```

동작 규칙:

1. 필수값은 앨범 제목과 트랙 수다.
2. `초안 만들기`는 `POST /albums`만 호출한다.
3. 생성 성공 후 `/albums/{albumId}/plan`으로 이동한다.
4. 사용자가 입력한 값은 요청 중 수정하지 못하도록 잠근다.
5. 실패 시 입력값을 유지하고 버튼 영역에 오류를 표시한다.

태블릿에서는 요약 Panel을 입력 폼 아래로 이동한다.

### 6.6.4 앨범 만들기 화면

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 앨범 만들기                    마지막 저장 23:14     [설정 수정] [기획 생성] │
├─────────────────────────────────────────────────────────────────────────────┤
│ ① 음악 스타일 설정                                                         │
│ [장르 K-Pop ▼] [보컬 여성 솔로 ▼] [템포 중간 ▼] [언어 한국어 ▼]           │
│ [분위기 차분하고 향수 어린 ▼] [악기 신시사이저, 피아노 ▼]                  │
│ 주제/키워드 [비 오는 날, 오래된 친구, 카페______________________________] │
│ 추가 요청   [후렴을 쉽게 기억할 수 있게________________________________] │
├─────────────────────────────────────────────────────────────────────────────┤
│ ② 공통 Suno 스타일                                               [복사]    │
│ Classic Korean synthpop, 90s nostalgia, soft female vocals, warm analog... │
├─────────────────────────────────────────────────────────────────────────────┤
│ ③ 완성된 플레이리스트                                      10 tracks       │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ 1  비 온 사진 속의 우리             가사 준비됨      [재생성] [저장]  │ │
│ ├─────────────────────────────────────────────────────────────────────────┤ │
│ │ [가사] [영문 스타일] [이미지 프롬프트]                                │ │
│ │ ┌─────────────────────────────────────────────────────────────────────┐ │ │
│ │ │ [Verse 1]                                                          │ │ │
│ │ │ 먼지 쌓인 앨범을 넘기는데...                                      │ │ │
│ │ │                                                                     │ │ │
│ │ └─────────────────────────────────────────────────────────────────────┘ │ │
│ │ 1,240자                                      저장됨     [TXT 다운로드] │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ 2  비 내리는 창가점                 가사 준비됨                   [⌄] │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

영역별 동작:

| 영역 | 동작 |
|---|---|
| ① 스타일 설정 | 수정 후 앨범 설정 저장, 재기획 여부 확인 |
| ② 공통 스타일 | 읽기 전용 표시와 Clipboard 복사 |
| ③ 트랙 목록 | 한 번에 하나의 Accordion만 펼치는 것을 기본값으로 사용 |

`기획 생성`을 누르면 확인 Dialog를 표시한다.

```text
새 기획을 생성하면 현재 트랙 내용이 교체될 수 있습니다.
[취소] [새로 생성]
```

기획 Job 실행 중:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Gemini가 앨범을 기획하고 있습니다.                              45%         │
│ 트랙 제목과 가사, Suno 영문 스타일을 작성 중입니다.                       │
│ [██████████████████──────────────────────]                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

가사 편집기는 고정폭 글꼴을 사용하고 최소 높이 420px, 최대 높이 65vh로 한다. 저장되지 않은 변경이 있으면 트랙 Header와 저장 버튼에 점 표시를 추가한다.

### 6.6.5 노래 만들기 화면

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 노래 만들기                   선택 3곡     Credits 2,220 [선택 곡 생성]     │
├─────────────────────────────────────────────────────────────────────────────┤
│ [전체] [생성 전] [생성 중] [후보 있음] [선택 완료]                         │
├─────────────────────────────────────────────────────────────────────────────┤
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ ☑ 1  비 온 사진 속의 우리       후보 2개       선택 완료      [⌃]     │ │
│ ├─────────────────────────────────────────────────────────────────────────┤ │
│ │ ① 입력 확인                                                           │ │
│ │ 가사 1,240자   스타일 286자   Custom Mode              [내용 수정]    │ │
│ │                                                                         │ │
│ │ ② 생성 후보                                                           │ │
│ │ ┌──────────────────────────────┐ ┌──────────────────────────────┐       │ │
│ │ │ Cover  Candidate A    선택됨│ │ Cover  Candidate B          │       │ │
│ │ │ ▶ 0:29 ━━━━━━━ 3:34         │ │ ▶ 0:00 ━━━━━━━ 3:41         │       │ │
│ │ │ tags...                    │ │ tags...                    │       │ │
│ │ │ [MP3 다운로드] [최종 선택] │ │ [MP3 다운로드] [최종 선택] │       │ │
│ │ └──────────────────────────────┘ └──────────────────────────────┘       │ │
│ │                                             [후보 다시 만들기]          │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────────────────────┐ │
│ │ ☐ 2  비 내리는 창가점         생성 전                      [노래 만들기]│ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

트랙 Header 상태:

| 상태 | 우측 Action |
|---|---|
| `lyrics_ready` | `노래 만들기` |
| `pending`, `running` | Progress와 `생성 중` 비활성 버튼 |
| 후보 생성 완료 | 후보 수와 `후보 다시 만들기` |
| 최종 선택 완료 | `선택 완료` Badge와 재생 버튼 |
| 실패 | 오류 요약과 `다시 시도` |

생성 버튼을 누르면 크레딧 사용 안내 Dialog를 표시한다. 현재 Backend가 정확한 예상 크레딧을 제공하지 않으므로 비용 수치를 임의로 표시하지 않는다.

오디오 플레이어는 Sticky Mini Player와 연동한다.

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Cover  비 온 사진 속의 우리 - Candidate A    ◀  ▶  ━━━━━━━  0:29 / 3:34  │
└─────────────────────────────────────────────────────────────────────────────┘
```

Mini Player는 화면 하단에 고정하고, 다른 후보 재생 시 현재 Audio Source를 교체한다.

### 6.6.6 플레이 루프 영상 만들기 화면

영상 화면은 한 페이지 안에서 4단계 Stepper를 사용한다.

```text
[1 음원 선택] ─── [2 이미지 선택] ─── [3 꾸미기] ─── [4 영상 생성]
```

#### Step 1: 음원 선택

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 영상에 사용할 음원 선택                                                     │
│ ● 01 비 온 사진 속의 우리       3:34       ▶ 미리듣기                      │
│ ○ 02 비 내리는 창가점           3:41       ▶ 미리듣기                      │
│ ○ 03 청춘의 언덕                3:12       ▶ 미리듣기                      │
│                                                         [다음: 이미지 선택] │
└─────────────────────────────────────────────────────────────────────────────┘
```

최종 Generation이 선택된 트랙만 목록에 표시한다.

#### Step 2: 이미지 선택

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 이미지 만들기                                                               │
│ [AI 이미지 생성] [파일 업로드]                                              │
│ 추가 요청 [빗바랜 앨범을 보고 있는 중년________________________________] │
│ 화면 비율 [16:9 ▼]   후보 수 [4 ▼]                     [이미지 생성]       │
├─────────────────────────────────────────────────────────────────────────────┤
│ 이미지 후보                                                                 │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │
│ │              │ │              │ │              │ │              │        │
│ │ Candidate 1  │ │ Candidate 2  │ │ Candidate 3  │ │ Candidate 4  │        │
│ │     ✓        │ │              │ │              │ │              │        │
│ └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘        │
│                                               [이전] [다음: 이미지 꾸미기] │
└─────────────────────────────────────────────────────────────────────────────┘
```

이미지 생성 중에는 후보 카드 자리에 비율을 유지한 Skeleton을 표시한다. 업로드는 Drop Zone과 파일 선택 버튼을 함께 제공한다.

#### Step 3: 이미지 꾸미기

```text
┌──────────────────────────────────────────────┬──────────────────────────────┐
│ ① 16:9 실시간 Preview                       │ ② 편집 설정                  │
│ ┌──────────────────────────────────────────┐ │ 채우기 [화면 채우기 ▼]      │
│ │                                          │ │ 밝기   ─────●────  0        │
│ │                PLAY LIST                 │ │ 대비   ─────●────  0        │
│ │                                          │ │ 채도   ─────●────  0        │
│ │ Artist Name                              │ │ 블러   ●─────────  0        │
│ │                         ▂▅▇▃▆           │ │                              │
│ └──────────────────────────────────────────┘ │ 오버레이 [■ #21000f] 15%    │
│                                              │ 제목 [PLAY LIST__________]   │
│                                              │ 아티스트 [_______________]   │
│                                              │ 위치 [왼쪽 아래 ▼]          │
│                                              │ ☑ 비주얼라이저              │
│                                              │                              │
│                                              │ [초기화] [설정 저장]         │
└──────────────────────────────────────────────┴──────────────────────────────┘
```

Preview는 Backend 이미지 렌더 결과가 아니라 CSS 기반 근사 미리보기다. `설정 저장` 성공 후 Step 4로 이동한다.

#### Step 4: 영상 생성

```text
┌──────────────────────────────────────────────┬──────────────────────────────┐
│ ① 최종 Preview                              │ ② 영상 설정                  │
│ ┌──────────────────────────────────────────┐ │ 모드 [Static Loop]           │
│ │                                          │ │ 해상도 [1920x1080 ▼]        │
│ │           선택 이미지 Preview            │ │ ☑ 제목 표시                 │
│ │                                          │ │ ☐ 가사 표시                 │
│ └──────────────────────────────────────────┘ │ ☑ 비주얼라이저              │
│ 음원: 비 온 사진 속의 우리 · 3:34           │ 모션 [Slow Zoom ▼]           │
│                                              │ Fade In  [1.0] 초            │
│                                              │ Fade Out [1.0] 초            │
│                                              │                              │
│                                              │ [영상 만들기]                │
└──────────────────────────────────────────────┴──────────────────────────────┘
```

렌더링 중에는 화면 이탈이 가능하다는 안내와 Job 진행 상태를 표시한다. 완료되면 같은 영역을 Video Player와 `MP4 다운로드` 버튼으로 교체한다.

### 6.6.7 결과 및 내보내기 화면

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 결과 및 내보내기                                  [앨범 ZIP 만들기]         │
├─────────────────────────────────────┬───────────────────────────────────────┤
│ ① 앨범 요약                        │ ② 완성도                             │
│ [Selected Cover]                    │ 가사             10 / 10             │
│ 비 오는 날의 기억                  │ 최종 음원         8 / 10              │
│ Playlist Studio                     │ 커버 이미지       완료                │
│ 10 tracks                           │ 루프 영상         완료                │
├─────────────────────────────────────┴───────────────────────────────────────┤
│ ③ 트랙 결과                                                                 │
│ 01 비 온 사진 속의 우리    ▶ 3:34    [MP3] [가사 TXT]                      │
│ 02 비 내리는 창가점        ▶ 3:41    [MP3] [가사 TXT]                      │
│ 03 청춘의 언덕             음원 미선택              [노래 만들기로 이동]  │
├─────────────────────────────────────────────────────────────────────────────┤
│ ④ 이미지와 영상                                                            │
│ [Cover Thumbnail] cover.png [다운로드]                                      │
│ [Video Thumbnail] playlist.mp4 3:34 [재생] [다운로드]                      │
├─────────────────────────────────────────────────────────────────────────────┤
│ ⑤ 앨범 패키지                                                               │
│ MP3 8개 · 가사 10개 · Cover 1개 · Video 1개                               │
│ [ZIP 만들기] 또는 [album-name.zip 다운로드]                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

ZIP 생성 전 누락된 에셋이 있으면 경고만 표시하고 생성 자체는 허용한다. Backend는 선택된 음원만 ZIP에 포함하므로 UI에도 실제 포함 예정 파일 수를 표시한다.

### 6.6.8 공통 Dialog

#### 생성 확인

```text
노래를 생성할까요?
Suno 크레딧이 사용되며 완료까지 몇 분이 걸릴 수 있습니다.

[취소] [생성 시작]
```

#### 페이지 이탈

```text
저장되지 않은 변경사항이 있습니다.

[계속 편집] [변경사항 버리고 이동]
```

#### 오류 상세

```text
음원 생성에 실패했습니다.
Suno 인증 상태 또는 크레딧을 확인한 뒤 다시 시도해 주세요.

오류 코드: SUNO_GENERATION_FAILED

[닫기] [상태 확인] [다시 시도]
```

### 6.6.9 반응형 UI 규칙

| 화면 너비 | 레이아웃 |
|---|---|
| 1440px 이상 | Sidebar 240px, 본문 2열, 트랙 후보 2~3열 |
| 1024~1439px | Sidebar 220px, 본문 2열 유지, 후보 2열 |
| 768~1023px | Sidebar Drawer, 편집 Panel 1열, 후보 2열 |
| 768px 미만 | 조회와 간단 편집만 허용하는 제한 UI |

가사 편집, 이미지 합성, 영상 옵션은 작업 공간이 넓어야 하므로 768px 미만에서는 데스크톱 사용 권장 Banner를 표시한다.

### 6.6.10 UI 상태 표시 기준

모든 주요 Panel은 다음 5개 상태를 구현한다.

```text
idle
loading
success
empty
error
```

Job이 있는 Panel은 `pending`, `running`을 추가한다. 로딩 중에도 기존 데이터가 있으면 화면을 Skeleton으로 교체하지 않고 기존 데이터를 유지한 채 갱신 Indicator만 표시한다.

## 7. Frontend 데이터 모델

Backend 응답은 `{ "data": ... }` 구조이므로 API Client에서 Envelope를 제거한다.

```ts
type ApiResponse<T> = {
  data: T;
};

type AlbumStatus =
  | "draft"
  | "planning"
  | "lyrics_ready"
  | "generating"
  | "partially_complete"
  | "complete"
  | "failed"
  | "archived";

interface Album {
  id: string;
  title: string;
  artist_name: string | null;
  description: string | null;
  genre: string;
  vocal_style: string;
  tempo: string;
  lyrics_language: string;
  mood: string;
  instruments: string[];
  keywords: string;
  additional_instructions: string;
  style_prompt: string;
  track_count: number;
  status: AlbumStatus;
  selected_cover_asset_id: string | null;
  created_at: string;
  updated_at: string;
}

interface Track {
  id: string;
  album_id: string;
  sequence: number;
  title: string;
  concept: string;
  lyrics: string;
  style_prompt: string;
  image_prompt: string;
  negative_tags: string;
  instrumental: boolean;
  model: string;
  status: string;
  selected_generation_id: string | null;
}

interface Generation {
  id: string;
  track_id: string;
  job_id: string;
  clip_id: string;
  status: string;
  title: string;
  audio_url: string | null;
  image_url: string | null;
  local_audio_path: string | null;
  generated_lyrics: string | null;
  tags: string | null;
  is_selected: boolean;
}

interface Job {
  id: string;
  type: string;
  resource_type: string;
  resource_id: string;
  status: "pending" | "running" | "succeeded" | "failed";
  progress: number;
  error_code: string | null;
  error_message: string | null;
  result: unknown;
}

interface Asset {
  id: string;
  album_id: string | null;
  track_id: string | null;
  generation_id: string | null;
  type: "audio" | "lyrics" | "cover" | "composed_image" | "video" | "archive";
  original_name: string;
  content_type: string;
  size_bytes: number;
  metadata: Record<string, unknown>;
}
```

실제 Backend의 SQLite 필드가 JSON 문자열 또는 정수 Boolean으로 반환될 수 있으므로 API Client의 정규화 계층에서 다음 변환을 수행한다.

```text
instruments_json -> instruments[]
metadata_json -> metadata
instrumental: 0/1 -> boolean
is_selected: 0/1 -> boolean
```

## 8. API Client 설계

```text
src/api/
  client.ts
  albums.ts
  tracks.ts
  generations.ts
  jobs.ts
  assets.ts
  system.ts
  types.ts
  normalize.ts
```

환경 변수:

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1
```

공통 Client 책임:

1. Base URL 결합
2. JSON 직렬화와 역직렬화
3. `{data}` Envelope 해제
4. HTTP 오류를 `ApiError`로 정규화
5. 파일 다운로드 응답 처리
6. 요청 취소를 위한 `AbortSignal` 전달

파일 URL은 다음 함수로 생성한다.

```ts
const assetDownloadUrl = (assetId: string) =>
  `${API_BASE_URL}/assets/${assetId}/download`;
```

## 9. Query와 Mutation 설계

### Query Key

```ts
const queryKeys = {
  albums: ["albums"] as const,
  album: (albumId: string) => ["albums", albumId] as const,
  tracks: (albumId: string) => ["albums", albumId, "tracks"] as const,
  track: (trackId: string) => ["tracks", trackId] as const,
  generations: (trackId: string) => ["tracks", trackId, "generations"] as const,
  covers: (albumId: string) => ["albums", albumId, "covers"] as const,
  job: (jobId: string) => ["jobs", jobId] as const,
  sunoStatus: ["system", "suno-status"] as const,
};
```

### Job Polling

```text
pending/running -> 3초마다 조회
succeeded       -> polling 종료, 관련 Album/Track/Asset Query 무효화
failed          -> polling 종료, 오류 표시
```

브라우저 탭이 백그라운드이면 polling 간격을 늘리고, 화면을 벗어나도 전역 Job Store에 등록된 작업은 완료될 때까지 추적한다.

### Optimistic Update

다음 동작에만 낙관적 업데이트를 적용한다.

- 생성 후보 선택
- 커버 선택
- 트랙 제목과 콘셉트 저장

음원 생성, 이미지 생성, 영상 렌더처럼 외부 서비스 결과가 필요한 작업에는 적용하지 않는다.

## 10. 상태 관리

### Server State

TanStack Query:

- 앨범과 트랙
- 생성 후보
- Job
- Asset
- Suno 상태와 크레딧

### Form State

React Hook Form:

- 앨범 스타일 설정
- 트랙 편집
- 이미지 생성 요청
- 이미지 합성 설정
- 영상 렌더 설정

### UI State

Zustand 또는 Context:

- Sidebar 접힘 상태
- 현재 열린 트랙 ID
- 현재 재생 중인 Generation ID
- 전역 Job 목록
- Toast와 Dialog

서버 데이터를 Zustand에 중복 저장하지 않는다.

## 11. 컴포넌트 구조

```text
src/
  app/
    router.tsx
    providers.tsx
  api/
  components/
    layout/
      AppShell.tsx
      Sidebar.tsx
      Header.tsx
      JobIndicator.tsx
    common/
      AsyncButton.tsx
      ConfirmDialog.tsx
      EmptyState.tsx
      ErrorPanel.tsx
      StatusBadge.tsx
    album/
      AlbumForm.tsx
      AlbumCard.tsx
      StylePromptPanel.tsx
    track/
      TrackAccordion.tsx
      TrackEditor.tsx
      LyricsEditor.tsx
      GenerationCard.tsx
      AudioPlayer.tsx
    image/
      CoverGallery.tsx
      ImageUploader.tsx
      ImageComposer.tsx
      ComposerPreview.tsx
    video/
      VideoRenderForm.tsx
      VideoPreview.tsx
  features/
    albums/
    planning/
    generation/
    video/
    export/
  hooks/
    useJobPolling.ts
    useAudioController.ts
    useAutoSave.ts
  pages/
    AlbumListPage.tsx
    AlbumCreatePage.tsx
    AlbumPlanPage.tsx
    TrackGenerationPage.tsx
    VideoCreationPage.tsx
    ExportPage.tsx
  stores/
    uiStore.ts
    jobStore.ts
  styles/
  utils/
```

## 12. 오디오 재생

여러 후보가 동시에 재생되지 않도록 전역 Audio Controller를 사용한다.

1. 새 후보 재생 시 기존 음원을 정지한다.
2. 페이지 이동 시 재생을 정지한다.
3. 원격 `audio_url`보다 Backend에 저장된 Asset URL을 우선한다.
4. 로딩, 재생 실패, 재시도 상태를 플레이어에 표시한다.

## 13. 오류 처리

오류 표시 수준:

| 오류 | 표시 방식 |
|---|---|
| 입력 검증 | 필드 하단 메시지 |
| 저장 실패 | 해당 편집 패널의 Inline Error |
| Job 실패 | Job Panel + Toast |
| Suno 연결 실패 | Header 경고 Banner |
| 페이지 조회 실패 | Page Error State |
| 다운로드 실패 | Toast |

Backend의 현재 오류가 `{"detail": "..."}` 또는 일반 HTTP 오류 형태일 수 있으므로 Client에서 공통 형식으로 변환한다.

```ts
interface ApiError {
  status: number;
  code?: string;
  message: string;
  retryable: boolean;
}
```

`429`, `502`, `503`, `504`는 재시도 가능 오류로 분류하되, 생성 Mutation을 자동 재요청하지 않는다. 중복 생성 위험이 있으므로 사용자가 Job 상태를 확인한 뒤 명시적으로 다시 실행한다.

## 14. 접근성과 반응형

1. 모든 입력에 `label`을 연결한다.
2. Accordion, Dialog, Select는 키보드로 조작할 수 있어야 한다.
3. 상태를 색상만으로 표현하지 않고 텍스트나 아이콘을 함께 사용한다.
4. 진행 상태는 `aria-live`로 알린다.
5. 데스크톱은 고정 Sidebar와 2열 편집 화면을 사용한다.
6. 태블릿 이하는 Sidebar를 Drawer로 전환한다.
7. MVP의 최소 지원 너비는 768px로 한다.

## 15. 시각 디자인 원칙

첨부된 참고 화면처럼 어두운 와인/버건디 계열의 제작 도구 UI를 사용한다.

```text
Background: #120008
Surface:    #22000f
Panel:      #300016
Primary:    #ff7a32
Accent:     #ff4d8d
Text:       #fff7f2
Muted:      #c6aeb8
Border:     rgba(255, 180, 150, 0.18)
```

원칙:

- 긴 가사 편집과 후보 비교가 중심이므로 장식보다 가독성을 우선한다.
- 생성 버튼은 크레딧을 사용하는 작업임을 명확히 표시한다.
- 진행 중인 영역은 glow보다 Progress와 상태 문구로 전달한다.
- 카드와 패널의 중첩 깊이는 최대 3단계로 제한한다.

## 16. 보안

1. `SESSION_ID`, `COOKIE`, Gemini API Key는 Frontend에 전달하지 않는다.
2. Frontend 환경 변수에는 공개 가능한 Backend URL만 둔다.
3. Backend 오류의 인증 토큰이나 내부 경로를 화면에 그대로 출력하지 않는다.
4. 업로드 파일의 MIME, 확장자, 크기를 Client와 Backend 양쪽에서 검증한다.
5. 사용자 입력을 HTML로 직접 렌더링하지 않는다.
6. 가사와 제목은 React 텍스트 렌더링을 사용하고, Markdown이 필요하면 제한된 sanitizer를 적용한다.

## 17. 테스트 전략

### 단위 테스트

- 응답 Envelope와 데이터 정규화
- Job 종료 상태 판별
- 생성 버튼 활성화 조건
- 이미지 합성 Preview 값 계산
- 오류 메시지 변환

### 컴포넌트 테스트

- 앨범 폼 입력과 검증
- 트랙 Accordion 편집과 저장
- 생성 후보 선택
- Job 진행 상태
- 이미지 업로드 제한

### 통합 테스트

MSW로 Backend를 모의한다.

1. 앨범 생성 후 기획 Job 완료
2. 트랙 가사 수정과 저장
3. Suno 생성 Job 완료 후 후보 표시
4. 후보 선택 후 Audio Player 재생
5. 이미지 생성과 선택
6. 영상 렌더 완료 후 다운로드

### E2E

실제 Backend를 실행한 상태에서 다음 흐름을 검증한다.

```text
앨범 생성
 -> Gemini 기획
 -> 가사 수정
 -> 1개 트랙 음원 생성
 -> 후보 선택
 -> 이미지 생성 또는 업로드
 -> 영상 렌더
 -> MP4 다운로드
```

실제 Gemini/Suno 호출 테스트는 비용이 발생하므로 별도 smoke 실행으로 분리한다.

## 18. 구현 단계

### Phase 1: 기반

1. Vite React TypeScript 프로젝트 생성
2. Router, Query Client, Theme, API Client 구성
3. 공통 Layout과 상태 컴포넌트 구현
4. 앨범 목록과 생성 화면 구현

### Phase 2: 앨범 기획

1. 앨범 스타일 폼
2. Gemini 기획 Job polling
3. 트랙 Accordion
4. 가사와 스타일 편집 및 저장

### Phase 3: 노래 생성

1. 단일/전체 트랙 생성
2. Job 진행 상태
3. 생성 후보 Gallery
4. Audio Player와 후보 선택

### Phase 4: 이미지와 영상

1. Gemini 이미지 생성과 업로드
2. 이미지 후보 선택
3. 이미지 꾸미기 Preview
4. 영상 렌더 설정과 Job polling
5. MP4 미리보기와 다운로드

### Phase 5: 내보내기와 안정화

1. 개별 에셋 다운로드
2. 앨범 ZIP 생성
3. 오류 및 빈 상태 보완
4. 통합/E2E 테스트
5. 접근성과 반응형 점검

## 19. Backend 계약상 주의점

현재 구현 기준으로 Frontend가 고려할 사항:

1. Job 취소와 재시도 전용 API는 아직 없다.
2. 트랙 순서 일괄 변경 API는 아직 없다. 개별 `PATCH /tracks/{id}`를 사용한다.
3. Generation 삭제 API는 아직 없다.
4. 영상은 MVP에서 `static_loop`만 정상 지원한다.
5. 이미지 합성 API는 별도 이미지 파일을 즉시 만들지 않고 Asset metadata에 설정을 저장한다.
6. Archive 생성은 비동기 Job이 아니라 요청 안에서 완료된 Asset을 반환한다.
7. 업로드는 raw binary body 방식이다.
8. 데이터 필드 일부는 Backend 정규화 전까지 `*_json` 형태로 전달될 수 있다.

이 차이는 API Client와 UI 기능 노출에서 흡수하고, 구현되지 않은 동작은 버튼을 표시하지 않는다.

## 20. MVP 완료 조건

다음 시나리오가 브라우저에서 처음부터 끝까지 동작하면 Frontend MVP 완료로 본다.

1. 사용자가 앨범 스타일과 트랙 수를 입력한다.
2. Gemini 기획 Job의 진행 상태를 확인한다.
3. 생성된 트랙별 가사와 영문 스타일을 편집하고 저장한다.
4. 선택한 트랙을 Suno로 생성한다.
5. 음원 후보를 재생하고 최종 후보를 선택한다.
6. Gemini 이미지 후보를 만들거나 이미지를 업로드한다.
7. 이미지를 선택하고 꾸미기 옵션을 설정한다.
8. 선택 음원과 이미지로 루프 영상을 렌더링한다.
9. MP3, 가사, 이미지, MP4를 확인하고 다운로드한다.
10. 앨범 ZIP을 생성하고 다운로드한다.
