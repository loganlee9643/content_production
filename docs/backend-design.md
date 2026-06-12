# 앨범 제작 서비스 Backend 설계

## 1. 문서 목적

이 문서는 React 기반 앨범 제작 UI를 지원하기 위한 Backend(BE)의 구조, 데이터 모델, API, 비동기 작업, 파일 저장 정책을 정의한다.

루트의 `backend`를 통합 Backend로 사용한다. 하나의 FastAPI 애플리케이션이 Suno 연동과 함께 앨범과 트랙의 수명 주기, 생성 작업, 결과 후보, 가사, 오디오, 이미지와 영상 산출물을 관리한다.

배포 및 실행 단위는 다음 두 개다.

1. `backend`: 통합 FastAPI Backend
2. React Frontend

Suno 연동 코드는 별도 서버로 분리하지 않고, 통합 Backend 내부의 integration 계층으로 격리한다.

## 2. 목표 범위

### 2.1 화면별 대표 기능

| 화면 | 대표 기능 | 외부 연동 | 핵심 산출물 |
|---|---|---|---|
| 앨범 만들기 | 음악 스타일 설정 후 앨범과 수록곡 기획 | Gemini Text | 트랙 제목, 가사, Suno용 영문 스타일 |
| 노래 만들기 | 확정된 가사와 영문 스타일로 음원 생성 | Suno | 생성 후보, MP3, 결과 가사, 이미지 |
| 플레이 루프 영상 만들기 | 어울리는 이미지 생성, 꾸미기, 영상 렌더링 | Gemini Image, FFmpeg/Veo | 배경 이미지, 편집 설정, 루프 영상 |

```text
[앨범 만들기]
음악 스타일 설정
  -> Gemini 앨범 기획
  -> 제목 + 가사 + Suno용 영문 스타일 생성
  -> 사용자 검토 및 수정

[노래 만들기]
확정 가사 + Suno용 영문 스타일
  -> Suno Custom Mode
  -> 후보 재생 및 선택
  -> MP3 저장

[플레이 루프 영상 만들기]
선택 음원 + 가사 + 분위기
  -> Gemini 이미지 프롬프트 및 이미지 생성
  -> 이미지 선택 및 꾸미기
  -> FFmpeg/Veo 영상 생성
```

### 2.2 MVP

1. 앨범 기본 정보와 음악 스타일 설정
2. Gemini 기반 앨범 단위 플레이리스트 기획
3. Gemini 기반 트랙별 제목, 가사와 Suno용 영문 스타일 생성 및 수정
4. Suno 음악 생성 요청
5. 생성 상태 조회와 실패 재시도
6. 생성 후보 재생 및 최종 후보 선택
7. MP3, 가사, 앨범 ZIP 다운로드
8. Gemini 기반 이미지 생성과 사용자 이미지 업로드
9. 이미지 꾸미기와 플레이 루프 영상 렌더링
10. 앨범 제작 이력 조회

### 2.3 확장 범위

1. 장면이 변하는 다중 이미지 영상
2. Gemini/Veo 기반 이미지 애니메이션
3. 사용자 계정, 포인트와 사용량 관리
4. 알림
5. 협업과 권한 관리
6. 외부 Object Storage 및 작업 큐 도입

## 3. 시스템 구성

```text
React Frontend
      |
      | REST / SSE
      v
backend
통합 FastAPI Backend
  - Album API
  - Track API
  - Generation API
  - Asset API
  - Job API
  - Suno Integration
  - Gemini Integration
  - Video Renderer
      |
      +-------------------+
      |                   |
      v                   v
SQLite/PostgreSQL     Local Storage/S3
      |
      v
Suno Web API
```

실행 프로세스:

```text
Process 1: backend (FastAPI Backend)
Process 2: React Frontend
```

### 3.1 컴포넌트 책임

| 컴포넌트 | 책임 |
|---|---|
| React Frontend | 입력 폼, 진행 상태, 가사 편집, 오디오 재생, 다운로드 |
| 통합 FastAPI Backend | 도메인 API, 검증, 상태 관리, 작업 오케스트레이션 |
| Suno Integration | Backend 내부에서 Suno 인증, 곡/가사 생성, feed 및 크레딧 조회 |
| Gemini Integration | 앨범 기획, 가사, 영문 스타일, 이미지 프롬프트와 이미지 생성 |
| Video Renderer | 이미지 꾸미기, 오디오 결합, FFmpeg 루프 영상 및 선택적 Veo 영상 생성 |
| Database | 앨범, 트랙, 작업, 생성 후보, 자산 메타데이터 저장 |
| File Storage | MP3, TXT, 이미지, 영상, ZIP 저장 |
| Worker | 폴링, 다운로드, ZIP 생성, 영상 렌더링 |

