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

## PostgreSQL 데이터를 다른 서버로 옮기기

출발지와 대상에서 숏폼 백엔드와 수집 워커를 멈춥니다. 특히 진행 중인 영상 생성은 완료한 뒤 중지해야 합니다. 아래 명령은 저장소 루트에서 실행하며, `shortform`은 데이터베이스와 계정 이름 예시입니다.

1. 출발지에서 PostgreSQL 백업 파일을 만듭니다.

   ```powershell
   python scripts/migrate_shortform_postgres.py backup shortform.dump --host 127.0.0.1 --database shortform --user shortform
   ```

2. 대상 PostgreSQL 서버에 앱 계정을 만들고, 그 계정이 소유하는 **빈 데이터베이스**를 준비합니다. 백업 파일을 옮겨 아래 명령을 실행합니다. 대상 서버에 네트워크로 접속할 수 있다면 출발지에서 바로 실행해도 됩니다.

   ```powershell
   python scripts/migrate_shortform_postgres.py restore shortform.dump --host DESTINATION_HOST --database shortform --user shortform
   ```

3. 영상도 이전하려면 출발지의 `video_production.storage_dir` 폴더 전체를 대상 백엔드의 저장 폴더에 복사합니다. 기본값은 `apps/backend/.production-media`입니다. UUID로 된 MP4 파일 이름을 유지합니다.
4. 대상 백엔드의 `apps/backend/config.yaml`에 새 PostgreSQL 접속 정보와 동영상 저장 경로를 입력한 뒤 서비스를 실행합니다. 기존 레퍼런스와 프로젝트, 영상 재생이 정상인지 확인합니다.

PostgreSQL 클라이언트 도구가 PATH에 없으면 두 명령에 `--bin-dir "C:\Program Files\PostgreSQL\bin"`을 추가합니다. `pg_dump`는 출발지 서버와 같은 메이저 버전 이상을 사용하고, 대상 서버는 출발지와 같거나 더 최신 버전을 사용합니다. 암호는 PostgreSQL 도구가 요청할 때 입력합니다. 복원은 한 트랜잭션으로 진행되며, 기존 테이블을 삭제하거나 서버의 계정과 권한을 복제하지 않습니다.
