from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


class PiperTtsError(RuntimeError):
    pass


def _no_window_kwargs() -> dict[str, int]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def piper_config_path_for_model(model_path: Path) -> Path | None:
    """Rhasspy/OHF Piper 관례: `이름.onnx` 옆에 `이름.onnx.json` 설정 파일."""
    candidate = model_path.parent / (model_path.name + ".json")
    return candidate if candidate.is_file() else None


def expected_piper_config_path(model_path: Path) -> Path:
    """Piper가 찾는 설정 파일의 정확한 경로(존재 여부와 무관)."""
    return model_path.parent / (model_path.name + ".json")


_ESPEAK_PHONEME_TYPES = frozenset({"espeak", "espeak-ng"})


def rhasspy_piper_phoneme_incompatible_reason(model_path: Path) -> str | None:
    """
    rhasspy/OHF 배포 piper.exe는 espeak 계열 phoneme_type과 짝을 이룹니다.
    neurlang piper-kss-korean(pygoruut) 등은 같은 실행 파일로는 정상 합성이 어렵습니다.
    """
    cfg = piper_config_path_for_model(model_path)
    if cfg is None:
        return None
    try:
        raw = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    pt = raw.get("phoneme_type")
    if not isinstance(pt, str) or not pt.strip():
        return None
    key = pt.strip().lower()
    if key in _ESPEAK_PHONEME_TYPES:
        return None
    return (
        f"모델 JSON의 phoneme_type이 「{pt}」입니다.\n\n"
        "이 앱이 실행하는 **표준 Piper(piper.exe)** 는 보통 **espeak / espeak-ng** 로 "
        "글자를 음소로 바꾼 뒤 ONNX에 넣습니다.\n\n"
        "neurlang **piper-kss-korean** 은 **pygoruut** 용이라, 같은 Piper.exe로는 "
        "한국어가 제대로 나오지 않고 잡음·이상한 발음이 날 수 있습니다.\n\n"
        "**권장:** Hugging Face 저장소 **rhasspy/piper-voices** 에서 "
        "한국어 ko_KR / kss / medium 패키지의 "
        "`ko_KR-kss-medium.onnx` 와 `ko_KR-kss-medium.onnx.json` 을 받아 "
        "환경 설정의 ONNX 경로를 그쪽으로 바꾸세요.\n\n"
        "neurlang 모델만 쓰려면 문서대로 **piper-rs**(Rust, cargo run …)로 합성해야 합니다."
    )


def wrong_stem_json_hint(model_path: Path) -> str:
    """`모델.json`만 있고 `모델.onnx.json`이 없을 때 안내."""
    wrong = model_path.parent / f"{model_path.stem}.json"
    need = expected_piper_config_path(model_path)
    if wrong.is_file() and not need.is_file():
        return (
            f"\n\n같은 폴더에 「{wrong.name}」이(가) 있습니다. "
            f"Piper가 읽는 파일 이름은 「{need.name}」입니다. "
            "이름을 바꾸거나, 공식 음성 패키지의 .onnx.json을 함께 두세요."
        )
    return ""