## 4. 설계 원칙

1. `backend`를 전체 서비스의 통합 Backend로 사용한다.
2. Suno 연동 코드는 동일 FastAPI 애플리케이션 내부의 integration 계층으로 격리한다.
3. 프론트엔드는 Suno의 clip ID나 응답 구조에 직접 의존하지 않는다.
4. 오래 걸리는 작업은 요청-응답 안에서 완료하지 않고 Job으로 관리한다.
5. DB에는 상태와 메타데이터를 저장하고 대용량 파일은 파일 저장소에 저장한다.
6. 모든 생성 요청은 재시도와 중복 요청을 고려해 멱등성을 갖도록 한다.
7. Suno 인증정보는 서버에서만 관리하며 API 응답과 로그에 노출하지 않는다.

## 5. 권장 프로젝트 구조

```text
backend/
  main.py
  start_suno_server.py
  app/
    api/
      albums.py
      tracks.py
      jobs.py
      assets.py
      system.py
    core/
      config.py
      database.py
      errors.py
      logging.py
    models/
      album.py
      track.py
      generation.py
      job.py
      asset.py
    schemas/
      album.py
      track.py
      generation.py
      job.py
      asset.py
    services/
      album_service.py
      planning_service.py
      lyrics_service.py
      generation_service.py
      download_service.py
      archive_service.py
      cover_service.py
      video_service.py
    integrations/
      suno_client.py
      gemini_text_client.py
      gemini_image_client.py
      veo_video_client.py
    renderers/
      image_composer.py
      loop_video_renderer.py
    workers/
      job_runner.py
      generation_worker.py
      asset_worker.py
    repositories/
      album_repository.py
      track_repository.py
      job_repository.py
  storage/
  tests/
```

기존 `main.py`, `utils.py`, `cookie.py`의 기능은 단계적으로 `app/api`, `app/services`, `app/integrations`로 이동한다. 마이그레이션 중에는 기존 라우트를 유지할 수 있지만, 최종적으로 Suno API 프록시 라우트와 애플리케이션 도메인 라우트의 코드 책임을 분리한다.

이 분리는 코드 계층 분리이며 서버 프로세스 분리를 의미하지 않는다. 모든 API는 하나의 Uvicorn/FastAPI 프로세스에서 제공한다.

## 6. 도메인 모델

### 6.1 Album

앨범 제작의 최상위 단위다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | UUID | 내부 앨범 ID |
| `title` | string | 앨범 제목 |
| `artist_name` | string, nullable | 아티스트명 |
| `description` | text, nullable | 앨범 설명 |
| `genre` | string | 장르 |
| `vocal_style` | string | 보컬 성별 및 스타일 |
| `tempo` | string | 느림/중간/빠름 또는 BPM 범위 |
| `lyrics_language` | string | 가사 언어 |
| `mood` | string | 분위기 |
| `instruments` | JSON | 악기 목록 |
| `keywords` | text | 주제와 키워드 |
| `additional_instructions` | text | 추가 요구사항 |
| `style_prompt` | text | 최종 공통 스타일 프롬프트 |
| `track_count` | integer | 목표 트랙 수 |
| `status` | enum | 앨범 상태 |
| `selected_cover_asset_id` | UUID, nullable | 선택된 커버 |
| `created_at` | datetime | 생성 시각 |
| `updated_at` | datetime | 수정 시각 |

앨범 상태:

```text
draft
planning
lyrics_ready
generating
partially_complete
complete
failed
archived
```

### 6.2 Track

앨범에 포함된 곡의 기획과 최종 결과를 관리한다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | UUID | 내부 트랙 ID |
| `album_id` | UUID | 소속 앨범 |
| `sequence` | integer | 앨범 내 순서 |
| `title` | string | 곡 제목 |
| `concept` | text | 곡별 주제와 전개 |
| `lyrics` | text | 현재 확정 가사 |
| `style_prompt` | text | Suno 요청용 곡별 영문 스타일 |
| `image_prompt` | text, nullable | Gemini 이미지 생성용 영문 프롬프트 |
| `negative_tags` | text | 제외할 스타일 |
| `instrumental` | boolean | 연주곡 여부 |
| `model` | string | Suno 모델 |
| `status` | enum | 트랙 상태 |
| `selected_generation_id` | UUID, nullable | 최종 선택 후보 |
| `created_at` | datetime | 생성 시각 |
| `updated_at` | datetime | 수정 시각 |

트랙 상태:

```text
draft
lyrics_generating
lyrics_ready
queued
submitted
streaming
complete
failed
cancelled
```

### 6.3 Generation

