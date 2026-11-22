"use client";

import { FormEvent, useState } from "react";
import styles from "./planning.module.css";
import type { Storyboard } from "./storyboard";
import CharacterPicker from "./character-picker";

const roles = ["무속인", "선녀", "양복 입은 상담가", "친근한 언니"] as const;
const tones = ["기본", "차분한 상담가", "친근한 언니", "단호한 전문가", "신비로운 해설자"] as const;
const custom = "직접 입력";

export type Replanning = {
  direction: string; audience: string; staging: string;
  role: string; age: "청년" | "중년" | "노년";
  sections: 4 | 8; duration: 40 | 44 | 48; tone: string;
  hook: "궁금증형" | "경고형" | "반전형" | "공감형";
  shaman_tone: boolean; cta: "없음" | "저장" | "댓글" | "팔로우";
  include_content: string; exclude_phrases: string;
  character_mode?: "generate" | "reference" | null;
  character_id?: string | null;
};

const recommendationFields = ["direction", "audience", "staging", "include_content", "exclude_phrases"] as const;
export type ReplanningRecommendation = Pick<Replanning, (typeof recommendationFields)[number]>;

const defaults: Replanning = {
  direction: "", audience: "", staging: "", role: "무속인", age: "중년",
  sections: 4, duration: 44, tone: "기본", hook: "궁금증형", shaman_tone: false, cta: "없음",
  include_content: "", exclude_phrases: "",
};

function Choices<T extends string | number>({ label, value, choices, onChange, format = String }: {
  label: string; value: T; choices: readonly T[]; onChange: (value: T) => void; format?: (value: T) => string;
}) {
  return <fieldset className={styles.choices}>
    <legend>{label}</legend>
    {choices.map((choice) => <button key={choice} type="button"
      aria-pressed={value === choice} onClick={() => onChange(choice)}>{format(choice)}</button>)}
  </fieldset>;
}

