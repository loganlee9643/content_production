from __future__ import annotations

import argparse
import sys
from argparse import Namespace

import suno_fastapi_smoke_test as smoke


# The previous successful response reported model_name=chirp-v3 and
# major_model_version=v3. This API used chirp-v3-0 as the request key.
DEFAULT_MODEL = "chirp-v3-0"
DEFAULT_PROMPT = (
    "90s Korean synthpop with a nostalgic rainy-night atmosphere, "
    "warm analog synthesizers, gentle piano, soft female vocals, "
    "a memorable melodic chorus, and polished studio production."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "과거 성공 조건인 Suno description mode와 v3 모델 전용 테스트"
        )
    )
    parser.add_argument(
        "--base-url",
        default=smoke.DEFAULT_BASE_URL,
        help=f"FastAPI 서버 주소 (기본값: {smoke.DEFAULT_BASE_URL})",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="음악 설명")
    parser.add_argument(
        "--instrumental",
        action="store_true",
        help="보컬 없는 연주곡으로 생성",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            f"Suno 내부 모델 키 (기본값: {DEFAULT_MODEL}, "
            "v3.5 후보는 chirp-v3-5)"
        ),
    )
    parser.add_argument("--output-dir", default="tmp/suno_description_v35")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="완료된 음원을 다운로드하지 않음",
    )
    parser.add_argument(
        "--save-lyrics",
        action="store_true",
        help="응답에 가사가 있으면 UTF-8 텍스트 파일로 저장",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="생성 요청 없이 서버와 Suno 크레딧 조회만 확인",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    base_url = args.base_url.rstrip("/")
    try:
        if args.check_only:
            smoke.check_server(base_url)
            return 0

        smoke.run_generation(
            Namespace(
                command="description",
                base_url=base_url,
                prompt=args.prompt,
                instrumental=args.instrumental,
                model=args.model,
                output_dir=args.output_dir,
                timeout=args.timeout,
                poll_interval=args.poll_interval,
                no_download=args.no_download,
                save_lyrics=args.save_lyrics,
            )
        )
        return 0
    except (smoke.SunoTestError, OSError, ValueError) as exc:
        print(f"\n[실패] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[중단] 사용자가 테스트를 중단했습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