Suno 생성 요청과 반환된 후보를 저장한다. 한 트랙에 여러 번 생성할 수 있고, 한 요청에서 후보가 2개 이상 나올 수 있다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | UUID | 내부 생성 후보 ID |
| `track_id` | UUID | 대상 트랙 |
| `job_id` | UUID | 생성 작업 |
| `request_id` | string, nullable | Suno 요청 ID |
| `clip_id` | string | Suno clip ID |
| `status` | string | Suno 생성 상태 |
| `title` | string | Suno 결과 제목 |
| `audio_url` | text, nullable | 원격 오디오 URL |
| `image_url` | text, nullable | 원격 이미지 URL |
| `local_audio_path` | text, nullable | 저장된 MP3 경로 |
| `generated_lyrics` | text, nullable | 결과 가사 |
| `tags` | text, nullable | 결과 스타일 태그 |
| `raw_response` | JSON | 원본 응답 |
| `is_selected` | boolean | 최종 선택 여부 |
| `created_at` | datetime | 생성 시각 |
| `completed_at` | datetime, nullable | 완료 시각 |

### 6.4 Job

비동기 작업을 공통 형태로 관리한다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | UUID | Job ID |
| `type` | enum | 작업 유형 |
| `resource_type` | string | album, track, generation 등 |
| `resource_id` | UUID | 대상 리소스 ID |
| `status` | enum | 작업 상태 |
| `progress` | integer | 0~100 |
| `attempt` | integer | 실행 횟수 |
| `max_attempts` | integer | 최대 재시도 횟수 |
| `error_code` | string, nullable | 오류 코드 |
| `error_message` | text, nullable | 사용자 표시용 오류 |
| `payload` | JSON | 작업 입력 |
| `result` | JSON, nullable | 작업 결과 |
| `created_at` | datetime | 생성 시각 |
| `started_at` | datetime, nullable | 시작 시각 |
| `finished_at` | datetime, nullable | 종료 시각 |

Job 유형:

```text
album_plan
lyrics_generate
track_generate
album_generate
audio_download
album_archive
cover_generate
image_compose
video_render
```

Job 상태:

```text
pending
running
succeeded
failed
cancel_requested
cancelled
```

### 6.5 Asset

생성되거나 업로드된 파일을 통합 관리한다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | UUID | Asset ID |
| `album_id` | UUID, nullable | 연결 앨범 |
| `track_id` | UUID, nullable | 연결 트랙 |
| `generation_id` | UUID, nullable | 연결 생성 후보 |
| `type` | enum | audio, lyrics, cover, video, archive |
| `storage_key` | text | 실제 저장 위치 |
| `original_name` | string | 다운로드 파일명 |
| `content_type` | string | MIME type |
| `size_bytes` | integer | 파일 크기 |
| `checksum` | string, nullable | 무결성 확인 값 |
| `created_at` | datetime | 생성 시각 |

## 7. 관계

```text
Album 1 --- N Track
Track 1 --- N Generation
Album 1 --- N Job
Track 1 --- N Job
Album/Track/Generation 1 --- N Asset
Track 1 --- 0..1 Selected Generation
Album 1 --- 0..1 Selected Cover Asset
```

## 8. API 설계

Base path는 `/api/v1`을 사용한다.

### 8.1 시스템 API

#### `GET /api/v1/system/health`

애플리케이션과 DB 상태를 반환한다.

#### `GET /api/v1/system/suno-status`

Suno 연결과 크레딧 상태를 반환한다.

응답 예시:

```json
{
  "connected": true,
  "credits_left": 2130,
  "monthly_limit": 2500,
  "monthly_usage": 370
}
```

인증 쿠키, 세션 ID와 JWT는 절대 반환하지 않는다.

### 8.2 앨범 API

#### `POST /api/v1/albums`

앨범 초안을 생성한다.

```json
{
  "title": "비 오는 날의 기억",
  "artist_name": "Playlist Studio",
  "genre": "K-Pop",
  "vocal_style": "soft female vocal",
  "tempo": "90-110 BPM",
  "lyrics_language": "ko",
  "mood": "calm and nostalgic",
  "instruments": ["synthesizer", "piano", "soft drums"],
  "keywords": "비, 오래된 친구, 카페, 추억",
  "additional_instructions": "후렴이 쉽게 기억되도록 구성",
  "track_count": 10
}
```

#### `GET /api/v1/albums`

앨범 목록을 조회한다.

Query:

```text
status
genre
search
limit
cursor
```

#### `GET /api/v1/albums/{album_id}`

앨범 상세와 트랙 요약을 조회한다.

#### `PATCH /api/v1/albums/{album_id}`

앨범 설정을 수정한다.

#### `DELETE /api/v1/albums/{album_id}`

