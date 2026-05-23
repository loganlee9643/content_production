# 콘텐츠 제작 (Content Production)

스토리보드 기반 영상 제작 및 **WAV 목록 합치기** 모드를 지원하는 데스크톱 앱입니다.  
PySide6 GUI, FFmpeg 렌더, Gemini API, Piper TTS, STT(Whisper) 등을 사용합니다.

---

## 요구 사항

- **Python** 3.10 이상 권장 (프로젝트 가상환경은 3.11 기준으로 테스트)
- **Windows** 기준 안내 (macOS/Linux도 동일하게 `pip` + PATH 도구 설치 가능)

---

## 1. pip 설치 패키지

아래는 `requirements.txt`에 정의된 **Python 패키지**입니다. 가상환경 사용을 권장합니다.

| 패키지 | 용도 |
|--------|------|
| [PySide6](https://pypi.org/project/PySide6/) | GUI (Qt) |
| [faster-whisper](https://pypi.org/project/faster-whisper/) | WAV 정밀 자막 생성(STT), 1차 엔진 |
| [openai-whisper](https://pypi.org/project/openai-whisper/) | STT 대체 경로 (`whisper` CLI) |
| [google-generativeai](https://pypi.org/project/google-generativeai/) | Gemini API (환경/확장용; 앱 내 일부 호출은 REST `urllib` 사용) |

### 설치 방법

```powershell
cd C:\JavaProject\content_production

# 가상환경 생성 (최초 1회)
python -m venv .venv

# 활성화 (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# 패키지 설치
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> **참고:** `faster-whisper` / `openai-whisper`는 처음 STT 실행 시 모델을 내려받아 시간이 걸릴 수 있습니다.  
> `torch` 등 의존성이 함께 설치되며 용량이 큽니다.

---

## 2. 별도 설치 도구 (PATH)

이 항목들은 **pip로 설치되지 않습니다.** 실행 파일이 `PATH`에 있거나, 앱 **환경 설정**에서 경로를 지정해야 합니다.

### FFmpeg / FFprobe (필수에 가깝음)

| 도구 | 용도 |
|------|------|
| **ffmpeg** | WAV 자르기/합치기, MP4 렌더, 오디오 인코딩, STT용 MP3 변환 등 |
| **ffprobe** | 오디오 길이(초) 확인 — 보통 FFmpeg 설치 시 함께 제공 |

**Windows 설치 예 (택 1)**

1. [gyan.dev FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/) 등에서 `ffmpeg`/`ffprobe` 포함 빌드 다운로드
2. 압축 해제 후 `bin` 폴더를 시스템 **PATH**에 추가
3. 새 터미널에서 확인:

```powershell
ffmpeg -version
ffprobe -version
```

앱 내 **환경 검증**(F5)에서도 ffmpeg/ffprobe 상태를 확인할 수 있습니다.

---

### Piper TTS (스토리보드 모드 — TTS 사용 시)

| 도구 | 용도 |
|------|------|
| **piper** (`piper.exe`) | 한국어/다국어 TTS로 씬별 WAV 생성 |

**모델 파일 (별도 다운로드)**

- `*.onnx` + **같은 이름의** `*.onnx.json` (예: `ko_KR-kss-medium.onnx`, `ko_KR-kss-medium.onnx.json`)
- Rhasspy/OHF 계열 Piper와 호환되는 한국어 음성 권장: [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) — `ko_KR` / `kss` / `medium`

**앱 설정**

- 메뉴 **환경 설정** → TTS: Piper 실행 파일 경로, ONNX 모델 경로

> neurlang `piper-kss-korean` 등 **pygoruut** 계열 모델은 기본 `piper.exe`와 phoneme 방식이 달라 잡음이 날 수 있습니다. 위 rhasspy 패키지 사용을 권장합니다.

---

### Ollama (선택 — 스토리보드 LLM)

| 도구 | 용도 |
|------|------|
| **Ollama** | 로컬 LLM으로 씬 생성 (Gemini 대신 선택 가능) |

- [Ollama 설치](https://ollama.com/) 후 로컬 서버 실행
- 앱 **환경 설정**에서 LLM 제공자를 Ollama로 선택하고 URL/모델명 설정

---

## 3. API 키 및 환경 변수 (선택)

### Gemini (구간 분석, 이미지 생성, LLM 씬 생성 등)

- 앱 **환경 설정** → Gemini API 키  
  또는 환경 변수:

```powershell
$env:GEMINI_API_KEY = "your-api-key"
```

### 로그

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `CONTENT_PRODUCTION_LOG` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `off` | `DEBUG` |
| `CONTENT_PRODUCTION_LOG_FILE` | 로그 파일 경로 | `logs/content_production.log` |
| `CONTENT_PRODUCTION_GEMINI_DUMP_CHARS` | Gemini 요청/응답 로그 길이 | `6000` |

### STT (정밀 자막 생성)

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `CONTENT_PRODUCTION_STT_MODEL` | faster-whisper 모델 크기 (`medium`, `large-v3` 권장) | `small` |
| `CONTENT_PRODUCTION_STT_COMPUTE` | `int8`, `float16` 등 | `int8` |
| `CONTENT_PRODUCTION_STT_VAD_FILTER` | `true`면 무음·비음성 구간 제거 (음악 가창에선 가사 누락) | `false` |
| `CONTENT_PRODUCTION_STT_BEAM_SIZE` | 빔 서치 크기 (클수록 정확·느림) | `5` |
| `CONTENT_PRODUCTION_STT_NO_SPEECH_THRESHOLD` | 이 값보다 `no_speech` 확률이 크면 구간 스킵 (음악은 높게) | `0.99` |
| `CONTENT_PRODUCTION_STT_LOG_PROB_THRESHOLD` | 평균 logprob가 이보다 낮으면 재시도 (음수, 더 작을수록 관대) | `-2.0` |

---

## 4. 실행

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

또는 가상환경 Python 직접 사용:

```powershell
.\.venv\Scripts\python.exe main.py
```

---

## 5. 프로젝트 모드 요약

| 모드 | 주요 기능 | 외부 의존 |
|------|-----------|-----------|
| **스토리보드** | 씬 편집, Piper TTS, SRT, MP4 렌더 | ffmpeg, piper( TTS 시 ), Gemini/Ollama(선택) |
| **WAV 목록** | WAV별 구간·자막·이미지, 합쳐 MP4 | ffmpeg, Gemini(구간/이미지), STT(선택) |

WAV 모드에서는 원본 WAV를 자르지 않고 구간 타임라인으로 영상·자막을 맞춥니다.

---

## 6. 문제 해결

| 증상 | 확인 |
|------|------|
| `ffmpeg를 찾지 못했습니다` | PATH에 ffmpeg 추가 후 앱 재시작 |
| `ffprobe N/A` / 렌더 실패 | WAV 파일 손상 여부, ffmpeg 버전 |
| Piper 잡음/이상 발음 | rhasspy 호환 `.onnx` + `.onnx.json` 쌍 사용 |
| STT 실패 | `.venv`에 `faster-whisper` 설치, 첫 실행 시 모델 다운로드 대기 |
| Gemini `MAX_TOKENS` | 환경 설정에서 분석 모델을 `gemini-2.5-flash` 등으로 변경 |

로그 파일: 프로젝트 루트 `logs/content_production.log`
