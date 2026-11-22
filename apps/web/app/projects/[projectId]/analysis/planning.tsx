"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import ReplanningForm, { type Replanning, type ReplanningRecommendation } from "./replanning";
import StoryboardEditor, { type Storyboard } from "./storyboard";
import Production from "./production";
import styles from "./planning.module.css";

type Analysis = {
  hook: string; audience: string; visual_pattern: string;
  reusable_formula: string; summary: string; transcript: string;
  recommendations?: ReplanningRecommendation;
};

export type PlanningProject = {
  url: string; category: string; notes: string;
  caption: string | null; username: string | null;
  views: number | null; likes: number | null; comments: number | null;
  mediaUrl: string | null; thumbnailUrl: string | null; analysis: Analysis | null;
  replanning: Replanning | null;
  storyboard: Storyboard | null;
  activeCharacterId: string | null;
};

const fields = [
  ["후킹", "hook"], ["타깃", "audience"],
  ["비주얼 패턴", "visual_pattern"], ["재사용 공식", "reusable_formula"],
] as const;
const stages = ["레퍼런스", "내용 분석", "재기획 설정", "대본 완성"];
const numberFormat = new Intl.NumberFormat("ko-KR");

function recommendationsFrom(analysis: Analysis): ReplanningRecommendation {
  return analysis.recommendations ?? {
    direction: ("새 주제에 아래 전개 방식을 적용하세요.\n" + analysis.reusable_formula).slice(0, 1200),
    audience: analysis.audience.slice(0, 500),
    staging: analysis.visual_pattern.slice(0, 500),
    include_content: ("첫 장면에서 새 주제의 핵심을 한 문장으로 전달하세요.\n참고할 후킹: " + analysis.hook).slice(0, 1000),
    exclude_phrases: "원본 문구의 반복, 과장된 단정, 주제와 무관한 참여 유도",
  };
}