앨범을 soft delete 또는 archive 처리한다.

#### `POST /api/v1/albums/{album_id}/plan`

음악 스타일 설정을 Gemini에 전달해 공통 영문 스타일과 트랙 기획안을 생성한다.

Gemini 응답은 구조화된 JSON으로 제한한다.

```json
{
  "album_summary": "비 오는 날과 오래된 우정을 다룬 레트로 신스팝 앨범",
  "common_style_prompt": "90s retro synthpop, soft female vocals, warm analog synthesizers...",
  "tracks": [
    {
      "sequence": 1,
      "title": "비 오는 창가",
      "concept": "오래된 친구를 떠올리는 화자",
      "lyrics": "[Verse 1]\n...",
      "style_prompt": "Nostalgic 90s Korean synthpop, soft female vocal...",
      "image_prompt": "A nostalgic rainy cafe window at night..."
    }
  ]
}
```

생성 규칙:

1. 가사는 사용자가 선택한 언어로 생성한다.
2. `style_prompt`는 Suno Custom Mode에 적합한 영어로 생성한다.
3. 영문 스타일에는 장르, 보컬, 템포, 악기, 분위기와 프로덕션 특성을 포함한다.
4. Gemini 응답은 Pydantic schema로 검증한 뒤 저장한다.

응답:

```json
{
  "job_id": "uuid",
  "status": "pending"
}
```

#### `POST /api/v1/albums/{album_id}/generate`

생성 가능한 전체 트랙의 음악 생성을 예약한다.

요청:

```json
{
  "track_ids": ["uuid-1", "uuid-2"],
  "max_concurrency": 1,
  "download_audio": true
}
```

#### `POST /api/v1/albums/{album_id}/archive`

선택된 MP3, 가사, 커버와 메타데이터를 ZIP으로 생성한다.

### 8.3 트랙 API

#### `GET /api/v1/albums/{album_id}/tracks`

앨범의 트랙 목록을 순서대로 반환한다.

#### `POST /api/v1/albums/{album_id}/tracks`

트랙을 수동 추가한다.

#### `PATCH /api/v1/tracks/{track_id}`

제목, 순서, 콘셉트, 가사, 스타일 등을 수정한다.

#### `DELETE /api/v1/tracks/{track_id}`

트랙을 삭제한다. 이미 생성된 자산 삭제 정책은 별도로 적용한다.

#### `POST /api/v1/tracks/reorder`

트랙 순서를 일괄 변경한다.

```json
{
  "album_id": "uuid",
  "track_ids": ["track-3", "track-1", "track-2"]
}
```

### 8.4 가사 API

#### `POST /api/v1/tracks/{track_id}/lyrics/generate`

트랙 콘셉트와 앨범 설정을 Gemini에 전달해 가사와 Suno용 영문 스타일을 함께 생성한다.

#### `POST /api/v1/tracks/{track_id}/lyrics/regenerate`

현재 가사와 수정 지시를 이용해 재생성한다.

```json
{
  "instruction": "후렴을 짧고 반복적으로 수정",
  "regenerate_style": false
}
```

#### `PUT /api/v1/tracks/{track_id}/lyrics`

사용자가 편집한 가사를 저장한다.

```json
{
  "lyrics": "[Verse 1]\n..."
}
```

#### `GET /api/v1/tracks/{track_id}/lyrics/download`

UTF-8 텍스트 파일로 다운로드한다.

#### `PUT /api/v1/tracks/{track_id}/style`

사용자가 수정한 Suno용 영문 스타일을 저장한다.

```json
{
  "style_prompt": "Nostalgic 90s Korean synthpop, soft female vocal..."
}
```

### 8.5 음악 생성 API

#### `POST /api/v1/tracks/{track_id}/generate`

트랙 한 곡의 Suno 생성을 예약한다.

```json
{
  "mode": "custom",
  "model": "chirp-fenix",
  "download_audio": true,
  "idempotency_key": "client-generated-key"
}
```

`mode`:

- `custom`: 기본 모드. Gemini가 생성하고 사용자가 확정한 가사와 영문 스타일 사용
- `description`: 사용자가 명시적으로 선택한 경우에만 Suno 설명 모드 사용

노래 만들기 화면은 기본적으로 다음 값으로 Suno Custom Mode를 호출한다.

```json
{
  "prompt": "트랙에 저장된 전체 가사",
  "title": "트랙 제목",
  "tags": "트랙에 저장된 영문 style_prompt",
  "negative_tags": "",
  "mv": "chirp-fenix"
}
```

#### `POST /api/v1/tracks/{track_id}/regenerate`

새 Generation Job을 생성한다. 기존 후보는 유지한다.

