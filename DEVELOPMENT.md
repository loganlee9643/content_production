# 숏폼 제작 스튜디오 개발 실행

## 로컬 네트워크에서 접속

같은 네트워크의 다른 기기에서 Next.js 개발 화면을 열 때는 `apps/web/.env.local`의 `NEXT_DEV_ALLOWED_ORIGINS`에 웹 서버의 LAN IP 또는 호스트 이름을 넣는다. 여러 주소는 쉼표로 구분한다. 예시는 `NEXT_DEV_ALLOWED_ORIGINS=192.168.219.128`이다. 개발 서버를 재시작한 뒤 다른 기기에서 `http://192.168.219.128:3000`에 접속한다.

터미널의 개발 서버가 루프백 주소에서만 수신한다면 웹 폴더에서 `npm run dev -- --hostname 0.0.0.0`으로 실행한다. 웹과 FastAPI가 같은 서버에서 실행되는 경우 웹의 API 프록시는 기존 로컬 주소를 사용한다.

## 영상 제작

재기획 설정의 캐릭터 탭에서 등록 사진을 선택한다. 새 캐릭터 탭에서는 역할·나이로 이미지를 생성하거나, 4 MiB 이하 JPG/PNG 파일을 선택해 등록 버튼으로 추가한다. 새 캐릭터 탭에서 목록을 관리할 수 있고, 삭제 전에 적용한 프로젝트는 계속 기존 이미지를 사용한다. 설정 저장 또는 재기획 생성으로 선택한 캐릭터를 프로젝트에 적용한다. 기존 이미지 없이 만들었거나 이전 이미지로 만든 클립은 새 최종 조립 전에 다시 생성한다.

캐릭터 생성 모델은 `character_image.model`에서 지정한다. 기본값은 `gemini-3.1-flash-image`다. `character_image.api_key`가 비어 있고 영상 공급사가 Gemini Omni이면 `video_production.api_key`를 사용한다.

Gemini Omni에서는 `이전 장면 이어서 생성`을 선택할 수 있다. 앞 장면 끝부분에서 프레임을 추출해 기준 이미지와 함께 다음 장면에 전달한다. 장소나 구도가 바뀌는 장면에는 `이 장면은 기준 이미지만 사용`을 선택한다. 이미 생성한 특정 장면은 `이전 장면 참조로 생성` 버튼으로 다시 만들 수 있다. 일괄 이어 생성의 진행 상태는 DB에 기록되며, 영상 제작 화면의 상태 조회가 다음 장면 제출을 이어간다.

스토리보드를 생성하거나 편집 후 저장하면 영상 제작 영역에서 장면별 클립을 만들 수 있다. `apps/backend/config.yaml`의 `video_production.model`에 영상 생성 모델 ID를, `video_production.api_key`에 해당 공급사의 키를 입력한다. 지원 모델은 Gemini Developer API의 `gemini-omni-1.2-flash`와 BytePlus ModelArk의 `dreamina-seedance-2-0-260128`다. Gemini Omni는 유료 결제가 활성화된 프로젝트의 키가 필요하다. 영상 분석과 대본 생성은 별도 `video_analysis` 설정을 사용한다.

```yaml
video_production:
  model: gemini-omni-1.2-flash
  api_key: ""
  storage_dir: .production-media
  ffmpeg: ffmpeg
  ffprobe: ffprobe
```

FFmpeg와 ffprobe를 PATH에 설치하거나 각 설정에 실행 파일의 절대 경로를 입력한다. 클립과 최종 MP4는 지정한 저장 경로에 남는다. 운영 환경에서는 DB와 영상 저장 경로의 데이터를 함께 보존한다. 장면별로 대사와 연출을 선택한 모델에 전달하며, 결과에 포함된 음성을 최종 영상에 사용한다. 공급사를 전환한 뒤 이전 작업을 조회하려면 `video_production.seedance_api_key` 또는 `video_production.gemini_api_key`에 이전 공급사의 키를 별도로 둘 수 있다.