export default function Planning({ projectId, project }: { projectId: string; project: PlanningProject }) {
  const [analysis, setAnalysis] = useState(project.analysis);
  const [step, setStep] = useState(project.storyboard ? 3 : 1);
  const [storyboard, setStoryboard] = useState(project.storyboard);
  const [savedStoryboard, setSavedStoryboard] = useState(project.storyboard);
  const [productionRefresh, setProductionRefresh] = useState(0);
  const [revealStoryboard, setRevealStoryboard] = useState(0);

  useEffect(() => {
    if (revealStoryboard) document.getElementById("storyboard")?.scrollIntoView({ block: "start" });
  }, [revealStoryboard]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [showFile, setShowFile] = useState(!project.mediaUrl);
  const [videoFailed, setVideoFailed] = useState(false);
  const [embed, setEmbed] = useState(false);
  const metrics = [["조회", project.views], ["좋아요", project.likes], ["댓글", project.comments]] as const;

  async function analyze() {
    setError("");
    setBusy(true);
    try {
      if (file && file.size > 8 * 1024 * 1024) {
        throw new Error("8 MiB 이하의 MP4 파일을 사용해 주세요.");
      }
      const response = await fetch("/api/projects/" + encodeURIComponent(projectId) + "/video-analysis", {
        method: "POST",
        ...(file ? { headers: { "Content-Type": "video/mp4" }, body: file } : {}),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        if (response.status === 413 || response.status === 422 || !project.mediaUrl) setShowFile(true);
        if (response.status === 409 && typeof data?.detail === "string" && data.detail.includes("MP4")) {
          setShowFile(true);
        }
        throw new Error(typeof data?.detail === "string" ? data.detail : "영상 분석 요청에 실패했습니다.");
      }
      setAnalysis(data.analysis);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "영상 분석에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  }

  return <main className={styles.page}>
    <Link href="/references" className={styles.back}>← 레퍼런스</Link>
    <div className={styles.heading}>
      <h1>영상 재기획</h1>
      {analysis && <span>분석 결과 저장됨</span>}
    </div>
    <ol className={styles.stages} aria-label="영상 재기획 진행 단계">
      {stages.map((stage, index) => <li key={stage} aria-current={index === step ? "step" : undefined}
        data-complete={index === 0 || (index === 1 && Boolean(analysis)) || (index === 2 && Boolean(storyboard))}>
        <span aria-hidden="true">{index === 0 || (index === 1 && analysis) || (index === 2 && storyboard) ? "✓" : index + 1}</span>
        {index === 2 && analysis
          ? <a href="#replanning-settings" onClick={() => setStep(2)}>{stage}</a>
          : index === 3 && storyboard
            ? <a href="#storyboard" onClick={() => setStep(3)}>{stage}</a>
            : stage}
      </li>)}
    </ol>

    <section className={styles.reference} aria-label="선택한 레퍼런스">
      <div className={styles.cover}>
        {project.mediaUrl && !videoFailed
          ? <video controls playsInline preload="metadata" poster={project.thumbnailUrl ?? undefined}
              src={project.mediaUrl + "#t=0.1"} onError={() => { setVideoFailed(true); setShowFile(true); }} />
          : embed
            ? <iframe title="인스타그램 릴스 원본" src={project.url.replace(/\/?$/, "/") + "embed/"} />
            : <button type="button" onClick={() => setEmbed(true)} className={styles.preview}>
                {project.thumbnailUrl && <img src={project.thumbnailUrl} alt="" />}
                <span>▷ 원본 미리보기</span>
              </button>}
      </div>
      <div className={styles.referenceBody}>
        <div className={styles.referenceTop}>
          <span className={styles.category}>{project.category}</span>
          <a href={project.url} target="_blank" rel="noreferrer">원본 보기 ↗</a>
        </div>
        <h2>{project.username ? "@" + project.username : "Instagram 릴스"}</h2>
        {project.caption && <p>{project.caption}</p>}
        {project.notes && <p><strong>관찰 메모</strong> {project.notes}</p>}
        <dl className={styles.metrics}>
          {metrics.map(([label, value]) => <div key={label}>
            <dt>{label}</dt><dd>{value == null ? "미확인" : numberFormat.format(value)}</dd>
          </div>)}
        </dl>
        <div className={styles.actions}>
          <button type="button" disabled={busy} onClick={() => void analyze()}>
            {busy ? "영상 분석 중…" : analysis ? "다시 분석" : "분석하기"}
          </button>
          {showFile && <label>분석할 MP4
            <input type="file" accept="video/mp4,.mp4" disabled={busy}
              onChange={(event) => setFile(event.currentTarget.files?.[0] ?? null)} />
          </label>}
          <span className={styles.status} role="status">{busy && "영상의 화면과 음성을 분석하고 있습니다."}</span>
          {error && <p className={styles.error} role="alert">{error}</p>}
        </div>
      </div>
    </section>

    {analysis && <section className={styles.analysis} aria-labelledby="analysis-title">
      <div className={styles.analysisHeading}>
        <h2 id="analysis-title">영상 구조 분석</h2><span>분석 완료</span>
      </div>
      <div className={styles.analysisGrid}>{fields.map(([label, field]) => <article key={field}>
        <h3>{label}</h3><p>{analysis[field]}</p>
      </article>)}</div>
      <p className={styles.summary}>{analysis.summary}</p>
      <details className={styles.transcript}>
        <summary>원본 대본</summary>
        <p>{analysis.transcript || "영상에서 확인된 대사가 없습니다."}</p>
      </details>
      <div className={styles.formFooter}>
        <a className={styles.nextStep} href="#replanning-settings" onClick={() => setStep(2)}>재기획 설정으로 이동 ↓</a>
      </div>
    </section>}
    {analysis && <ReplanningForm projectId={projectId} initial={project.replanning}
      activeCharacterId={project.activeCharacterId}
      onSettingsSaved={() => setProductionRefresh((value) => value + 1)}
      recommendations={recommendationsFrom(analysis)} hasStoryboard={Boolean(storyboard)}
      onGenerated={(draft) => {
        setStoryboard(draft);
        setSavedStoryboard(draft);
        setStep(3);
        setRevealStoryboard((value) => value + 1);
      }} />}
    {storyboard && <StoryboardEditor key={revealStoryboard} projectId={projectId}
      draft={storyboard} onChange={setStoryboard} onSaved={setSavedStoryboard} />}
    {savedStoryboard && <Production key={savedStoryboard.revision + ":" + productionRefresh} projectId={projectId} draft={savedStoryboard} />}
  </main>;
}
