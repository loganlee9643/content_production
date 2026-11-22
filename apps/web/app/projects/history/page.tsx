import Link from "next/link";
import styles from "./history.module.css";

type Export = { assetId: string; createdAt: string | null; current: boolean };
type Project = {
  id: string; title: string | null; shortcode: string; category: string;
  url: string; created_at: string; updated_at: string; analyzed_at: string | null;
  planned_at: string | null; storyboard_at: string | null; completed_clips: number;
  scene_count: number; has_current_export: boolean; exports: Export[];
};
type History = { items: Project[]; nextOffset: number | null };

const dateTime = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", dateStyle: "medium", timeStyle: "short",
});

function stage(project: Project) {
  if (project.has_current_export) return "최종 영상 완료";
  if (project.completed_clips > 0) return `영상 제작 · ${project.completed_clips}/${project.scene_count}개 클립`;
  if (project.storyboard_at) return "스토리보드 생성";
  if (project.planned_at) return "재기획 설정 저장";
  if (project.analyzed_at) return "분석 완료";
  return "레퍼런스 선택";
}

function historyUrl(q: string, offset: number) {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (offset) params.set("offset", String(offset));
  return "/projects/history" + (params.size ? "?" + params : "");
}

export default async function HistoryPage({ searchParams }: {
  searchParams: Promise<{ q?: string | string[]; offset?: string | string[] }>;
}) {
  const query = await searchParams;
  const q = typeof query.q === "string" ? query.q.trim().slice(0, 100) : "";
  const rawOffset = typeof query.offset === "string" ? Number(query.offset) : 0;
  const offset = Number.isSafeInteger(rawOffset) && rawOffset > 0 ? Math.min(rawOffset, 2_147_483_627) : 0;
  const backend = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";
  let history: History | undefined;
  try {
    const params = new URLSearchParams({ q, offset: String(offset) });
    const response = await fetch(backend + "/api/project-history?" + params, { cache: "no-store" });
    if (response.ok) history = await response.json();
  } catch {
    // Keep navigation and search available if the backend cannot be reached.
  }
  return <main className={styles.page}>
    <Link href="/references" className={styles.back}>← 레퍼런스</Link>
    <header className={styles.heading}>
      <h1>작업 내역</h1>
      <form action="/projects/history" className={styles.search}>
        <input name="q" aria-label="프로젝트 제목 또는 카테고리 검색" placeholder="프로젝트 제목 또는 카테고리"
          defaultValue={q} maxLength={100} type="search" />
        <button>검색</button>
      </form>
    </header>
    {!history && <p role="alert">작업 내역을 불러오지 못했습니다. <Link href={historyUrl(q, offset)}>다시 시도</Link></p>}
    {history && history.items.length === 0 && <p>{q ? "검색 조건에 맞는 작업이 없습니다." : "표시할 작업 내역이 없습니다."}</p>}
    {history && <div className={styles.list}>
      {history.items.map((project) => {
        const projectPath = "/projects/" + encodeURIComponent(project.id);
        const title = project.title || `${project.category} · ${project.shortcode}`;
        return <article key={project.id} className={styles.card}>
          <div className={styles.details}>
            <span className={styles.category}>{project.category}</span>
            <h2><Link href={projectPath + "/analysis"}>{title}</Link></h2>
            <p className={styles.timestamps}>
              <span>{stage(project)}</span>
              <span>최근 작업 <time dateTime={project.updated_at}>{dateTime.format(new Date(project.updated_at))}</time></span>
            </p>
          </div>
          <div className={styles.actions}>
            <a href={project.url} target="_blank" rel="noreferrer">원본 릴스 ↗</a>
            <Link className={styles.open} href={projectPath + "/analysis" + (project.storyboard_at ? "#storyboard"
              : project.planned_at ? "#replanning-settings" : "")}>작업 열기</Link>
          </div>
          {project.exports.length > 0 && <div className={styles.exports}>
            <h3>저장된 최종 영상</h3>
            <ul>{project.exports.map((item, index) => <li key={item.assetId}>
              <span>{item.current ? "현재 스토리보드" : `저장본 ${index + 1}`}</span>
              {item.createdAt && <time dateTime={item.createdAt}>{dateTime.format(new Date(item.createdAt))}</time>}
              <a href={"/api" + projectPath + "/production/media/" + encodeURIComponent(item.assetId) + "?download=true"}>
                MP4 다운로드
              </a>
            </li>)}</ul>
          </div>}
        </article>;
      })}
    </div>}
    {(offset > 0 || history?.nextOffset != null) && <nav className={styles.pagination} aria-label="작업 내역 페이지">
      {offset > 0 && <Link href={historyUrl(q, Math.max(0, offset - 20))}>이전</Link>}
      {history?.nextOffset != null && <Link href={historyUrl(q, history.nextOffset)}>다음</Link>}
    </nav>}
  </main>;
}
