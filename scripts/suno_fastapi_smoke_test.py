from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_MODEL = "chirp-fenix"


class SunoTestError(RuntimeError):
    pass


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SunoTestError(f"HTTP {exc.code} {url}\n{detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise SunoTestError(f"요청 실패: {url}\n{exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SunoTestError(f"JSON이 아닌 응답입니다: {url}\n{raw[:1000]}") from exc


def _print_json(label: str, value: Any) -> None:
    print(f"\n[{label}]")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _clip_ids(payload: Any) -> list[str]:
    if isinstance(payload, dict) and isinstance(payload.get("clips"), list):
        return [
            str(item["id"])
            for item in payload["clips"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            clip_id = value.get("id")
            if isinstance(clip_id, str) and clip_id and clip_id not in found:
                if any(key in value for key in ("status", "audio_url", "metadata", "title")):
                    found.append(clip_id)
            for key in ("clips", "data", "songs", "result"):
                if key in value:
                    visit(value[key])
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return found


def _feed_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "clips", "songs", "result"):
            items = _feed_items(payload.get(key))
            if items:
                return items
        if isinstance(payload.get("id"), str):
            return [payload]
    return []


def check_server(base_url: str) -> None:
    root = _request_json("GET", f"{base_url}/")
    _print_json("서버 응답", root)
    credits = _request_json("GET", f"{base_url}/get_credits", timeout=60.0)
    _print_json("Suno 크레딧", credits)


def submit_description(args: argparse.Namespace) -> Any:
    payload = {
        "gpt_description_prompt": args.prompt,
        "make_instrumental": args.instrumental,
        "mv": args.model,
        "prompt": "",
    }
    return _request_json(
        "POST",
        f"{args.base_url}/generate/description-mode",
        payload,
        timeout=120.0,
    )


def submit_custom(args: argparse.Namespace) -> Any:
    lyrics = args.lyrics
    if args.lyrics_file:
        lyrics = Path(args.lyrics_file).read_text(encoding="utf-8")
    if not lyrics.strip():
        raise SunoTestError("custom 모드에는 --lyrics 또는 --lyrics-file이 필요합니다.")
    payload = {
        "prompt": lyrics,
        "mv": args.model,
        "title": args.title,
        "tags": args.tags,
        "negative_tags": args.negative_tags,
        "continue_at": None,
        "continue_clip_id": None,
    }
    return _request_json("POST", f"{args.base_url}/generate", payload, timeout=120.0)


def wait_for_songs(
    base_url: str,
    clip_ids: list[str],
    *,
    timeout: float,
    poll_interval: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    ids = ",".join(clip_ids)
    last_items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        payload = _request_json("GET", f"{base_url}/feed/{ids}", timeout=60.0)
        last_items = _feed_items(payload)
        states = [
            f"{item.get('id', '?')}={item.get('status', 'unknown')}"
            for item in last_items
        ]
        print(f"[상태] {', '.join(states) if states else '응답 대기 중'}")
        complete = [item for item in last_items if item.get("audio_url")]
        failed = [
            item
            for item in last_items
            if str(item.get("status", "")).lower() in {"error", "failed"}
        ]
        if failed:
            raise SunoTestError(
                "생성 실패:\n" + json.dumps(failed, ensure_ascii=False, indent=2)
            )
        if len(complete) >= len(clip_ids):
            return complete
        time.sleep(max(1.0, poll_interval))
    raise SunoTestError(
        f"{timeout:.0f}초 안에 생성되지 않았습니다.\n"
        + json.dumps(last_items, ensure_ascii=False, indent=2)
    )


def download_song(url: str, destination: Path, *, timeout: float = 300.0) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.part")
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 256):
                output.write(chunk)
        if partial.stat().st_size < 1024:
            raise SunoTestError(f"다운로드 파일이 너무 작습니다: {partial.stat().st_size} bytes")
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def save_lyrics(song: dict[str, Any], destination: Path) -> bool:
    metadata = song.get("metadata")
    lyrics = metadata.get("prompt") if isinstance(metadata, dict) else None
    if not isinstance(lyrics, str) or not lyrics.strip():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(lyrics.strip() + "\n", encoding="utf-8")
    return True


def save_song_outputs(args: argparse.Namespace, songs: list[dict[str, Any]]) -> None:
    output_dir = Path(args.output_dir).resolve()
    for index, song in enumerate(songs, start=1):
        clip_id = str(song.get("id", "")).strip() or f"song_{index}"
        if args.save_lyrics:
            lyrics_path = output_dir / f"suno_{clip_id}.txt"
            if save_lyrics(song, lyrics_path):
                print(f"[가사 저장] {lyrics_path}")
            else:
                print(f"[가사 없음] {clip_id}")
        if args.no_download:
            continue
        audio_url = str(song.get("audio_url", "")).strip()
        if not audio_url:
            continue
        destination = output_dir / f"suno_{clip_id}.mp3"
        print(f"[다운로드] {destination}")
        download_song(audio_url, destination)
        print(f"[완료] {destination} ({destination.stat().st_size:,} bytes)")


def run_generation(args: argparse.Namespace) -> None:
    check_server(args.base_url)
    submitted = submit_description(args) if args.command == "description" else submit_custom(args)
    _print_json("생성 요청 응답", submitted)
    clip_ids = _clip_ids(submitted)
    if not clip_ids:
        raise SunoTestError(
            "응답에서 클립 ID를 찾지 못했습니다. 서버가 현재 Suno 응답과 호환되지 않을 수 있습니다."
        )
    print(f"\n[클립 ID] {', '.join(clip_ids)}")
    songs = wait_for_songs(
        args.base_url,
        clip_ids,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    _print_json("완료 결과", songs)
    save_song_outputs(args, songs)


def fetch_existing(args: argparse.Namespace) -> None:
    check_server(args.base_url)
    clip_ids = [value.strip() for value in args.clip_ids.split(",") if value.strip()]
    if not clip_ids:
        raise SunoTestError("--clip-ids에 하나 이상의 곡 ID가 필요합니다.")
    songs = wait_for_songs(
        args.base_url,
        clip_ids,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )
    _print_json("완료 결과", songs)
    save_song_outputs(args, songs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SunoAI-API/Suno-API 로컬 FastAPI 서버 스모크 테스트"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"FastAPI 서버 주소 (기본값: {DEFAULT_BASE_URL})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="서버 연결과 Suno 크레딧만 확인")

    fetch = subparsers.add_parser("fetch", help="기존 곡 ID를 조회하고 다운로드")
    fetch.add_argument("--clip-ids", required=True, help="쉼표로 구분한 곡 ID")

    description = subparsers.add_parser("description", help="음악 설명으로 곡 생성")
    description.add_argument(
        "--prompt",
        default="A calm cinematic instrumental with warm piano and soft strings.",
    )
    description.add_argument("--instrumental", action="store_true")
    description.add_argument("--model", default=DEFAULT_MODEL)

    custom = subparsers.add_parser("custom", help="가사와 스타일로 곡 생성")
    custom.add_argument("--title", default="Suno API Test")
    custom.add_argument("--tags", default="cinematic pop, warm piano")
    custom.add_argument("--negative-tags", default="")
    custom.add_argument("--lyrics", default="")
    custom.add_argument("--lyrics-file")
    custom.add_argument("--model", default=DEFAULT_MODEL)

    for child in (fetch, description, custom):
        child.add_argument("--output-dir", default="tmp/suno_api_test")
        child.add_argument("--timeout", type=float, default=300.0)
        child.add_argument("--poll-interval", type=float, default=10.0)
        child.add_argument("--no-download", action="store_true")
        child.add_argument(
            "--save-lyrics",
            action="store_true",
            help="완료된 곡의 가사를 UTF-8 텍스트 파일로 저장",
        )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.base_url = args.base_url.rstrip("/")
    try:
        if args.command == "check":
            check_server(args.base_url)
        elif args.command == "fetch":
            fetch_existing(args)
        else:
            run_generation(args)
        return 0
    except (SunoTestError, OSError, ValueError) as exc:
        print(f"\n[실패] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[중단] 사용자가 테스트를 중단했습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
