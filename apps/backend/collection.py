import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Query
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from media_source import MEDIA_URL_PATTERN

from config import get_settings
from discovery import (
    LOOKUP_TIMEOUT, MAX_MEDIA_PAGES, MEDIA_TIMEOUT, TOPICS, discover, publish_time, search_usage,
)


router = APIRouter()
logger = logging.getLogger(__name__)
LOCK_ID = 716480272
RATE_DELAY = timedelta(minutes=15)
WORKER_SLEEP = 10
# Two worst-case loops: sleep, one hashtag lookup, and every media page at its timeout.
WORKER_STALE_AFTER = timedelta(seconds=2 * (WORKER_SLEEP + LOOKUP_TIMEOUT + MAX_MEDIA_PAGES * MEDIA_TIMEOUT))
TOP_INTERVAL = timedelta(hours=24)


class ManualCollectionOptions(BaseModel):
    hours: int = 24


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS collection_tags (
        tag text PRIMARY KEY, due_at timestamptz NOT NULL,
        last_success timestamptz, last_error text
    )""",
    "ALTER TABLE collection_tags ADD COLUMN IF NOT EXISTS hashtag_id text",
    "ALTER TABLE collection_tags ADD COLUMN IF NOT EXISTS top_due_at timestamptz NOT NULL DEFAULT now()",
    """CREATE TABLE IF NOT EXISTS collected_reels (
        id text PRIMARY KEY, published_at timestamptz, payload jsonb NOT NULL,
        first_collected_at timestamptz NOT NULL, collected_at timestamptz NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS collected_reel_tags (
        reel_id text REFERENCES collected_reels(id), tag text REFERENCES collection_tags(tag),
        PRIMARY KEY (reel_id, tag)
    )""",
    """CREATE TABLE IF NOT EXISTS collection_requests (
        id uuid PRIMARY KEY, topic text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "ALTER TABLE collection_requests ADD COLUMN IF NOT EXISTS window_hours integer NOT NULL DEFAULT 24",
    """CREATE TABLE IF NOT EXISTS collection_request_tags (
        request_id uuid REFERENCES collection_requests(id), tag text REFERENCES collection_tags(tag),
        state text NOT NULL DEFAULT 'pending', new_count integer NOT NULL DEFAULT 0,
        completed_at timestamptz, PRIMARY KEY (request_id, tag)
    )""",
    """CREATE TABLE IF NOT EXISTS collection_gate (
        id integer PRIMARY KEY CHECK (id = 1), next_call_at timestamptz NOT NULL DEFAULT now(),
        running_tag text, limited boolean NOT NULL DEFAULT false
    )""",
    "ALTER TABLE collection_gate ADD COLUMN IF NOT EXISTS worker_seen_at timestamptz",
    "CREATE INDEX IF NOT EXISTS collected_reels_published ON collected_reels (published_at DESC)",
    "CREATE INDEX IF NOT EXISTS collection_requests_topic ON collection_requests (topic, created_at DESC)",
)


def connection(*, autocommit=False):
    return psycopg.connect(
        get_settings().database_dsn, row_factory=dict_row, autocommit=autocommit,
    )


def configure_logging():
    logging.basicConfig(level=logging.INFO)
    # httpx logs every request URL at INFO, and Graph API URLs carry the access token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def worker_alive(seen_at, now):
    return seen_at is not None and now - seen_at <= WORKER_STALE_AFTER


def topic_status(states, gate, alive, now):
    """Return stopped > running > token_expired > delayed > ready > partial > empty, first match wins."""
    errors = {row["last_error"] for row in states if row["last_error"]}
    successes = sum(1 for row in states if row["last_success"])
    if not alive:
        return "stopped"
    if gate["running_tag"] in {row["tag"] for row in states}:
        return "running"
    if "token_expired" in errors:
        return "token_expired"
    if errors or (gate["limited"] and gate["next_call_at"] > now):
        return "delayed"
    if successes == len(states):
        return "ready"
    return "partial" if successes else "empty"


def may_call(gate, has_manual, now):
    """A pending manual request skips the call interval, but not a Meta rate limit."""
    return gate["next_call_at"] <= now or (has_manual and not gate["limited"])


def pick_job(rows, now):
    """Pick the oldest pending manual request, else the earliest overdue recent or top run, else None."""
    manual = [row for row in rows if row["requested_at"] is not None]
    if manual:
        row = min(manual, key=lambda row: (row["requested_at"], row["tag"]))
        return {
            "tag": row["tag"], "mode": "top" if row["window_hours"] > 24 else "recent",
            "request_id": row["request_id"], "hours": row["window_hours"],
        }
    due = [
        (row[column], rank, row["tag"], mode)
        for row in rows
        for rank, (column, mode) in enumerate((("due_at", "recent"), ("top_due_at", "top")))
        if row[column] <= now
    ]
    if not due:
        return None
    _, _, tag, mode = min(due)
    return {"tag": tag, "mode": mode, "request_id": None, "hours": 24}


def call_slot(tag_count, interval_hours):
    """Spread one day over every scheduled run: each tag's recent runs plus one top run."""
    return timedelta(hours=24) / (tag_count * (24 / interval_hours + 1))


def error_label(status_code):
    return "token_expired" if status_code == 401 else f"HTTP {status_code}"


def initialize_collection(db):
    interval = timedelta(hours=get_settings().collection_interval_hours)
    db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID + 1,))
    for statement in SCHEMA:
        db.execute(statement)
    db.execute("INSERT INTO collection_gate (id) VALUES (1) ON CONFLICT DO NOTHING")
    tags = list(dict.fromkeys(tag for topic_tags in TOPICS.values() for tag in topic_tags))
    now = datetime.now(UTC)
    for index, tag in enumerate(tags):
        db.execute(
            "INSERT INTO collection_tags (tag, due_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tag, now + interval * index / len(tags)),
        )