#### `GET /api/v1/tracks/{track_id}/generations`

해당 트랙의 모든 생성 후보를 반환한다.

#### `POST /api/v1/tracks/{track_id}/generations/{generation_id}/select`

최종 사용할 후보를 선택한다. 한 트랙에는 하나의 후보만 선택할 수 있다.

#### `DELETE /api/v1/generations/{generation_id}`

생성 후보를 삭제한다.

### 8.6 Job API

#### `GET /api/v1/jobs/{job_id}`

작업 상태, 진행률과 오류를 반환한다.

#### `GET /api/v1/jobs`

리소스 또는 상태별 작업 목록을 조회한다.

#### `POST /api/v1/jobs/{job_id}/cancel`

취소 가능한 작업에 취소 요청을 기록한다.

#### `POST /api/v1/jobs/{job_id}/retry`

실패한 작업을 재시도한다.

### 8.7 실시간 상태 API

MVP에서는 3~10초 간격의 REST polling을 사용할 수 있다.

확장 시 SSE를 권장한다.

#### `GET /api/v1/events?album_id={album_id}`

이벤트 예시:

```text
job.updated
track.updated
generation.updated
asset.ready
```

### 8.8 Asset API

#### `GET /api/v1/assets/{asset_id}/download`

서버 저장 파일을 다운로드한다.

#### `GET /api/v1/generations/{generation_id}/audio`

오디오를 스트리밍한다. 로컬 파일이 있으면 Range 요청을 지원한다.

#### `GET /api/v1/albums/{album_id}/archive/download`

완료된 ZIP 파일을 다운로드한다.

### 8.9 이미지 API

#### `POST /api/v1/albums/{album_id}/covers/generate`

앨범 설정과 선택 트랙을 Gemini에 전달해 이미지 프롬프트를 만들고, Gemini 이미지 모델로 16:9 후보 이미지를 생성한다.

```json
{
  "track_id": "uuid",
  "instruction": "따뜻한 수채화 느낌, 인물의 얼굴은 강조하지 않기",
  "aspect_ratio": "16:9",
  "candidate_count": 4
}
```

#### `POST /api/v1/albums/{album_id}/covers/upload`

사용자 이미지를 업로드한다.

#### `GET /api/v1/albums/{album_id}/covers`

커버 후보 목록을 조회한다.

#### `POST /api/v1/albums/{album_id}/covers/{asset_id}/select`

대표 커버를 선택한다.

#### `POST /api/v1/albums/{album_id}/images/{asset_id}/compose`

원본 이미지를 영상 배경용으로 꾸미고 편집 설정을 저장한다.

```json
{
  "crop": "fill",
  "brightness": 0,
  "contrast": 0,
  "saturation": 0,
  "blur": 0,
  "overlay_color": "#21000f",
  "overlay_opacity": 0.15,
  "title": "PLAY LIST",
  "artist_name": "Artist",
  "title_position": "bottom-left",
  "show_visualizer": true
}
```

### 8.10 플레이 루프 영상 API

#### `POST /api/v1/albums/{album_id}/videos/render`

선택 이미지와 음원을 이용해 플레이 루프 영상을 렌더링한다.

```json
{
  "mode": "static_loop",
  "track_id": "uuid",
  "image_asset_id": "uuid",
  "aspect_ratio": "16:9",
  "resolution": "1920x1080",
  "show_title": true,
  "show_lyrics": false,
  "show_visualizer": true,
  "visualizer_style": "bars",
  "loop_motion": "slow_zoom",
  "fade_in_seconds": 1.0,
  "fade_out_seconds": 1.0
}
```

지원 모드:

| 모드 | 처리 방식 |
|---|---|
| `static_loop` | FFmpeg로 이미지, 패닝/줌, 시각화와 음원을 결합 |
| `animated_image` | Gemini/Veo로 짧은 영상을 만든 뒤 음원 길이에 맞게 반복 |
| `album_mix` | 여러 선택 트랙을 이어 붙여 앨범 통합 영상 생성 |

영상 렌더링은 별도 Job으로 처리한다. MVP 기본 모드는 `static_loop`다.

## 9. 핵심 처리 흐름

### 9.1 앨범 기획

```text
1. Frontend -> POST /albums
2. Frontend -> POST /albums/{id}/plan
3. Backend -> Job 생성
4. Worker -> 입력값을 Gemini 프롬프트로 변환
5. Gemini -> 앨범 요약과 공통 영문 스타일 생성
6. Gemini -> 트랙별 제목, 콘셉트, 가사와 영문 스타일 생성
7. Backend -> JSON schema 검증
8. Album과 Track DB 저장
9. Job succeeded
10. Frontend polling/SSE로 결과 갱신
```

