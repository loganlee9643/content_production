# 숏폼 제작 사이트 설치 및 실행

아래는 새 Windows 컴퓨터에서 저장소를 내려받은 뒤 처음 실행하는 절차입니다. Python, Node.js와 npm, 실행 중인 PostgreSQL 서버가 필요합니다. PostgreSQL은 로컬 서버나 접속 가능한 원격 서버를 사용할 수 있습니다.

## 1. 백엔드 설정

저장소 루트에서 PowerShell을 열고 실행합니다.

```powershell
Set-Location apps\backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path -LiteralPath config.yaml)) {
    Copy-Item config.example.yaml config.yaml
}
```

`apps/backend/config.yaml`을 열고 사용할 기능에 맞게 설정합니다.

| 설정 | 입력할 값 |
| --- | --- |
| `database` | PostgreSQL 호스트, 포트, 앱 데이터베이스 이름과 계정 정보 |
| `instagram` | 레퍼런스 수집용 Instagram 계정의 숫자 ID와 액세스 토큰 |
| `video_analysis` | 영상 분석과 스토리보드 생성에 사용할 모델 및 API 키 |
| `video_production` | 클립 생성에 사용할 모델 및 API 키 |
| `character_image` | 캐릭터 이미지 생성에 사용할 모델 및 API 키 |

최종 영상 조립에는 FFmpeg와 FFprobe가 필요합니다. 설치 후 두 명령을 PATH에서 사용할 수 있게 하거나, `video_production.ffmpeg`와 `video_production.ffprobe`에 각 실행 파일의 경로를 입력합니다.

## 2. 프런트엔드 설치

위 과정에 이어 `apps/backend`에서 실행합니다.

```powershell
Set-Location ..\web
npm ci
```

## 3. 사이트 실행

[숏폼 실행 배치](../scripts/start_shortform_backend_and_frontend.bat)를 더블클릭합니다. 백엔드, 프런트엔드, Instagram 수집 워커가 별도 창에서 실행됩니다. 워커는 백엔드가 준비되면 시작합니다.

- 숏폼 사이트 `http://localhost:3000/references`
- 백엔드 `http://127.0.0.1:8001`

다음부터는 실행 배치만 열면 됩니다. 배치가 백엔드 가상환경의 Python을 직접 사용합니다.
