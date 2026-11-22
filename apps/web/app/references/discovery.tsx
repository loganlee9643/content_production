"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { request } from "../../lib/api";
import styles from "./discovery.module.css";

const topics = ["사주 풀이", "오늘의 운세", "띠별 운세", "재물·사업", "연애·궁합", "대운·신년", "신점·타로"];

type Reel = {
  id: string;
  permalink: string;
  tag: string;
  caption?: string;
  timestamp?: string;
  thumbnail_url?: string;
  media_url?: string;
  username?: string;
  like_count?: number;
  comments_count?: number;
};

const dateFormat = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", year: "numeric", month: "short", day: "numeric",
});
const countFormat = new Intl.NumberFormat("ko-KR");
const collectionTime = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", month: "long", day: "numeric",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

type CollectionRequest = {
  id: string; status: "queued" | "running" | "completed" | "partial" | "failed" | "cancelled";
  newCount: number; completedAt: string | null; hours: number;
};

type TopicState = {
  name: string; status: "empty" | "partial" | "ready" | "running" | "delayed" | "stopped" | "token_expired";
  lastCollectedAt: string | null; lastActivityAt: string | null; nextCollectAt: string;
  nextAutomaticAt: string;
  collectedTagCount: number; activeTagCount: number; request: CollectionRequest | null;
};