def request_status(db, topic):
    row = db.execute(
        """SELECT r.id AS id, r.window_hours, count(*) FILTER (WHERE t.state = 'pending') AS pending,
                  count(*) FILTER (WHERE t.state = 'succeeded') AS succeeded,
                  count(*) FILTER (WHERE t.state = 'failed') AS failed,
                  count(*) FILTER (WHERE t.state = 'cancelled') AS cancelled,
                  sum(t.new_count) AS new_count, max(t.completed_at) AS completed_at,
                  coalesce(bool_or(t.state = 'pending' AND t.tag = g.running_tag), false) AS running
           FROM collection_requests r JOIN collection_request_tags t ON t.request_id = r.id
           CROSS JOIN collection_gate g
           WHERE r.id = (SELECT id FROM collection_requests WHERE topic = %s
                         ORDER BY created_at DESC, id DESC LIMIT 1)
           GROUP BY r.id""",
        (topic,),
    ).fetchone()
    if row is None:
        return None
    if row["pending"]:
        state = "running" if row["running"] else "queued"
    elif row["cancelled"]:
        state = "cancelled"
    elif row["failed"]:
        state = "partial" if row["succeeded"] else "failed"
    else:
        state = "completed"
    return {
        "id": row["id"], "status": state, "newCount": row["new_count"],
        "hours": row["window_hours"],
        "completedAt": row["completed_at"] if not row["pending"] else None,
    }


