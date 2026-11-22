"use client";

import { useEffect, useState } from "react";
import { request } from "../../../../lib/api";
import type { Storyboard } from "./storyboard";
import styles from "./production.module.css";

type Clip = {
  scene: number; status: "submitting" | "queued" | "running" | "succeeded" | "failed" | "unknown" | "waiting" | "cancelled";
  assetId: string | null; error: string | null;
  matchesCharacter: boolean;
  usage: { totalTokens: number; estimatedUsd: number } | null;
};
type ProductionState = {
  model: string; clips: Clip[]; exportId: string | null; characterVersion: string | null;
  stopRequested: boolean;
  sequence: { status: "running" | "completed" | "stopped"; error: string | null; cut_scenes: number[] } | null;
};
const labels = {
  submitting: "접수 중", queued: "생성 대기", running: "생성 중",
  succeeded: "생성 완료", failed: "생성 실패", unknown: "접수 확인 필요", waiting: "생성 대기", cancelled: "생성 취소됨",
};
const tokenFormat = new Intl.NumberFormat("ko-KR");
const usdFormat = new Intl.NumberFormat("ko-KR", { style: "currency", currency: "USD" });

function isActive(clip: Clip) {
  return clip.status === "submitting" || clip.status === "queued" || clip.status === "running";
}