### 9.2 음악 생성

```text
1. Frontend -> POST /tracks/{id}/generate
2. Backend -> 입력 검증 및 Job 생성
3. Worker -> 저장된 가사와 영문 style_prompt 조회
4. Worker -> Suno Custom Mode 호출
5. Suno request ID와 clip ID 저장
6. Worker -> feed polling
7. 후보별 상태와 메타데이터 갱신
8. audio_url 생성 시 MP3 다운로드
9. 가사 TXT와 Asset 생성
10. 모든 후보 완료 시 Job succeeded
```

### 9.3 앨범 전체 생성

```text
1. Album Job이 대상 트랙 목록을 확정
2. 트랙별 하위 Job 생성
3. 설정된 동시 실행 수만큼 순차 처리
4. 실패 트랙은 제한 횟수만큼 재시도
5. 일부 성공 시 album=partially_complete
6. 전체 성공 시 album=complete
```

기본 동시 실행 수는 `1`을 권장한다. Suno 크레딧과 rate limit을 고려해 운영 설정으로 제한한다.

### 9.4 후보 선택

```text
1. 트랙 후보 오디오 재생
2. 사용자가 후보 선택
3. 트랜잭션에서 기존 is_selected 해제
4. 새 후보 is_selected=true
5. Track.selected_generation_id 갱신
```

### 9.5 플레이 루프 영상 생성

```text
1. 사용자가 최종 음원 후보 선택
2. Frontend -> 이미지 생성 요청
3. Gemini Text -> 이미지용 영문 프롬프트 생성 또는 보정
4. Gemini Image -> 16:9 이미지 후보 생성
5. 사용자가 이미지 선택 또는 직접 업로드
6. 사용자가 크롭, 색상, 텍스트와 시각화 옵션 설정
7. Frontend -> 영상 렌더링 요청
8. Worker -> 합성 이미지와 렌더 설정 확정
9. FFmpeg/Veo -> 영상 생성
10. 음원 결합, 길이 검증, MP4 Asset 저장
```

## 10. Suno 연동 규칙

### 10.1 기존 어댑터 매핑

| 애플리케이션 기능 | 기존 API |
|---|---|
| 크레딧 확인 | `GET /get_credits` |
| Custom 음악 생성 | `POST /generate` |
| Description 음악 생성 | `POST /generate/description-mode` |
| 생성 상태 확인 | `GET /feed/{clip_ids}` |
| 가사 생성 | `POST /generate/lyrics/` |
| 가사 결과 확인 | `GET /lyrics/{lyrics_id}` |

통합 Backend의 `app/integrations/suno_client.py`에서 Suno 연동 함수를 직접 호출한다. 내부 Suno 기능을 다시 HTTP로 호출하지 않는다.

```text
API Router
  -> Application Service
    -> Suno Integration
      -> Suno Web API
```

기존 `/generate`, `/feed`, `/get_credits` 라우트는 호환성을 위해 유지할 수 있다. 신규 앨범 서비스는 이 HTTP 라우트를 경유하지 않고 동일 프로세스의 integration 함수를 사용한다.

### 10.2 인증정보

1. `SESSION_ID`, `COOKIE`는 `.env` 또는 Secret Store에서만 관리한다.
2. `.env`와 `.auth/`는 Git에 포함하지 않는다.
3. 쿠키 전체 또는 JWT를 로그에 기록하지 않는다.
4. 오류 응답에 upstream 요청 헤더를 포함하지 않는다.
5. 인증 실패 시 운영자용 상태 코드와 사용자용 메시지를 분리한다.

### 10.3 폴링

권장 값:

```text
초기 간격: 5초
일반 간격: 10초
최대 생성 대기: 10분
일시적 오류 재시도: 3회
```

완료 판정:

- `audio_url`이 존재하고 상태가 실패 상태가 아님
- 실패 상태: `error`, `failed`

### 10.4 Gemini 연동 규칙

Gemini Text는 다음 용도로 사용한다.

1. 앨범 공통 음악 스타일 생성
2. 트랙 제목과 콘셉트 생성
3. 선택 언어의 가사 생성
4. Suno Custom Mode용 영문 스타일 생성
5. 이미지 생성용 영문 프롬프트 생성

응답은 JSON schema를 강제하고, 파싱 또는 검증 실패 시 최대 2회 보정 요청한다.

Gemini Image 프롬프트에는 앨범 제목, 트랙 콘셉트, 가사 요약, 영문 음악 스타일, 사용자 요구사항과 16:9 구도 지시를 포함할 수 있다.

저장소의 기존 서비스를 검토해 통합 Backend용 순수 서비스로 재사용한다.

