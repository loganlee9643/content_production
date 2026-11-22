"use client";

import { useState } from "react";
import styles from "./planning.module.css";

type Scene = {
  title: string; narration: string; overlay: string;
  scene_direction: string; motion_direction: string;
};

export type Storyboard = {
  title: string; direction: string; scenes: Scene[]; durations: number[]; cta: string; revision: string;
};

const labels = [
  ["낭독 대본", "narration", 700], ["화면 문구", "overlay", 300],
  ["장면 지시", "scene_direction", 1200], ["모션 지시", "motion_direction", 1200],
] as const;

// Same values as apps/backend/narration_timing.py.
const SPOKEN_UNITS_PER_SECOND = 4.5;
const CHARACTERS_PER_SECOND = 5.25;
const ELEVEN_SECOND_RANGE = [57, 54, 60] as const;

function timing(draft: Storyboard, index: number) {
  const narration = draft.scenes[index].narration.trim();
  const cta = index === draft.scenes.length - 1 ? draft.cta.trim() : "";
  const text = cta && !narration.includes(cta) ? narration + "\n" + cta : narration;
  const words = text.match(/[\p{L}\p{N}]/gu)?.length ?? 0;
  const numbers = (text.match(/[0-9]+/g) ?? []).reduce((sum, number) => sum + Math.max(2, number.length), 0);
  const pauses = text.trim().match(/[.!?。！？,，\n]+/g)?.length ?? 0;
  const seconds = draft.durations[index];
  const [elevenTarget, elevenMin, elevenMax] = ELEVEN_SECOND_RANGE;
  const target = seconds === 11 ? elevenTarget : Math.floor(seconds * CHARACTERS_PER_SECOND);
  const tolerance = Math.floor(seconds / 2) + 2;
  return {
    characters: Array.from(text.replace(/\s+/gu, " ")).length,
    min: seconds === 11 ? elevenMin : target - tolerance,
    max: seconds === 11 ? elevenMax : target + tolerance,
    units: words + numbers + pauses, limit: Math.max(1, Math.floor(seconds * SPOKEN_UNITS_PER_SECOND)),
  };
}

export default function StoryboardEditor({ projectId, draft, onChange, onSaved }: {
  projectId: string; draft: Storyboard; onChange: (draft: Storyboard) => void;
  onSaved: (draft: Storyboard) => void;
}) {
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [shortening, setShortening] = useState<number | null>(null);
  const [error, setError] = useState("");
  const total = draft.durations.reduce((sum, duration) => sum + duration, 0);

  function change(next: Storyboard) {
    setSaved(false);
    setDirty(true);
    onChange(next);
  }

  function editScene(index: number, field: keyof Scene, value: string) {
    change({ ...draft, scenes: draft.scenes.map((scene, i) => i === index ? { ...scene, [field]: value } : scene) });
  }

  async function shorten(index: number) {
    setError("");
    setShortening(index);
    try {
      const response = await fetch("/api/projects/" + encodeURIComponent(projectId) + "/storyboard/scenes/" + index + "/shorten", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: draft.revision, narration: draft.scenes[index].narration, cta: draft.cta }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.detail === "string" ? data.detail : "대본을 줄이지 못했습니다.");
      }
      change({
        ...draft,
        scenes: draft.scenes.map((scene, i) => i === index ? { ...scene, narration: data.narration } : scene),
        cta: index === draft.scenes.length - 1 ? data.cta : draft.cta,
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "대본을 줄이지 못했습니다.");
    } finally {
      setShortening(null);
    }
  }

  async function save() {
    setSaving(true);
    setSaved(false);
    setError("");
    try {
      const response = await fetch("/api/projects/" + encodeURIComponent(projectId) + "/storyboard", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.detail === "string" ? data.detail : "장면 제목과 낭독 대본을 확인해 주세요.");
      }
      onChange(data.storyboard);
      onSaved(data.storyboard);
      setSaved(true);
      setDirty(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "스토리보드를 저장하지 못했습니다.");
    } finally {
      setSaving(false);
    }
  }

  return <section id="storyboard" className={styles.storyboard} aria-labelledby="storyboard-title">
    <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <fieldset disabled={saving || shortening !== null} className={styles.storyboardFields}>
        <header className={styles.storyboardHeader}>
          <div>
            <h2 id="storyboard-title">{draft.scenes.length}섹션 스토리보드</h2>
            <p>{total}초</p>
            <label>제목<input required maxLength={150} value={draft.title}
              onChange={(event) => change({ ...draft, title: event.target.value })} /></label>
          </div>
          <label>콘텐츠 방향<textarea rows={3} maxLength={1500} value={draft.direction}
            onChange={(event) => change({ ...draft, direction: event.target.value })} /></label>
        </header>
        <div className={styles.storyboardGrid}>
          {draft.scenes.map((scene, index) => {
            const marker = draft.scenes.length === 4 ? ["기", "승", "전", "결"][index] : String(index + 1);
            const length = timing(draft, index);
            const tooLong = length.units > length.limit || length.characters > length.max;
            const tooShort = length.characters < length.min;
            const includesCta = index === draft.scenes.length - 1 && Boolean(draft.cta.trim());
            return <article className={styles.scene} key={index} aria-label={marker + " 장면"}>
              <div className={styles.sceneHeader}>
                <span className={styles.sceneMarker} aria-hidden="true">{marker}</span>
                <input aria-label={marker + " 장면 제목"} required maxLength={150} value={scene.title}
                  onChange={(event) => editScene(index, "title", event.target.value)} />
                <span>{draft.durations[index]}초</span>
              </div>
              <div className={styles.sceneBody}>
                {scene.narration.trim() && (tooLong || tooShort) && <div className={styles.timingWarning}>
                  <span>{length.units > length.limit && length.characters <= length.max
                    ? `예상 발화 분량 초과 (${length.units}/${length.limit})`
                    : tooLong ? "낭독 분량 초과" : "낭독 분량이 짧습니다"} · {length.characters}자 / 목표 {length.min}~{length.max}자{includesCta ? " · CTA 포함" : ""}</span>
                  <button type="button" onClick={() => void shorten(index)}>
                    {shortening === index ? "조정 중…" : "장면 길이에 맞게 조정"}
                  </button>
                </div>}
                {labels.map(([label, field, max]) => <label key={field} data-field={field}>
                  <span>{label}{field === "narration" && <small>{length.characters}자 / 목표 {length.min}~{length.max}자{includesCta ? " · CTA 포함" : ""}</small>}</span>
                  <textarea rows={3} required={field === "narration"} maxLength={max} value={scene[field]}
                    onChange={(event) => editScene(index, field, event.target.value)} />
                </label>)}
                {index === draft.scenes.length - 1 && <label data-field="overlay">
                  <span>마지막 CTA</span><textarea rows={2} maxLength={300} value={draft.cta}
                    onChange={(event) => change({ ...draft, cta: event.target.value })} />
                </label>}
              </div>
            </article>;
          })}
        </div>
      </fieldset>
      <div className={styles.formFooter}>
        {saved && <span role="status">스토리보드를 저장했습니다.</span>}
        {error && <span role="alert" className={styles.error}>{error}</span>}
        <button type="submit" disabled={saving || shortening !== null}>{saving ? "저장 중…" : "스토리보드 저장"}</button>
        {!dirty && <a className={styles.nextStep} href="#production">영상 제작으로 이동 ↓</a>}
      </div>
    </form>
  </section>;
}