def espeak_voice_from_config(config_path: Path) -> str | None:
    """실제 음소화에 쓰이는 espeak 음성(우선) 및 기타 언어 힌트. 없으면 None."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    # espeak.voice가 실제 엔진에 넘어가는 값이라 language 필드보다 우선
    espeak = raw.get("espeak")
    if isinstance(espeak, dict):
        v = espeak.get("voice")
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    lang = raw.get("language")
    if isinstance(lang, str) and lang.strip():
        return lang.strip().lower()
    audio = raw.get("audio")
    if isinstance(audio, dict):
        al = audio.get("language")
        if isinstance(al, str) and al.strip():
            return al.strip().lower()
    return None


def voice_config_log_lines(model_path: Path) -> list[str]:
    """로그에 남길 Piper 모델·JSON 메타 한 줄 요약(원인 조사용)."""
    lines: list[str] = [f"Piper ONNX: {model_path.resolve()}"]
    cfg = piper_config_path_for_model(model_path)
    if cfg is None:
        lines.append(
            f"동봉 JSON 없음: '{model_path.name}.json' 이 ONNX와 같은 폴더에 있어야 합니다. "
            "공식 음성은 Hugging Face `rhasspy/piper-voices` 의 ko_KR 패키지를 권장합니다."
        )
        return lines
    lines.append(f"Piper JSON: {cfg.resolve()}")
    try:
        raw = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        lines.append(f"JSON 읽기 실패: {e}")
        return lines
    if not isinstance(raw, dict):
        lines.append("JSON 루트가 객체가 아님")
        return lines
    for key in ("key", "name", "language", "phoneme_type", "phonem_type", "dataset"):
        if key in raw:
            val = raw[key]
            s = repr(val)
            if len(s) > 400:
                s = s[:400] + "…"
            lines.append(f"  {key}: {s}")
    esp = raw.get("espeak")
    if isinstance(esp, dict):
        lines.append(f"  espeak: {repr(esp)[:400]}{'…' if len(repr(esp)) > 400 else ''}")
    return lines


def onnx_filename_language_red_flags(model_path: Path) -> list[str]:
    """파일명만으로도 흔한 오설치(중국어 모델)를 짚습니다."""
    n = model_path.name.lower()
    out: list[str] = []
    if any(x in n for x in ("zh_cn", "zh_tw", "cmn_", "yue_", "mandarin", "cantonese")):
        out.append(
            "경고: ONNX 파일명이 중국어·중국 방언 계열로 보입니다. "
            "한국어 나레이션에는 `ko_KR-kss-medium` 등 **ko_KR** 로 시작하는 한국어 음성을 지정하세요."
        )
    elif "ja_jp" in n or "ja-jp" in n:
        out.append("경고: ONNX 파일명이 일본어(ja) 계열로 보입니다. 한국어는 ko_KR 모델을 쓰세요.")
    elif "ko_kr" not in n and "ko-kr" not in n and not n.startswith("ko_"):
        out.append(
            "참고: ONNX 파일명에 보통 `ko_KR` 이 들어갑니다. "
            "다른 이름의 한국어 모델이라면 이 메시지는 무시해도 됩니다."
        )
    return out


def synthesize_wav(
    text: str,
    wav_path: Path,
    *,
    piper_executable: Path,
    model_path: Path,
    timeout_sec: float = 180.0,
) -> None:
    """Piper CLI로 단일 WAV 생성. 표준 입력에 UTF-8 텍스트를 넣습니다."""
    if not piper_executable.is_file():
        raise PiperTtsError(f"Piper 실행 파일이 없습니다: {piper_executable}")
    if not model_path.is_file():
        raise PiperTtsError(f"Piper 모델(.onnx)이 없습니다: {model_path}")

    text = text.lstrip("\ufeff").strip()

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    if wav_path.exists():
        wav_path.unlink()

    cmd: list[str | Path] = [
        str(piper_executable),
        "--model",
        str(model_path),
        "--output_file",
        str(wav_path),
    ]
    cfg = piper_config_path_for_model(model_path)
    if cfg is not None:
        cmd.extend(["--config", str(cfg)])
    try:
        proc = subprocess.run(
            [str(x) for x in cmd],
            input=text,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_no_window_kwargs(),
        )
    except subprocess.TimeoutExpired as e:
        raise PiperTtsError("Piper 실행 시간 초과") from e
    except OSError as e:
        raise PiperTtsError(f"Piper 실행 오류: {e}") from e

    if proc.returncode != 0:
        err_raw = proc.stderr
        out_raw = proc.stdout
        err = (
            err_raw.decode("utf-8", errors="replace").strip()
            if isinstance(err_raw, bytes)
            else str(err_raw or "").strip()
        )
        out = (
            out_raw.decode("utf-8", errors="replace").strip()
            if isinstance(out_raw, bytes)
            else str(out_raw or "").strip()
        )
        msg = err or out or f"exit code {proc.returncode}"
        raise PiperTtsError(msg)

    if not wav_path.is_file():
        raise PiperTtsError("WAV 파일이 생성되지 않았습니다.")
