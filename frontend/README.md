# Tubemaster Playlist Frontend

React + TypeScript 기반 앨범 제작 Frontend다. 통합 FastAPI Backend의 `/api/v1` API를 사용한다.

## 실행

Backend를 먼저 실행한다.

```powershell
cd backend
.\.venv\Scripts\python.exe .\start_suno_server.py
```

다른 터미널에서 Frontend를 실행한다.

```powershell
cd frontend
npm install
npm run dev
```

브라우저:

```text
http://127.0.0.1:5173
```

5173 포트가 이미 사용 중이면 Vite는 다른 포트로 자동 변경하지 않고 오류를 표시한다.
기존 Frontend 개발 서버를 종료한 뒤 다시 실행한다.

## 환경 변수

개발 시 Frontend는 `/api/v1` 경로로 Backend(`8000`)에 프록시됩니다.
`VITE_API_BASE_URL`을 `.env`에 넣지 마세요. 넣으면 CORS로 영상 합성이 실패할 수 있습니다.

Backend가 다른 머신에 있을 때만 프록시 대상을 지정합니다.

```dotenv
VITE_API_PROXY_TARGET=http://backend-host:8000
```

## 주요 화면

- `/albums`: 앨범 목록
- `/albums/new`: 새 앨범 생성
- `/albums/:albumId/plan`: Gemini 앨범 기획과 가사 편집
- `/albums/:albumId/tracks`: Suno 음원 생성과 후보 선택
- `/albums/:albumId/video`: 이미지 생성, 편집, 루프 영상 렌더링
- `/albums/:albumId/export`: 결과 확인과 ZIP 내보내기

## 빌드

```powershell
npm run build
```