## 레퍼런스 영상 분석

`apps/backend/config.yaml`에 아래 설정을 추가하고 실제 Gemini API 키를 입력한다. 분석 버튼을 누르면 사용 가능한 원본 MP4의 화면과 음성을 Gemini에 보내 분석 결과를 프로젝트에 저장한다. 수집한 영상 URL이 없거나 만료된 경우 사용 권한이 있는 MP4를 선택할 수 있다. 현재 허용 파일 크기는 8 MiB다.

```yaml
video_analysis:
  api_key: ""
  model: gemini-3.8-flash
```

## 정기 및 수동 레퍼런스 수집

레퍼런스 화면은 주제별로 마지막 수집 시각과 다음 자동 수집 예정 시각을 한국 시간으로 표시한다. 수동 수집 버튼에서 목록의 표시 기간과 별개로 최근 6·12·24시간의 최신 릴스 또는 최근 7·30일의 Meta 인기 릴스를 선택할 수 있다. 인기 릴스 옵션은 Meta 인기 목록에서 해당 기간에 게시된 릴스만 추가한다.

릴스 ID가 같으면 기존 항목의 제공 데이터와 최근 수집 시각을 갱신한다. 좋아요와 댓글 수는 Meta가 반환할 때 저장하며, 누락값은 기존 값을 유지한다. 해시태그 검색 경로에는 조회수 필드가 제공되지 않는다.

FastAPI를 실행한 다음 별도 터미널에서 `apps/backend`로 이동해 같은 가상환경으로 아래 명령을 실행한다. 운영에서는 API와 수집 워커를 각각 별도 서비스나 컨테이너로 실행한다. 수집 워커의 진입점과 DB 구조는 운영체제에 따라 달라지지 않는다.

```text
python -m collection
```

`/references`는 PostgreSQL에 쌓인 릴스를 검색한다. 기본은 최근 7일이고 반응순은 제공된 좋아요와 댓글 수 합을 기준으로 한다. 첫 수집 전에는 결과가 비어 있다. 주제 옆 수동 수집 버튼을 누르면 FastAPI는 수집 요청만 저장하고, 수동 수집도 워커가 실행한다. 워커는 대기 중인 수동 요청을 자동 수집보다 먼저 처리하고 호출 간격을 기다리지 않지만, Meta 호출 한도로 제한된 동안에는 제한이 풀릴 때까지 기다린다. 워커는 루프마다 PostgreSQL에 확인 시각을 남기며, 마지막 확인 시각이 `WORKER_STALE_AFTER`(워커 대기와 한 태그 최악 수집 시간 합의 두 배)보다 오래되면 화면에 "수집 워커 중지됨"이 표시된다. Meta 토큰이 만료되면 "Meta 토큰 만료"가 표시된다.

워커는 `config.yaml`의 최상위 `collection.interval_hours` 값을 태그별 수집 간격으로 사용한다. 기본값은 6시간이며 1~168 사이의 정수를 설정할 수 있다. 태그별 인기 게시물을 하루 한 번 Meta 인기 목록에서 수집하며, 호출 간격은 하루 전체 실행 수(태그별 최신 수집과 인기 수집)로 나눈다. 최신 수집은 태그마다 최대 `MAX_MEDIA_PAGES`(현재 3)페이지를 받고, 한 페이지의 게시물이 모두 그 태그의 직전 성공 시각보다 오래됐으면 다음 페이지를 받지 않는다. 조회한 해시태그 ID는 저장해 다음 실행부터 재사용하고, 그 태그 수집이 호출 한도 이외의 오류로 실패하면 지운다. 수정 후 워커를 재시작하면 다음 예약 계산부터 새 간격을 적용한다. 이미 저장된 예약 시각은 유지한다. 수동 수집은 자동 수집 예약 간격을 기다리지 않으며, 한 번에 한 건 실행과 Meta 호출 제한은 공유한다. 호출 제한과 최근 성공 시각은 PostgreSQL에 저장한다. 수집 워커용 Meta 인증 정보는 기존 `config.yaml`의 `instagram` 설정을 사용한다.

