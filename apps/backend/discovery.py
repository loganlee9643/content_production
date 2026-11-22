import logging
import json
from contextvars import ContextVar
import re
from math import ceil
from time import monotonic
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Query

from config import CONFIG_PATH


router = APIRouter()
retry_after = 0.0
search_usage = ContextVar("search_usage", default=0)


def observe_search_usage(response):
    try:
        usage = json.loads(response.headers.get("X-App-Usage", "null"))
    except ValueError:
        return
    if isinstance(usage, dict):
        percentages = [usage.get(key) for key in ("call_count", "total_cputime", "total_time")]
        search_usage.set(max([search_usage.get(), *(value for value in percentages if type(value) is int)]))


def check_search_quota():
    remaining = ceil(retry_after - monotonic())
    if remaining > 0:
        raise HTTPException(
            429, "Meta 검색 호출 한도에 도달했습니다. 잠시 후 다시 검색해 주세요.",
            headers={"Retry-After": str(remaining)},
        )


def pause_search(response):
    global retry_after
    hint = response.headers.get("Retry-After", "")
    try:
        usage = json.loads(response.headers.get("X-App-Usage", "null"))
    except ValueError:
        usage = None
    if isinstance(usage, dict):
        percentages = {
            key: usage[key]
            for key in ("call_count", "total_cputime", "total_time")
            if type(usage.get(key)) is int
        }
        logger.info("Instagram rate limit: X-App-Usage=%s", percentages or "no usage values")
    else:
        logger.info("Instagram rate limit: X-App-Usage unavailable")
    delay = int(hint) if re.fullmatch(r"[0-9]{1,6}", hint) else 60
    retry_after = max(retry_after, monotonic() + max(delay, 1))
    check_search_quota()


logger = logging.getLogger("uvicorn.error")
TOPICS = {
    "사주 풀이": ("사주풀이", "사주"),
    "오늘의 운세": ("오늘의운세", "오늘운세"),
    "띠별 운세": ("띠별운세", "띠운세"),
    "재물·사업": ("재물운", "사업운"),
    "연애·궁합": ("연애운", "궁합"),
    "대운·신년": ("신년운세", "대운"),
    "신점·타로": ("신점", "타로운세"),
}
FIELDS = "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count"
LOOKUP_TIMEOUT = 8
# Hashtag media edges often take more than 10s upstream.
# Worst case per request: len(tags) * (LOOKUP_TIMEOUT + MAX_MEDIA_PAGES * MEDIA_TIMEOUT) seconds.
MEDIA_TIMEOUT = 30
MAX_MEDIA_PAGES = 3


def publish_time(value):
    try:
        if not value:
            return None
        if type(value) in (int, float):
            return datetime.fromtimestamp(value, UTC)
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)
    except (AttributeError, OverflowError, OSError, ValueError):
        return None


async def graph(client, token, path, *, stage, timeout=LOOKUP_TIMEOUT, **params):
    """Return the Graph API response body.

    Raises HTTPException 401 for Meta code 190, 429 while rate limited, and 502 otherwise.
    """
    try:
        response = await client.get(path, params={**params, "access_token": token}, timeout=timeout)
    except httpx.RequestError as error:
        logger.warning("Instagram search: stage=%s transport=%s", stage, type(error).__name__)
        raise HTTPException(502, f"Meta 서버에 연결하지 못했습니다. ({stage})") from None
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.is_success and isinstance(body, dict) and isinstance(body.get("data"), list):
        observe_search_usage(response)
        return body

    meta_error = body.get("error") if isinstance(body, dict) else None
    meta_error = meta_error if isinstance(meta_error, dict) else {}
    code = meta_error.get("code")
    subcode = meta_error.get("error_subcode")
    code = code if type(code) is int else None
    subcode = subcode if type(subcode) is int else None
    raw_message = meta_error.get("message")
    raw_message = raw_message.lower() if isinstance(raw_message, str) else ""
    if "public content access" in raw_message or "permission" in raw_message:
        reason = "permission"
    elif "user_id" in raw_message or "does not exist" in raw_message or "unsupported get request" in raw_message:
        reason = "account_or_endpoint"
    elif "hashtag" in raw_message:
        reason = "hashtag"
    else:
        reason = "unspecified"
    logger.warning(
        "Instagram search: stage=%s upstream_status=%s meta_code=%s meta_subcode=%s reason=%s",
        stage, response.status_code, code, subcode, reason,
    )
    if code == 190:
        message = "Meta 액세스 토큰이 유효하지 않거나 만료됐습니다."
    elif code in {4, 17, 32, 613, 80002} or response.status_code == 429:
        pause_search(response)
    elif code in {10, 200} or response.status_code == 403:
        message = "Meta 앱의 해시태그 검색 권한과 연결 계정을 확인해 주세요."
    elif code == 100:
        message = {
            "permission": "Meta 앱의 공개 콘텐츠 검색 권한을 확인해 주세요.",
            "account_or_endpoint": "같은 Facebook 사용자 토큰으로 연결된 Instagram 프로 계정 ID를 조회할 수 있는지 확인해 주세요.",
            "hashtag": "Meta가 해시태그 요청을 거절했습니다. 다른 태그로 확인해 주세요.",
            "unspecified": "Meta가 검색 요청을 거절했습니다. Graph API Explorer에서 같은 계정 ID와 토큰으로 해시태그 검색을 확인해 주세요.",
        }[reason]
    else:
        message = "인스타그램 검색 결과를 가져오지 못했습니다."
    diagnostic = f"{stage}, Meta {code}" if code is not None else f"{stage}, HTTP {response.status_code}"
    raise HTTPException(401 if code == 190 else 502, f"{message} ({diagnostic})")