@router.get("/api/reference-topics")
def collection_topics():
    with connection() as db:
        rows = db.execute("SELECT * FROM collection_tags").fetchall()
        by_tag = {row["tag"]: row for row in rows}
        gate = db.execute("SELECT * FROM collection_gate WHERE id = 1").fetchone()
        now = datetime.now(UTC)
        alive = worker_alive(gate["worker_seen_at"], now)
        topics = []
        for topic, tags in TOPICS.items():
            states = [by_tag[tag] for tag in tags]
            successes = [row["last_success"] for row in states if row["last_success"]]
            request = request_status(db, topic)
            next_at = min(row["due_at"] for row in states)
            next_automatic_at = max(next_at, gate["next_call_at"])
            manual_pending = request and request["status"] in {"queued", "running"}
            if manual_pending:
                next_at = now
            delayed = gate["limited"] and gate["next_call_at"] > now
            status = topic_status(states, gate, alive, now)
            topics.append({
                "name": topic, "lastCollectedAt": min(successes) if len(successes) == len(tags) else None,
                "lastActivityAt": max(successes) if successes else None,
                "collectedTagCount": len(successes), "activeTagCount": len(tags),
                "nextCollectAt": next_at if manual_pending and not delayed else max(next_at, gate["next_call_at"]),
                "nextAutomaticAt": next_automatic_at,
                "status": status, "request": request,
            })
        return {"topics": topics}


@router.post("/api/reference-topics/{topic}/collection-requests", status_code=202)
def enqueue_collection(
    topic: str, options: ManualCollectionOptions | None = None,
    period_hours: int | None = None,
):
    tags = TOPICS.get(topic)
    if not tags:
        raise HTTPException(404, "수집할 주제를 찾을 수 없습니다.")
    hours = period_hours if period_hours is not None else options.hours if options else 24
    if hours not in {6, 12, 24, 168, 720}:
        raise HTTPException(422, "지원하지 않는 수동 수집 기간입니다.")
    with connection() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", ("collection:" + topic,))
        current = request_status(db, topic)
        if current and current["status"] in {"queued", "running"}:
            if current["hours"] != hours:
                raise HTTPException(409, "이 주제의 수집 요청이 이미 진행 중입니다.")
            return current
        request_id = uuid4()
        db.execute(
            "INSERT INTO collection_requests (id, topic, window_hours) VALUES (%s, %s, %s)",
            (request_id, topic, hours),
        )
        for tag in tags:
            db.execute(
                "INSERT INTO collection_request_tags (request_id, tag) VALUES (%s, %s)",
                (request_id, tag),
            )
        return {"id": request_id, "status": "queued", "newCount": 0, "completedAt": None, "hours": hours}


@router.delete("/api/reference-topics/{topic}/collection-requests/{request_id}")
def cancel_collection(topic: str, request_id: UUID):
    with connection() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", ("collection:" + topic,))
        current = request_status(db, topic)
        if current is None or current["id"] != request_id:
            raise HTTPException(404, "이 주제에서 해당 수집 요청을 찾을 수 없습니다.")
        if current["status"] == "cancelled":
            return current
        if current["status"] != "queued":
            raise HTTPException(409, "대기 중인 수집 요청만 취소할 수 있습니다.")
        db.execute(
            """UPDATE collection_request_tags
               SET state = 'cancelled', completed_at = now()
               WHERE request_id = %s AND state = 'pending'""",
            (request_id,),
        )
        return request_status(db, topic)