export default function Discovery() {
  const router = useRouter();
  const [topic, setTopic] = useState("띠별 운세");
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("띠별 운세");
  const [order, setOrder] = useState("recent");
  const [days, setDays] = useState(7);
  const [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState<Reel[]>();
  const [topicStates, setTopicStates] = useState<TopicState[]>([]);
  const [requesting, setRequesting] = useState(false);
  const [collectionOptionsOpen, setCollectionOptionsOpen] = useState(false);
  const [collectionHours, setCollectionHours] = useState(24);
  const [statusRevision, refreshStatus] = useState(0);
  const [error, setError] = useState("");
  const [pending, setPending] = useState("");
  const [actionError, setActionError] = useState("");
  const [openPreview, setOpenPreview] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setResult(undefined);
    setError("");
    setOpenPreview("");
    if (!query) return;
    const params = new URLSearchParams({
      q: topic ? "" : query, topic, order, days: String(days), analyzable_only: "true",
    });
    request<{ items: Reel[] }>("/references/collected?" + params, {
      cache: "no-store", signal: controller.signal,
    })
      .then(({ items }) => {
        if (!controller.signal.aborted) setResult(items);
      })
      .catch((err: Error) => {
        if (!controller.signal.aborted) setError(err.message);
      });
    return () => controller.abort();
  }, [topic, query, order, days, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    let refreshTimer: ReturnType<typeof setTimeout>;
    let updating = false;
    async function update() {
      if (updating || controller.signal.aborted) return;
      clearTimeout(refreshTimer);
      updating = true;
      let delay = 60000;
      try {
        if (document.visibilityState === "visible") {
          const data = await request<{ topics: TopicState[] }>("/reference-topics", {
            cache: "no-store", signal: controller.signal,
          });
          if (data.topics.some(({ status }) => status === "running")) {
            delay = 3000;
          } else {
            const queued = data.topics.filter(({ request }) => request?.status === "queued");
            if (queued.length) {
              const untilNextCollection = Math.min(...queued.map(({ nextCollectAt }) =>
                Date.parse(nextCollectAt) - Date.now(),
              ));
              if (Number.isFinite(untilNextCollection)) {
                delay = Math.min(60000, Math.max(15000, untilNextCollection));
              }
            }
          }
          if (!controller.signal.aborted) setTopicStates(data.topics);
        }
      } catch {
        delay = 60000;
      } finally {
        updating = false;
        if (!controller.signal.aborted && document.visibilityState === "visible") {
          refreshTimer = setTimeout(update, delay);
        }
      }
    }
    void update();
    document.addEventListener("visibilitychange", update);
    return () => {
      controller.abort();
      clearTimeout(refreshTimer);
      document.removeEventListener("visibilitychange", update);
    };
  }, [statusRevision]);

  const selected = topicStates.find((state) => state.name === topic);
  const collectionVersion = selected?.lastActivityAt;
  const requestCompletedAt = selected?.request?.completedAt;
  useEffect(() => {
    if (collectionVersion || requestCompletedAt) setRefresh((value) => value + 1);
  }, [collectionVersion, requestCompletedAt]);

  async function collectTopic(hours = collectionHours) {
    setRequesting(true);
    setActionError("");
    try {
      const result = await request<CollectionRequest>(
        "/reference-topics/" + encodeURIComponent(topic) + "/collection-requests?period_hours=" + hours,
        { method: "POST" },
      );
      setTopicStates((states) => states.map((state) =>
        state.name === topic ? { ...state, request: result } : state,
      ));
      refreshStatus((value) => value + 1);
      setCollectionOptionsOpen(false);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "수집 요청을 등록하지 못했습니다.");
    } finally {
      setRequesting(false);
    }
  }

  async function cancelRequest(requestId: string) {
    setRequesting(true);
    setActionError("");
    try {
      const result = await request<CollectionRequest>(
        "/reference-topics/" + encodeURIComponent(topic) + "/collection-requests/" + encodeURIComponent(requestId),
        { method: "DELETE" },
      );
      setTopicStates((states) => states.map((state) =>
        state.name === topic ? { ...state, request: result } : state,
      ));
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "수집 요청을 취소하지 못했습니다.");
    } finally {
      refreshStatus((value) => value + 1);
      setRequesting(false);
    }
  }

  function automaticLabel(state: TopicState) {
    if (state.status === "stopped") return "수집 워커 중지됨";
    if (state.status === "token_expired") return "Meta 토큰 만료";
    return Date.parse(state.nextAutomaticAt) <= Date.now()
      ? "자동 수집 예약 지연"
      : "다음 자동 수집 " + collectionTime.format(new Date(state.nextAutomaticAt));
  }

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = input.trim();
    if (!value) return;
    setTopic("");
    setCollectionOptionsOpen(false);
    setQuery(value);
    setRefresh((previous) => previous + 1);
  }

  async function start(reel: Reel) {
    setPending(reel.id);
    setActionError("");
    try {
      const { referenceId } = await request<{ referenceId: string }>("/references/import/instagram", {
        method: "POST",
        body: JSON.stringify({ url: reel.permalink, category: topic || reel.tag }),
      });
      const { projectId } = await request<{ projectId: string }>(
        "/references/" + encodeURIComponent(referenceId) + "/planning-project", { method: "POST" },
      );
      router.push("/projects/" + projectId + "/analysis");
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "프로젝트 생성에 실패했습니다.");
    } finally {
      setPending("");
    }
  }

  const items = result ?? [];
  const collectionRequest = selected?.request;
  const activeRequest = collectionRequest?.status === "queued" || collectionRequest?.status === "running";

  return <section aria-label="인스타그램 릴스 검색">
    <header className={styles.header}>
      <div>
        <h1>숏폼 레퍼런스</h1>
      </div>
      <div className={styles.controls}>
        <form onSubmit={search}>
          <input aria-label="키워드 또는 해시태그" placeholder="키워드 또는 해시태그 검색"
            value={input} maxLength={60} onChange={(event) => setInput(event.target.value)} />
          <button>검색</button>
        </form>
        <button type="button" className={styles.secondary} onClick={() => setRefresh((value) => value + 1)}>새로고침</button>
        <Link href="/projects/history" className={styles.historyLink}>작업 내역</Link>
      </div>
    </header>
    <nav className={styles.topics} aria-label="레퍼런스 주제">
      {topics.map((value) => {
        const state = topicStates.find((item) => item.name === value);
        return <button key={value} type="button" aria-pressed={topic === value}
          onClick={() => { setTopic(value); setQuery(value); setInput(""); setCollectionOptionsOpen(false); }}>
          {value}
          {state && <small className={styles.topicTime}>
            {state.lastCollectedAt ? "최근 " + collectionTime.format(new Date(state.lastCollectedAt))
              : state.lastActivityAt
                ? "일부 수집 " + collectionTime.format(new Date(state.lastActivityAt))
                : "미수집"}
            <br />{automaticLabel(state)}
          </small>}
        </button>;
      })}
    </nav>
    {topic && <div className={styles.collection} aria-live="polite">
      {selected && <span>
        {selected.lastCollectedAt
          ? "마지막 수집 " + collectionTime.format(new Date(selected.lastCollectedAt))
          : selected.collectedTagCount
            ? `일부 수집 (${selected.collectedTagCount}/${selected.activeTagCount}개 태그)`
            : "아직 수집되지 않음"}
        {collectionRequest?.status === "running" || selected.status === "running"
          ? " · 수집 중"
          : collectionRequest?.status === "queued"
            ? selected.status === "delayed"
              ? " · 수동 수집 재시도 예정 " + collectionTime.format(new Date(selected.nextCollectAt))
              : " · 수동 수집 시작 대기 중"
            : " · " + automaticLabel(selected)}
      </span>}
      <button type="button" className={styles.secondary} disabled={requesting || activeRequest || !selected}
        aria-expanded={collectionOptionsOpen}
        onClick={() => setCollectionOptionsOpen((open) => !open)}>
        {requesting ? "요청 중…" : collectionRequest?.status === "queued" ? "수집 대기 중"
          : collectionRequest?.status === "running" ? "수집 중" : "수동 수집"}
      </button>
      {collectionRequest?.status === "queued" && <>
        <button type="button" className={styles.secondary} disabled={requesting}
          onClick={() => void cancelRequest(collectionRequest.id)}>수동 요청 취소</button>
        {selected?.status !== "delayed" && <button type="button" className={styles.secondary}
          disabled={requesting} onClick={() => void collectTopic(collectionRequest.hours)}>
          다시 시작
        </button>}
      </>}
      {collectionOptionsOpen && <form className={styles.collectionOptions} onSubmit={(event) => {
        event.preventDefault();
        void collectTopic();
      }}>
        <label htmlFor="manual-collection-period">수동 수집 기간</label>
        <select id="manual-collection-period" value={collectionHours} disabled={requesting}
          onChange={(event) => setCollectionHours(Number(event.target.value))}>
          <optgroup label="최신 릴스">
            <option value={6}>최근 6시간</option>
            <option value={12}>최근 12시간</option>
            <option value={24}>최근 24시간</option>
          </optgroup>
          <optgroup label="Meta 인기 릴스">
            <option value={168}>최근 7일 인기 결과</option>
            <option value={720}>최근 30일 인기 결과</option>
          </optgroup>
        </select>
        <button type="submit" disabled={requesting || activeRequest}>수집 시작</button>
        <button type="button" className={styles.secondary} disabled={requesting}
          onClick={() => setCollectionOptionsOpen(false)}>취소</button>
        {collectionHours > 24 && <p>선택한 기간에 게시된 Meta 인기 목록의 릴스를 수집합니다.</p>}
      </form>}
      {collectionRequest?.completedAt && <span>
        {collectionRequest.status === "cancelled" ? "수동 수집 취소"
          : collectionRequest.status === "failed" ? "수동 수집 실패"
          : `${collectionRequest.status === "partial" ? "일부 완료" : "수집 완료"} · 신규 ${collectionRequest.newCount}개`}
        {" · " + collectionTime.format(new Date(collectionRequest.completedAt))}
      </span>}
    </div>}
    <div className={styles.filters}>
      <p><strong>{query}</strong>{result && <> · {countFormat.format(items.length)}개 결과</>}</p>
      <div className={styles.controls}>
        <select aria-label="게시 기간" value={days} onChange={(event) => setDays(Number(event.target.value))}>
          <option value={0}>검색 결과 전체 기간</option><option value={7}>최근 7일</option>
          <option value={30}>최근 30일</option><option value={90}>최근 90일</option>
        </select>
        <select aria-label="검색 정렬" value={order} onChange={(event) => setOrder(event.target.value)}>
          <option value="recent">최신순</option><option value="engagement">반응순</option>
        </select>
      </div>
    </div>
    {error && <p role="alert">{error}</p>}
    {actionError && <p role="alert">{actionError}</p>}
    {!error && !result && <p role="status">저장된 릴스를 불러오고 있습니다.</p>}
    {result && items.length === 0 && (
      <div role="status">
        <p>{selected && !selected.lastActivityAt
          ? activeRequest ? "첫 수집이 끝나면 여기에 릴스가 표시됩니다." : "이 주제에서 아직 수집한 릴스가 없습니다."
          : "검색 조건에 맞는 영상 분석 가능 릴스가 없습니다."}</p>
      </div>
    )}
    <div className={styles.grid}>
      {items.map((reel) => <article className={styles.card} key={reel.id}>
        <div className={styles.cover}>
          {openPreview === reel.id
            ? <iframe title="인스타그램 릴스 미리보기" src={reel.permalink.replace(/\/?$/, "/") + "embed/"} />
            : <button className={styles.preview} type="button" aria-label="릴스 미리보기"
                onClick={() => setOpenPreview(reel.id)}>
                {reel.thumbnail_url ? <img src={reel.thumbnail_url} alt="" loading="lazy" referrerPolicy="no-referrer" />
                  : reel.media_url
                    ? <video src={reel.media_url + "#t=0.1"} muted playsInline preload="metadata" aria-hidden="true" />
                    : <span>▷ 릴스 미리보기</span>}
              </button>}
          <span className={styles.tag}>#{reel.tag}</span>
          {openPreview !== reel.id && <div className={styles.metrics}>
            {reel.username && <strong>@{reel.username}</strong>}
            <span>좋아요 {reel.like_count == null ? "—" : countFormat.format(reel.like_count)}</span>
            <span>댓글 {reel.comments_count == null ? "—" : countFormat.format(reel.comments_count)}</span>
          </div>}
        </div>
        <div className={styles.body}>
          <p className={styles.caption}>{reel.caption || "캡션 미확인"}</p>
          <div className={styles.meta}>
            <a href={reel.permalink} target="_blank" rel="noreferrer">원본 보기 ↗</a>
            {reel.timestamp && !Number.isNaN(Date.parse(reel.timestamp)) &&
              <time dateTime={reel.timestamp}>{dateFormat.format(new Date(reel.timestamp))}</time>}
          </div>
          <button type="button" className={styles.plan} disabled={Boolean(pending)} onClick={() => start(reel)}>
            {pending === reel.id ? "기획을 여는 중…" : "이 영상으로 기획"}
          </button>
        </div>
      </article>)}
    </div>
  </section>;
}