```text
app/services/gemini_client.py
app/services/gemini_image_client.py
app/services/ffmpeg_render.py
app/services/veo_video_client.py
```

GUI Worker나 Qt 객체에 직접 의존하지 않고 네트워크 호출과 렌더링 핵심 함수만 분리하거나 래핑한다.

## 11. 파일 저장 정책

### 11.1 로컬 저장 구조

```text
storage/
  albums/
    {album_id}/
      album.json
      cover/
        selected.png
      tracks/
        01-{track_id}/
          lyrics.txt
          {generation_id}.mp3
          metadata.json
      video/
        album.mp4
      archive/
        album.zip
```

사용자가 입력한 제목을 실제 디렉터리 키로 사용하지 않는다. 경로에는 UUID를 사용하고 다운로드 파일명에서만 정제된 제목을 사용한다.

### 11.2 ZIP 구조

```text
앨범제목/
  cover.png
  01-곡제목.mp3
  01-곡제목.txt
  02-곡제목.mp3
  02-곡제목.txt
  album.json
```

### 11.3 다운로드 파일명

파일명에서 다음 문자를 제거하거나 치환한다.

```text
< > : " / \ | ? *
```

## 12. 오류 모델

공통 오류 응답:

```json
{
  "error": {
    "code": "SUNO_AUTH_FAILED",
    "message": "Suno 인증정보를 갱신한 후 다시 시도해 주세요.",
    "retryable": false,
    "details": null,
    "request_id": "uuid"
  }
}
```

주요 오류 코드:

| 코드 | HTTP | 설명 |
|---|---:|---|
| `VALIDATION_ERROR` | 422 | 입력값 오류 |
| `ALBUM_NOT_FOUND` | 404 | 앨범 없음 |
| `TRACK_NOT_FOUND` | 404 | 트랙 없음 |
| `JOB_NOT_FOUND` | 404 | 작업 없음 |
| `INVALID_TRACK_STATE` | 409 | 현재 상태에서 작업 불가 |
| `IDEMPOTENCY_CONFLICT` | 409 | 중복 생성 요청 |
| `SUNO_AUTH_FAILED` | 503 | Suno 인증 실패 |
| `SUNO_RATE_LIMITED` | 429 | Suno 요청 제한 |
| `SUNO_GENERATION_FAILED` | 502 | Suno 생성 실패 |
| `INSUFFICIENT_CREDITS` | 409 | 크레딧 부족 |
| `ASSET_DOWNLOAD_FAILED` | 502 | 산출물 다운로드 실패 |
| `JOB_TIMEOUT` | 504 | 작업 시간 초과 |

## 13. 멱등성과 동시성

### 13.1 멱등성

음악 생성 API는 `Idempotency-Key` 헤더 또는 요청의 `idempotency_key`를 지원한다.

동일 사용자, 동일 리소스, 동일 키의 성공 또는 실행 중 요청이 존재하면 기존 Job을 반환한다.

### 13.2 동시성

1. 한 트랙에 실행 중인 음악 생성 Job은 기본 1개만 허용한다.
2. 후보 선택은 DB 트랜잭션으로 처리한다.
3. 앨범 트랙 순서 변경은 전체 목록을 한 번에 검증한다.
4. Worker가 Job을 가져갈 때 row lock 또는 원자적 상태 변경을 사용한다.

## 14. 보안

1. CORS origin은 Frontend 주소로 제한한다.
2. 운영 환경에서 `allow_origins=["*"]`와 credential 허용을 함께 사용하지 않는다.
3. 업로드 파일의 MIME type, 확장자와 크기를 검증한다.
4. 다운로드 경로는 사용자 입력을 직접 결합하지 않는다.
5. 외부 URL 다운로드 시 허용 호스트를 제한해 SSRF를 방지한다.
6. 로그에서 쿠키, Authorization, JWT, 세션 ID를 마스킹한다.
7. 생성 요청에 사용자별 rate limit을 적용한다.
8. DB와 저장 파일은 사용자 또는 프로젝트 소유권을 검사한 후 반환한다.

## 15. 관측성과 로깅

모든 요청과 Job에 `request_id` 또는 `correlation_id`를 부여한다.

구조화 로그 필드:

```text
request_id
job_id
album_id
track_id
generation_id
clip_id
operation
duration_ms
status
error_code
```

기록하면 안 되는 값:

```text
COOKIE
SESSION_ID
Authorization
JWT
가사 전체 원문
```

권장 지표:

```text
generation_job_total
generation_job_success_total
generation_job_failure_total
generation_duration_seconds
suno_request_duration_seconds
active_jobs
credits_left
asset_download_failure_total
```

## 16. 데이터베이스 및 인프라

### 16.1 로컬/MVP