@router.get("/api/references/collected")
def list_collected(
    topic: str | None = None, q: str = Query(default="", max_length=60),
    days: int = Query(default=7, ge=0, le=365), order: str = "recent",
    analyzable_only: bool = False,
):
    if topic and topic not in TOPICS:
        raise HTTPException(422, "지원하지 않는 주제입니다.")
    if order not in {"recent", "engagement"}:
        raise HTTPException(422, "지원하지 않는 정렬입니다.")
    tags = list(TOPICS[topic]) if topic else None
    term = q.strip().removeprefix("#")
    deadline = datetime.now(UTC) - timedelta(days=days) if days else None
    sorting = """
        (CASE WHEN payload ? 'like_count' OR payload ? 'comments_count'
              THEN coalesce((payload->>'like_count')::bigint, 0)
                 + coalesce((payload->>'comments_count')::bigint, 0) END) DESC NULLS LAST,
    """ if order == "engagement" else ""
    with connection() as db:
        rows = db.execute(
            f"""SELECT r.payload FROM collected_reels r
                WHERE (%(tags)s::text[] IS NULL OR EXISTS (
                    SELECT 1 FROM collected_reel_tags rt
                    WHERE rt.reel_id = r.id AND rt.tag = ANY(%(tags)s::text[])
                )) AND (%(deadline)s::timestamptz IS NULL OR r.published_at >= %(deadline)s)
                AND (%(term)s = '' OR strpos(lower(coalesce(payload->>'caption', '')), lower(%(term)s)) > 0
                    OR EXISTS (SELECT 1 FROM collected_reel_tags rt WHERE rt.reel_id = r.id
                               AND strpos(lower(rt.tag), lower(%(term)s)) > 0))
                AND (NOT %(analyzable_only)s OR (r.payload->>'media_url') ~* %(media_url_pattern)s)
                ORDER BY {sorting} r.published_at DESC NULLS LAST, r.id LIMIT 100""",
            {"tags": tags, "deadline": deadline, "term": term,
             "analyzable_only": analyzable_only, "media_url_pattern": MEDIA_URL_PATTERN},
        ).fetchall()
    return {"items": [row["payload"] for row in rows]}


def save_reels(db, tag, items, now):
    new_count = 0
    for item in items:
        payload = {key: value for key, value in item.items() if value is not None}
        payload.update(id=str(item["id"]), tag=tag)
        for key in ("like_count", "comments_count"):
            if type(payload.get(key)) is not int:
                payload.pop(key, None)
        params = (payload["id"], publish_time(payload.get("timestamp")), Jsonb(payload), now, now)
        inserted = db.execute(
            """INSERT INTO collected_reels (id, published_at, payload, first_collected_at, collected_at)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id""", params,
        ).fetchone()
        if inserted:
            new_count += 1
        else:
            db.execute(
                """UPDATE collected_reels SET published_at = coalesce(%s, published_at),
                       payload = payload || %s, collected_at = %s WHERE id = %s""",
                (params[1], params[2], now, params[0]),
            )
        db.execute(
            "INSERT INTO collected_reel_tags (reel_id, tag) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (payload["id"], tag),
        )
    return new_count