export default function Production({ projectId, draft }: { projectId: string; draft: Storyboard }) {
  const [state, setState] = useState<ProductionState>();
  const [busy, setBusy] = useState("");
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [continuous, setContinuous] = useState(false);
  const [cutScenes, setCutScenes] = useState<number[]>([]);
  const path = "/projects/" + encodeURIComponent(projectId) + "/production";

  useEffect(() => {
    if (busy === "export" || stopping) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      try {
        const next = await request<ProductionState>(
          path + "?revision=" + encodeURIComponent(draft.revision),
          { signal: controller.signal, cache: "no-store" },
        );
        if (controller.signal.aborted) return;
        setState(next);
        setLoadError("");
        if (busy || next.clips.some(isActive) || next.sequence?.status === "running") timer = setTimeout(read, busy ? 1500 : 5000);
      } catch (cause) {
        if (!controller.signal.aborted) {
          setLoadError(cause instanceof Error ? cause.message : "제작 상태를 불러오지 못했습니다.");
          if (busy) timer = setTimeout(read, 5000);
        }
      }
    }
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [busy, stopping, path, draft.revision, refresh]);

  async function submit(action: "clips" | "export" | "sequence", scene?: number, continuePrevious = false) {
    setBusy(scene === undefined ? action : String(scene));
    setError("");
    try {
      const next = await request<ProductionState>(path + "/" + action, {
        method: "POST", body: JSON.stringify(action === "sequence"
          ? { revision: draft.revision, cut_scenes: cutScenes }
          : { revision: draft.revision, scene, continue_previous: continuePrevious }),
      });
      if (action === "export") setState(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "제작 요청에 실패했습니다.");
    } finally {
      setBusy("");
    }
  }

  async function stop() {
    setStopping(true);
    setError("");
    try {
      setState(await request<ProductionState>(path + "/stop", {
        method: "POST", body: JSON.stringify({ revision: draft.revision }),
      }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "제작 중지 요청에 실패했습니다.");
    } finally {
      setStopping(false);
    }
  }

  const media = (assetId: string) => "/api" + path + "/media/" + encodeURIComponent(assetId);
  const currentClips = state?.clips.filter((clip) => clip.assetId && clip.matchesCharacter) ?? [];
  const count = currentClips.length;
  const recorded = currentClips.flatMap((clip) => clip.usage ? [clip.usage] : []);
  const totals = recorded.reduce((sum, item) => ({
    tokens: sum.tokens + item.totalTokens, usd: sum.usd + item.estimatedUsd,
  }), { tokens: 0, usd: 0 });
  const sequenceRunning = state?.sequence?.status === "running";
  const activeClips = state?.clips.filter(isActive) ?? [];
  const active = activeClips.length > 0 || sequenceRunning;
  const canContinue = state?.model === "Gemini Omni 1.2 Flash" && Boolean(state.characterVersion);
  const needsGeneration = draft.scenes.some((_, index) => {
    const clip = state?.clips.find((item) => item.scene === index);
    return !clip || clip.status === "waiting" || clip.status === "failed" || clip.status === "cancelled" ||
      (clip.status === "succeeded" && !clip.matchesCharacter);
  });

  return <section id="production" className={styles.production} aria-labelledby="production-title">
    <header className={styles.header}>
      <div><h2 id="production-title">영상 제작 {state?.model && <small>{state.model}</small>}</h2>
        <p>장면별 클립을 생성하고 확인한 뒤 최종 영상으로 조립할 수 있습니다.</p></div>
      {active && !state?.stopRequested ? <button type="button" disabled={stopping} onClick={() => void stop()}>
        {stopping ? "중지 요청 중…" : "영상 제작 중지"}
      </button> : <button type="button" disabled={!state || Boolean(busy) || stopping || active || !needsGeneration}
        onClick={() => void submit(continuous && canContinue ? "sequence" : "clips")}>
        {busy === "clips" || busy === "sequence" ? "생성 요청 중…"
          : continuous && canContinue ? "남은 장면 이어서 생성" : "영상 제작 시작"}
      </button>}
    </header>
    {(active || state?.stopRequested || (busy && busy !== "export")) && <div className={styles.progress} role="status">
      {state?.stopRequested
        ? <p>{activeClips.length ? "다음 장면 제출을 중지했습니다. 이미 접수된 장면의 결과를 확인 중입니다."
          : "영상 제작을 중지했습니다. 완성된 클립은 다시 볼 수 있습니다."}</p>
        : <p>{activeClips.length ? "제작 진행 중인 장면" : sequenceRunning ? "다음 장면 준비 중" : "영상 생성 요청 준비 중"}</p>}
      {activeClips.length > 0 && <ul>{activeClips.map((clip) => <li key={clip.scene}>
        {clip.scene + 1}번 · {draft.scenes[clip.scene]?.title} · {labels[clip.status]}
      </li>)}</ul>}
    </div>}
    <div className={styles.character}>
      {state?.characterVersion && <img
        src={"/api" + path + "/character/" + encodeURIComponent(state.characterVersion)}
        alt="저장된 캐릭터 기준 이미지" />}
      <div>
        <h3>캐릭터 기준 이미지</h3>
        <p>{state?.characterVersion ? "재기획 설정에 저장한 캐릭터를 모든 장면에 사용합니다." : "선택한 캐릭터가 없습니다."}</p>
        <a href="#replanning-settings">{state?.characterVersion ? "재기획 설정에서 변경" : "재기획 설정에서 선택"}</a>
      </div>
    </div>
    {canContinue && <div className={styles.continuity}>
      <label><input type="checkbox" checked={continuous || sequenceRunning} disabled={active || Boolean(busy)}
        onChange={(event) => setContinuous(event.target.checked)} />이전 장면 이어서 생성</label>
      {(continuous || sequenceRunning) && <p>앞 장면이 완료되면 다음 장면을 시작합니다. 장소나 구도가 바뀌는 장면은 카드에서 지정하세요.</p>}
      {sequenceRunning && <p role="status">순서대로 생성 중입니다. 화면을 다시 열면 진행 상태를 이어서 확인합니다.</p>}
      {state?.sequence?.status === "stopped" && state.sequence.error && <p role="alert">{state.sequence.error}</p>}
    </div>}
    {loadError && <div className={styles.feedback} role="alert">
      <span>{loadError}</span><button type="button" onClick={() => setRefresh((value) => value + 1)}>상태 다시 확인</button>
    </div>}
    {error && <p className={styles.feedback} role="alert">{error}</p>}
    {!state && !loadError && <p className={styles.feedback} role="status">제작 상태를 불러오는 중입니다.</p>}
    <div className={styles.grid}>
      {draft.scenes.map((scene, index) => {
        const clip = state?.clips.find((item) => item.scene === index);
        const previousClip = state?.clips.find((item) => item.scene === index - 1);
        const marker = draft.scenes.length === 4 ? ["기", "승", "전", "결"][index] : String(index + 1);
        return <article key={index} className={styles.card}>
          <div className={styles.media}>
            {clip?.assetId
              ? <video src={media(clip.assetId)} controls playsInline preload="metadata" aria-label={scene.title} />
              : <div role="status"><span aria-hidden="true">▷</span>{clip ? labels[clip.status] : "생성 대기"}</div>}
            <span className={styles.marker}>{marker}</span>
          </div>
          <div className={styles.body}>
            <h3>{marker} · {scene.title} <small>{draft.durations[index]}초</small></h3>
            <p>{scene.narration}</p>
            {clip?.error && <p className={styles.error}>{clip.error}</p>}
            {clip?.assetId && <p className={styles.cost}>{clip.usage
              ? tokenFormat.format(clip.usage.totalTokens) + " 토큰 · 예상 " + usdFormat.format(clip.usage.estimatedUsd)
              : "생성 사용량 미확인"}</p>}
            {clip?.assetId && !clip.matchesCharacter && <p>이전 기준으로 만든 클립 · 재생성 필요</p>}
            {clip?.assetId && isActive(clip) && <p role="status">{labels[clip.status]} · 기존 클립 표시 중</p>}
            {clip?.assetId && clip.status === "cancelled" && <p>재생성 취소됨 · 기존 클립 표시 중</p>}
            {clip?.assetId && clip.status === "waiting" && <p>스토리보드 변경 전 클립 · 변경 내용을 반영하려면 다시 생성하세요.</p>}
            {state && <button type="button"
              disabled={Boolean(busy) || stopping || (active && Boolean(state.stopRequested)) || sequenceRunning || Boolean(clip && isActive(clip))}
              onClick={() => {
                if (clip?.status === "unknown" && !window.confirm("앞선 생성 요청이 접수됐을 수 있습니다. 공급사 작업 내역을 확인한 뒤 다시 생성하시겠어요?")) return;
                void submit("clips", index);
              }}>
              {busy === String(index) ? "요청 중…" : clip?.assetId ? "이 클립 다시 생성" : clip ? "이 클립 재시도" : "이 클립 생성"}
            </button>}
            {canContinue && index > 0 && <div className={styles.sceneContinuity}>
              {continuous && <label><input type="checkbox" checked={cutScenes.includes(index)}
                disabled={active || Boolean(busy)} onChange={(event) => {
                  const checked = event.target.checked;
                  setCutScenes((previous) => checked ? [...previous, index] : previous.filter((item) => item !== index));
                }} />이 장면은 기준 이미지만 사용</label>}
              {previousClip?.assetId && previousClip.matchesCharacter && !cutScenes.includes(index) &&
                <button type="button" disabled={Boolean(busy) || active}
                  onClick={() => {
                    if ((clip?.assetId || clip?.status === "unknown") &&
                      !window.confirm("이전 장면을 참조해 이 클립을 새로 생성할까요?")) return;
                    void submit("clips", index, true);
                  }}>이전 장면 참조로 생성</button>}
            </div>}
          </div>
        </article>;
      })}
    </div>
    <div className={styles.export}>
      <div className={styles.media}>
        {state?.exportId
          ? <video src={media(state.exportId)} controls playsInline preload="metadata" aria-label="최종 영상" />
          : <div><span aria-hidden="true">▤</span>최종 영상<small>완료 클립 {count}/{draft.scenes.length}</small></div>}
      </div>
      <div>
        {state?.exportId && currentClips.some((clip) => clip.status === "waiting") &&
          <p>스토리보드 변경 전 최종 영상입니다.</p>}
        <h3>클립 순서대로 최종 영상 조립</h3>
        <p>모든 장면의 클립이 완료되면 9:16 MP4로 저장할 수 있습니다.</p>
        {count > 0 && <div className={styles.costTotal}>
          <strong>{state?.exportId ? "최종 영상의 클립 생성비" : "조립 대상 클립 생성비"}</strong>
          {recorded.length > 0 && <p>{tokenFormat.format(totals.tokens)} 토큰 · 예상 {usdFormat.format(totals.usd)}</p>}
          {recorded.length < count && <p>사용량 확인 {recorded.length}/{count}개 클립</p>}
          <p>공개 요율 기준 추정치이며 분석·대본·이전 생성 비용은 제외됩니다.</p>
        </div>}
        <button type="button" disabled={Boolean(busy) || active || count !== draft.scenes.length}
          onClick={() => void submit("export")}>{busy === "export" ? "조립 중…" : "최종 영상 조립"}</button>
        {state?.exportId && <a href={media(state.exportId) + "?download=true"}>MP4 다운로드</a>}
      </div>
    </div>
  </section>;
}