```text
Database: SQLite
Worker: FastAPI lifespan에서 시작하는 단일 background worker
Storage: 로컬 파일 시스템
Status update: REST polling
Backend process: backend 단일 Uvicorn 프로세스
Frontend process: React 개발 서버 또는 정적 배포
```

### 16.2 운영 확장

```text
Database: PostgreSQL
Queue: Redis + RQ/Celery/Dramatiq
Storage: S3 호환 Object Storage
Status update: SSE
Reverse proxy: Nginx 또는 managed gateway
```

SQLite 기반 단일 Worker에서는 여러 Uvicorn worker를 사용하지 않는다. Backend는 Suno 연동과 앨범 API를 포함한 하나의 프로세스로 실행한다. 다중 프로세스 배포 전에는 Job queue를 외부 시스템으로 분리해야 한다.

### 16.3 로컬 실행

```powershell
# Terminal 1: 통합 FastAPI Backend
cd backend
.\.venv\Scripts\python.exe .\start_suno_server.py

# Terminal 2: React Frontend
cd frontend
npm run dev
```

## 17. API 응답 규칙

단일 리소스:

```json
{
  "data": {
    "id": "uuid"
  }
}
```

목록:

```json
{
  "data": [],
  "pagination": {
    "next_cursor": null,
    "has_more": false
  }
}
```

비동기 작업 접수:

```json
{
  "data": {
    "job_id": "uuid",
    "status": "pending"
  }
}
```

날짜는 UTC ISO 8601 형식을 사용한다.

## 18. 테스트 전략

### 18.1 단위 테스트

1. 앨범과 트랙 상태 전이
2. 트랙 순서 변경
3. 후보 선택 트랜잭션
4. 파일명 정제
5. Suno 응답 정규화
6. 재시도 가능 오류 판정

### 18.2 통합 테스트

1. Album CRUD
2. 기획 Job 생성과 완료
3. 음악 생성 Job과 feed polling
4. MP3와 가사 Asset 생성
5. ZIP 생성과 다운로드
6. 인증정보 마스킹

### 18.3 Suno 연동 테스트

실제 크레딧을 사용하는 테스트는 기본 테스트 스위트에서 제외한다.

```text
mock: CI와 일반 개발
smoke: 명시적으로 실행할 때만 실제 Suno 호출
```

실제 생성 스모크 테스트에는 비용 발생 여부를 명확히 표시한다.

## 19. 구현 단계

### Phase 1: 기반

1. `backend` 내부에 `app` 패키지 구조 생성
2. SQLite와 ORM 설정
3. Album, Track, Job, Generation, Asset 모델
4. 공통 오류와 응답 형식

### Phase 2: 앨범과 가사

1. Album/Track CRUD
2. Gemini Text 연동
3. 앨범 공통 스타일과 플레이리스트 기획
4. 트랙별 가사와 Suno용 영문 스타일 생성
5. 가사와 스타일 수정 및 다운로드

### Phase 3: 음악 생성

1. Suno client 추상화
2. 생성 Job
3. feed polling
4. 후보 저장과 선택
5. MP3 다운로드

### Phase 4: 앨범 산출물

1. 앨범 ZIP
2. Gemini 이미지 프롬프트 생성
3. 이미지 생성과 업로드
4. 이미지 꾸미기 설정 및 미리보기
5. FFmpeg 플레이 루프 영상
6. 앨범 히스토리

### Phase 5: 영상과 운영

1. Gemini/Veo 이미지 애니메이션
2. 앨범 통합 영상
3. 외부 Queue
4. PostgreSQL
5. Object Storage
6. 사용자 인증과 사용량 정책

## 20. MVP 완료 조건

다음 시나리오가 처음부터 끝까지 동작하면 MVP 완료로 본다.

1. 사용자가 앨범 스타일과 트랙 수를 입력한다.
2. Gemini가 트랙별 제목, 가사와 Suno용 영문 스타일을 생성한다.
3. 사용자가 트랙별 가사와 영문 스타일을 검토하고 수정한다.
4. Backend가 확정 가사와 스타일로 Suno Custom Mode 생성을 요청한다.
5. Backend가 생성 상태를 지속적으로 갱신한다.
6. 사용자가 생성 후보를 재생하고 하나를 선택한다.
7. Gemini가 선택 음원에 어울리는 이미지 후보를 생성한다.
8. 사용자가 이미지를 선택하고 꾸미기 옵션을 설정한다.
9. Backend가 음원 길이의 플레이 루프 영상을 생성한다.
10. MP3, 가사, 이미지, MP4와 앨범 ZIP을 다운로드한다.
11. 서버 재시작 후에도 앨범, 트랙과 작업 이력이 유지된다.