async def collect_one():
    # A session lock spans the external request and releases automatically on disconnect.
    with connection(autocommit=True) as db:
        if not db.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (LOCK_ID,)).fetchone()["acquired"]:
            return "busy"
        gate = db.execute("SELECT * FROM collection_gate WHERE id = 1").fetchone()
        now = datetime.now(UTC)
        db.execute("UPDATE collection_gate SET running_tag = NULL WHERE id = 1 AND running_tag IS NOT NULL")
        rows = db.execute(
            """SELECT t.tag, t.due_at, t.top_due_at, t.hashtag_id, t.last_success,
                      p.requested_at, p.request_id, p.window_hours
               FROM collection_tags t
               LEFT JOIN LATERAL (
                   SELECT r.created_at AS requested_at, r.id AS request_id, r.window_hours
                   FROM collection_request_tags rt JOIN collection_requests r ON r.id = rt.request_id
                   WHERE rt.tag = t.tag AND rt.state = 'pending'
                   ORDER BY r.created_at, r.id LIMIT 1
               ) p ON true""",
        ).fetchall()
        job = pick_job(rows, now)
        if job is None:
            return "done"
        if not may_call(gate, job["request_id"] is not None, now):
            return "deferred"
        tag, mode, request_id, hours = job["tag"], job["mode"], job["request_id"], job["hours"]
        row = next(row for row in rows if row["tag"] == tag)
        interval_hours = get_settings().collection_interval_hours
        interval = timedelta(hours=interval_hours)
        slot = call_slot(len({tag for tags in TOPICS.values() for tag in tags}), interval_hours)
        db.execute(
            """UPDATE collection_gate SET running_tag = %s,
               next_call_at = greatest(next_call_at, %s), limited = false WHERE id = 1""",
            (tag, now + slot),
        )
        automatic = request_id is None
        try:
            search_usage.set(0)
            result = await discover(
                q=tag, order=mode, hashtag_ids={tag: row["hashtag_id"]},
                since=row["last_success"] if automatic and mode == "recent" else None,
            )
        except HTTPException as error:
            now = datetime.now(UTC)
            with db.transaction():
                if error.status_code == 429:
                    hint = (error.headers or {}).get("Retry-After", "")
                    delay = timedelta(seconds=int(hint)) if hint.isdecimal() else RATE_DELAY
                    if delay <= timedelta(minutes=1):
                        delay = RATE_DELAY
                    if gate["limited"]:
                        delay = max(delay, timedelta(hours=1))
                    db.execute(
                        "UPDATE collection_gate SET running_tag = NULL, limited = true, next_call_at = %s WHERE id = 1",
                        (now + delay,),
                    )
                else:
                    # The stored hashtag ID may be the cause, so the next run looks it up again.
                    db.execute(
                        """UPDATE collection_tags SET last_error = %s, hashtag_id = NULL,
                           due_at = coalesce(%s, due_at), top_due_at = coalesce(%s, top_due_at) WHERE tag = %s""",
                        (
                            error_label(error.status_code),
                            now + interval if mode == "recent" else None,
                            now + TOP_INTERVAL if automatic and mode == "top" else None,
                            tag,
                        ),
                    )
                    db.execute(
                        """UPDATE collection_request_tags SET state = 'failed', completed_at = %s
                           WHERE tag = %s AND request_id = %s AND state = 'pending'""",
                        (now, tag, request_id),
                    )
                    db.execute("UPDATE collection_gate SET running_tag = NULL WHERE id = 1")
            return "deferred" if error.status_code == 429 else "processed"

        now = datetime.now(UTC)
        items = result["items"]
        if not automatic:
            cutoff = now - timedelta(hours=hours)
            items = [
                item for item in items
                if (published := publish_time(item.get("timestamp"))) is not None and published >= cutoff
            ]
        with db.transaction():
            new_count = save_reels(db, tag, items, now)
            db.execute(
                """UPDATE collection_tags SET last_success = %s, last_error = NULL, hashtag_id = %s,
                   due_at = coalesce(%s, due_at), top_due_at = coalesce(%s, top_due_at) WHERE tag = %s""",
                (
                    now, result["hashtagIds"].get(tag),
                    now + interval if mode == "recent" and hours == 24 else None,
                    now + TOP_INTERVAL if automatic and mode == "top" else None,
                    tag,
                ),
            )
            db.execute(
                """UPDATE collection_request_tags SET state = 'succeeded', new_count = %s, completed_at = %s
                   WHERE tag = %s AND request_id = %s AND state = 'pending'""",
                (new_count, now, tag, request_id),
            )
            if search_usage.get() >= 80:
                db.execute(
                    """UPDATE collection_gate SET running_tag = NULL, limited = true,
                       next_call_at = greatest(next_call_at, %s) WHERE id = 1""",
                    (now + timedelta(hours=1),),
                )
            else:
                db.execute("UPDATE collection_gate SET running_tag = NULL WHERE id = 1")
        logger.info("Collected %s %s reels for #%s (%s new)", len(items), mode, tag, new_count)
        return "processed"


async def run_worker():
    while True:
        try:
            with connection(autocommit=True) as db:
                db.execute("UPDATE collection_gate SET worker_seen_at = now() WHERE id = 1")
            await collect_one()
        except Exception:
            logger.exception("Instagram collection worker failed")
        await asyncio.sleep(WORKER_SLEEP)


if __name__ == "__main__":
    configure_logging()
    with connection() as db:
        initialize_collection(db)
    asyncio.run(run_worker())