async def media_pages(client, path, user, token, since=None):
    """Read up to MAX_MEDIA_PAGES pages of a hashtag media edge.

    A first-page failure raises. A later-page failure other than 429 keeps the earlier pages.
    With since, paging stops after a page whose posts are all older than since.
    """
    params = {"user_id": user, "fields": FIELDS, "limit": 50}
    seen = set()
    items = []
    for page_number in range(1, MAX_MEDIA_PAGES + 1):
        check_search_quota()
        try:
            body = await graph(client, token, path, stage="media_lookup", timeout=MEDIA_TIMEOUT, **params)
        except HTTPException as error:
            if not items or error.status_code == 429:
                raise
            logger.warning("Instagram media: page=%s upstream request failed", page_number)
            break
        media = body["data"]
        items.extend(media)
        if since and media and all(
            (published := publish_time(item.get("timestamp"))) is not None and published < since
            for item in media
        ):
            break
        paging = body.get("paging")
        next_url = paging.get("next") if isinstance(paging, dict) else None
        if not isinstance(next_url, str):
            break
        next_params = {
            key: value for key, value in parse_qsl(urlsplit(next_url).query)
            if key in {"after", "offset", "__paging_token"}
        }
        key = tuple(sorted(next_params.items()))
        if not key or key in seen:
            break
        seen.add(key)
        params.update(next_params)
    return items


@router.get("/api/references/instagram/discover")
async def discover(
    q: str = Query(min_length=1, max_length=60), order: str = "recent", *, since=None, hashtag_ids=None,
):
    """Collect reels for a topic or hashtag.

    hashtag_ids maps a tag to a known Meta hashtag ID; the lookup is skipped for those tags.
    since stops media paging after a page whose posts are all older than it.
    The result's hashtagIds holds the hashtag ID used for each tag.
    """
    if order not in {"recent", "top"}:
        raise HTTPException(422, "지원하지 않는 검색 순서입니다.")
    query = q.strip()
    tag = "".join(query.removeprefix("#").split())
    tags = TOPICS.get(query, (tag,))
    if not all(re.fullmatch(r"[\w]+", value) for value in tags):
        raise HTTPException(422, "주제 또는 해시태그를 입력해 주세요.")

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    settings = config.get("instagram") or {}
    user = settings.get("user_id")
    token = settings.get("access_token")
    if not user or not token:
        raise HTTPException(503, "config.yaml에 인스타그램 프로 계정 ID와 Meta 액세스 토큰을 설정해 주세요.")
    if type(user) not in (str, int) or not re.fullmatch(r"[0-9]+", str(user).strip()):
        raise HTTPException(503, "instagram.user_id에 Facebook 페이지에 연결된 Instagram 프로 계정의 숫자 ID를 입력해 주세요.")
    if not isinstance(token, str) or not token.strip():
        raise HTTPException(503, "instagram.access_token에 Facebook 사용자 액세스 토큰을 입력해 주세요.")
    user = str(user).strip()
    token = token.strip()
    version = settings.get("graph_version", "v27.0")
    if not isinstance(version, str) or not re.fullmatch(r"v\d+\.\d+", version):
        raise HTTPException(503, "config.yaml의 Instagram API 버전을 확인해 주세요.")

    found = {}
    resolved = {}
    async with httpx.AsyncClient(base_url=f"https://graph.facebook.com/{version}/", timeout=LOOKUP_TIMEOUT) as client:
        for value in tags:
            hashtag_id = (hashtag_ids or {}).get(value)
            if hashtag_id is None:
                check_search_quota()
                ids = (await graph(
                    client, token, "ig_hashtag_search", stage="hashtag_lookup", user_id=user, q=value,
                ))["data"]
                logger.info("Instagram discovery: order=%s hashtag_matches=%s", order, len(ids))
                if not ids:
                    continue
                hashtag_id = ids[0]["id"]
            resolved[value] = hashtag_id
            media = await media_pages(client, f"{hashtag_id}/{order}_media", user, token, since)
            reels = 0
            for item in media:
                url = item.get("permalink") or ""
                reel = re.fullmatch(r"https://(?:www\.)?instagram\.com/reel/([A-Za-z0-9_-]+)/?", url)
                if item.get("media_type") == "VIDEO" and reel:
                    found.setdefault(reel[1], {**item, "tag": value})
                    reels += 1
            logger.info("Instagram %s media: posts=%s reels=%s", order, len(media), reels)
    items = list(found.values())
    if order == "recent":
        items.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return {"items": items, "tags": tags, "hashtagIds": resolved}