export default function ReplanningForm({ projectId, initial, recommendations, hasStoryboard, onGenerated, onSettingsSaved, activeCharacterId }: {
  projectId: string; initial: Replanning | null; recommendations: ReplanningRecommendation;
  hasStoryboard: boolean; onGenerated: (draft: Storyboard) => void;
  onSettingsSaved: () => void;
  activeCharacterId: string | null;
}) {
  const [values, setValues] = useState<Replanning>(() => ({
    ...(initial ?? { ...defaults, ...recommendations }),
    character_mode: initial?.character_mode ?? (activeCharacterId ? "reference" : "generate"),
    character_id: initial?.character_mode ? initial.character_id ?? null : activeCharacterId,
  }));
  const [appliedCharacterId, setAppliedCharacterId] = useState(activeCharacterId);
  const [customRole, setCustomRole] = useState(!roles.some((role) => role === values.role));
  const [customTone, setCustomTone] = useState(!tones.some((tone) => tone === values.tone));
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const canFill = recommendationFields.some((field) => !values[field].trim() && recommendations[field].trim());

  function fillRecommendations() {
    setValues((previous) => {
      const next = { ...previous };
      for (const field of recommendationFields) {
        if (!next[field].trim()) next[field] = recommendations[field];
      }
      return next;
    });
    setMessage("");
  }

  function update<T extends keyof Replanning>(field: T, value: Replanning[T]) {
    setValues((previous) => ({ ...previous, [field]: value }));
    setMessage("");
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
    const generate = submitter?.value === "generate";
    if (generate && !values.character_id) {
      setError("기준 캐릭터를 생성하거나 등록한 이미지를 선택해 주세요.");
      return;
    }
    if (generate && hasStoryboard && !window.confirm("편집한 대본을 새로 생성한 대본으로 교체할까요?")) return;
    setGenerating(generate);
    setSaving(true);
    setMessage("");
    setError("");
    try {
      const endpoint = generate ? "/storyboard" : "/replanning";
      const response = await fetch("/api/projects/" + encodeURIComponent(projectId) + endpoint, {
        method: generate ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...values, role: values.role.trim(), tone: values.tone.trim() }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.detail === "string" ? data.detail : "입력값을 확인하고 다시 저장해 주세요.");
      }
      if (values.character_id) setAppliedCharacterId(values.character_id);
      if (generate) onGenerated(data.storyboard);
      else {
        setMessage("재기획 설정이 저장됐습니다.");
        onSettingsSaved();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "설정을 저장하지 못했습니다.");
    } finally {
      setSaving(false);
      setGenerating(false);
    }
  }

  return <section id="replanning-settings" className={styles.replanning} aria-labelledby="replanning-title">
    <h2 id="replanning-title" tabIndex={-1}>재기획 설정</h2>
    <div className={styles.recommendations}>
      <p>영상 분석을 바탕으로 작성한 추천입니다. 기획에 맞게 수정할 수 있습니다.</p>
      <button type="button" disabled={!canFill || saving} onClick={fillRecommendations}>빈 항목에 추천 적용</button>
    </div>
    <form onSubmit={save}>
      <fieldset disabled={saving} className={styles.formGrid}>
        <label className={styles.fullField}>기획 방향
          <textarea rows={3} maxLength={4000} placeholder="예: 불안을 자극하지 않고 이직 시기를 판단할 기준을 제시"
            value={values.direction} onChange={(event) => update("direction", event.target.value)} />
        </label>
        <label>타깃 시청자
          <textarea rows={3} maxLength={500} placeholder="예: 이직을 고민하는 30대 직장인"
            value={values.audience} onChange={(event) => update("audience", event.target.value)} />
        </label>
        <label>촬영·연출 방향
          <textarea rows={3} maxLength={500} placeholder="예: 정면 상담 구도, 따뜻한 조명"
            value={values.staging} onChange={(event) => update("staging", event.target.value)} />
        </label>
        <div className={styles.fullField}>
          <CharacterPicker selected={values.character_id ?? null}
            role={values.role} age={values.age} staging={values.staging}
            onSelect={(id) => {
              setValues((previous) => ({ ...previous, character_mode: "reference", character_id: id }));
              setMessage("");
            }}
            onDelete={(id) => {
              if (values.character_id === id && appliedCharacterId !== id) update("character_id", null);
            }}>
        <div>
          <Choices label="캐릭터 역할" choices={[...roles, custom]} value={customRole ? custom : values.role}
            onChange={(value) => {
              setCustomRole(value === custom);
              update("role", value === custom ? "" : value);
            }} />
          {customRole && <input aria-label="캐릭터 역할 직접 입력" required maxLength={100}
            value={values.role} onChange={(event) => {
              update("role", event.target.value);
            }} />}
        </div>
        <Choices label="캐릭터 나이" choices={["청년", "중년", "노년"]} value={values.age}
          onChange={(value) => {
            update("age", value);
          }} />
          </CharacterPicker>
        </div>
        <Choices label="섹션 구성" choices={[4, 8]} value={values.sections} format={(value) => value + "섹션"}
          onChange={(value) => update("sections", value)} />
        <Choices label="전체 길이" choices={[40, 44, 48]} value={values.duration} format={(value) => value + "초 내외"}
          onChange={(value) => update("duration", value)} />
        <div>
          <Choices label="말투" choices={[...tones, custom]} value={customTone ? custom : values.tone}
            onChange={(value) => { setCustomTone(value === custom); update("tone", value === custom ? "" : value); }} />
          {customTone && <input aria-label="말투 직접 입력" required maxLength={100}
            value={values.tone} onChange={(event) => update("tone", event.target.value)} />}
          <label className={styles.toggle}><input type="checkbox" checked={values.shaman_tone}
            onChange={(event) => update("shaman_tone", event.target.checked)} />무당 말투 추가</label>
        </div>
        <Choices label="첫 장면 후킹" choices={["궁금증형", "경고형", "반전형", "공감형"]} value={values.hook}
          onChange={(value) => update("hook", value)} />
        <Choices label="마지막 CTA" choices={["없음", "저장", "댓글", "팔로우"]} value={values.cta}
          onChange={(value) => update("cta", value)} />
        <label>꼭 넣을 내용
          <textarea rows={3} maxLength={2000} placeholder="반드시 포함할 핵심 메시지" value={values.include_content}
            onChange={(event) => update("include_content", event.target.value)} />
        </label>
        <label>빼고 싶은 표현
          <textarea rows={3} maxLength={2000} placeholder="예: 무조건, 대박, 안 보면 손해" value={values.exclude_phrases}
            onChange={(event) => update("exclude_phrases", event.target.value)} />
        </label>
      </fieldset>
      <div className={styles.formFooter}>
        {message && <span role="status">{message}</span>}
        {error && <span role="alert" className={styles.error}>{error}</span>}
        <button type="submit" disabled={saving}>{saving && !generating ? "저장 중…" : "설정 저장"}</button>
        <button type="submit" value="generate" disabled={saving}>
          {generating ? "대본 생성 중…" : hasStoryboard ? "재기획 다시 생성" : "재기획 생성"}
        </button>
      </div>
    </form>
  </section>;
}