첫 단계는 개발자 로컬 환경에서 공개 릴스 확인, 레퍼런스 저장·검색, 레퍼런스로 프로젝트 만들기를 검증하는 것이다. 현재 DB는 단일 사용자 개발 환경을 가정한다. 공유 호스팅 전 사용자 인증 및 작업공간 권한을 적용한다.

## 실행

Windows에 설치된 PostgreSQL 서비스를 사용한다. 기본 포트는 5432다. 포트가 다르면 config.yaml에 실제 포트를 입력한다.

config.example.yaml을 config.yaml로 복사한다. 호스트와 포트는 database 아래에 한 번만 설정하고, database.user에 앱이 사용할 DB 이름과 로그인 정보를 입력한다. 처음 역할이나 DB를 생성하는 경우 database.bootstrap을 true로 바꾸고 database.admin에 로컬 PostgreSQL 관리자 연결 정보를 입력한다. 기존 shortform 역할이 있으면 database.user.password에 기존 비밀번호를 사용한다. 초기화 성공 후 database.bootstrap을 false로 바꾸고 관리자 비밀번호를 지운 뒤 API를 재시작한다.

백엔드는 별도 PowerShell 세션에서 실행한다.

```powershell
Set-Location apps/backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath config.yaml)) {
    Copy-Item -LiteralPath config.example.yaml -Destination config.yaml
}
# config.yaml 편집 및 저장 후 실행
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

프론트엔드는 새 PowerShell 세션에서 실행한다.

```powershell
Set-Location apps/web
npm install
npm run dev
```

브라우저에서 http://localhost:3000/references 를 연다. 공개 릴스를 등록하고 추적 쿼리를 붙여 다시 등록해 중복 여부를 확인한다. 저장 항목에서 기획을 시작해 원본 링크와 관찰 메모를 확인한다.

DB 초기화는 개발 단계에서 API 시작 시 실행된다. Alembic 마이그레이션, 자동 분석, 초안 편집, 제작 워커는 이후 개발 범위다.

## PostgreSQL 접속 오류

API는 시작 시 백엔드 폴더의 config.yaml을 읽는다. 설정을 바꾼 뒤 실행 중인 API를 재시작한다. config.yaml은 Git에서 제외하며 비밀번호는 이 로컬 파일에만 입력한다.

비밀번호 인증 오류가 발생하면 pgAdmin에서 `shortform` 역할의 로그인 허용과 비밀번호를 확인한다. `psql`이 PATH에 있다면 `psql -h 127.0.0.1 -p 5432 -U shortform -d shortform -W`로 API와 같은 접속 정보를 먼저 검증한다. 접속은 되지만 테이블 생성 권한 오류가 발생하면 `shortform` 데이터베이스의 소유자와 `public` 스키마의 생성 권한을 확인한다.

## 인스타그램 주제 검색

`apps/backend/config.yaml`의 최상위에 아래 설정을 추가한다. Instagram 프로 계정 ID와 해당 계정에 대해 Meta Hashtag Search를 호출할 수 있는 액세스 토큰을 입력한다.

```yaml
instagram:
  graph_version: v27.0
  user_id: ""
  access_token: ""
```

API를 재시작하고 `/references`에서 주제 탭 또는 해시태그 검색을 사용한다. 인기 결과는 Meta의 해시태그 인기 결과이고, 기간 필터는 조회한 결과의 게시일에 적용된다. API에서 영상 주소가 제공되면 카드에 영상 첫 프레임을 표시한다. 카드 미리보기에서 원본을 열거나, 이 영상으로 기획 버튼으로 제작 프로젝트를 시작할 수 있다.
