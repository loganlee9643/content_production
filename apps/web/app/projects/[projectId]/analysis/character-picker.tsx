"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { request } from "../../../../lib/api";
import styles from "./planning.module.css";

type Character = { id: string; label: string };
type Library = { items: Character[]; nextOffset: number | null };

export default function CharacterPicker({ selected, role, age, staging, onSelect, onDelete, children }: {
  selected: string | null;
  role: string; age: string; staging: string; onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  children: ReactNode;
}) {
  const [items, setItems] = useState<Character[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [action, setAction] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"select" | "manage">("select");
  const [creation, setCreation] = useState<"generate" | "upload">("generate");
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    request<Library>("/characters", { signal: controller.signal, cache: "no-store" })
      .then((data) => {
        if (!controller.signal.aborted) {
          setItems(data.items);
          setNextOffset(data.nextOffset);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setError("캐릭터 목록을 불러오지 못했습니다.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  async function addCharacter(file?: File) {
    setError("");
    setNotice("");
    setAction(file ? "upload" : "generate");
    try {
      const mime = file && (file.type || (/\.png$/i.test(file.name) ? "image/png" : "image/jpeg"));
      if (file && (!mime || !["image/png", "image/jpeg"].includes(mime) || file.size > 4 * 1024 * 1024)) {
        throw new Error("4 MiB 이하의 JPG 또는 PNG 이미지를 선택해 주세요.");
      }
      const item = await request<Character>(file
        ? "/characters/upload?name=" + encodeURIComponent(file.name.slice(0, 100))
        : "/characters/generate", file && mime
        ? { method: "POST", headers: { "Content-Type": mime }, body: file }
        : { method: "POST", body: JSON.stringify({ role, age, staging }) });
      setItems((previous) => [item, ...previous.filter((character) => character.id !== item.id)]);
      setNextOffset((previous) => previous === null ? null : previous + 1);
      if (file) {
        setPendingFile(null);
        if (fileInput.current) fileInput.current.value = "";
      }
      setNotice(item.label + " 이미지가 등록됐습니다. 캐릭터 탭에서 선택할 수 있습니다.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "캐릭터를 저장하지 못했습니다.");
    } finally {
      setAction("");
    }
  }

  async function remove(item: Character) {
    if (!window.confirm(item.label + " 이미지를 등록 목록에서 삭제할까요? 기존 기획에 적용된 이미지는 유지됩니다.")) return;
    setAction("delete");
    setError("");
    setNotice("");
    try {
      await request("/characters/" + encodeURIComponent(item.id), { method: "DELETE" });
      setItems((previous) => previous.filter((character) => character.id !== item.id));
      setNextOffset((previous) => previous === null ? null : Math.max(0, previous - 1));
      onDelete(item.id);
      setNotice("등록 목록에서 이미지를 삭제했습니다.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "캐릭터를 삭제하지 못했습니다.");
    } finally {
      setAction("");
    }
  }

  async function loadMore() {
    if (nextOffset === null) return;
    setAction("list");
    setError("");
    try {
      const data = await request<Library>("/characters?offset=" + nextOffset, { cache: "no-store" });
      setItems((previous) => [...new Map([...previous, ...data.items].map((item) => [item.id, item])).values()]);
      setNextOffset(data.nextOffset);
    } catch {
      setError("다음 캐릭터 목록을 불러오지 못했습니다.");
    } finally {
      setAction("");
    }
  }

  const imageUrl = (id: string) => "/api/characters/" + encodeURIComponent(id) + "/image";
  const selectedItem = items.find((item) => item.id === selected);
  return <div className={styles.characterSettings}>
    <fieldset className={styles.choices} disabled={Boolean(action)}>
      <legend>캐릭터 설정</legend>
      <button type="button" aria-pressed={tab === "select"} onClick={() => {
        setPendingFile(null);
        setTab("select");
      }}>캐릭터</button>
      <button type="button" aria-pressed={tab === "manage"} onClick={() => setTab("manage")}>새 캐릭터</button>
    </fieldset>
    {tab === "select" && selected && <div className={styles.characterSelected}>
      <img src={imageUrl(selected)} alt="선택한 캐릭터" />
      <div><strong>{selectedItem?.label ?? "선택한 캐릭터"}</strong>
        <p>이 이미지를 영상의 모든 장면에 사용합니다.</p></div>
    </div>}
    {loading && <p role="status">캐릭터 목록을 불러오는 중입니다.</p>}
    {!loading && !items.length && !error && <p>등록된 캐릭터가 없습니다.</p>}
    {items.length > 0 && <div className={styles.characterGallery} aria-label="등록된 캐릭터">
      {items.map((item) => tab === "select"
        ? <button key={item.id} disabled={Boolean(action)} type="button"
            aria-pressed={selected === item.id} onClick={() => onSelect(item.id)}>
            <img src={imageUrl(item.id)} alt="" loading="lazy" /><span>{item.label}</span>
          </button>
        : <div key={item.id} className={styles.characterManageCard}>
            <img src={imageUrl(item.id)} alt="" loading="lazy" /><span>{item.label}</span>
            <button type="button" disabled={Boolean(action)} aria-label={item.label + " 삭제"}
              onClick={() => void remove(item)}>삭제</button>
          </div>)}
    </div>}
    {nextOffset !== null && <button type="button" disabled={Boolean(action)}
      onClick={() => void loadMore()}>캐릭터 더 보기</button>}
    {tab === "manage" && <>
      <fieldset className={styles.choices} disabled={Boolean(action)}>
        <legend>새 캐릭터 추가</legend>
        <button type="button" aria-pressed={creation === "generate"} onClick={() => setCreation("generate")}>새 캐릭터 생성</button>
        <button type="button" aria-pressed={creation === "upload"} onClick={() => {
          if (creation !== "upload") {
            setPendingFile(null);
            setCreation("upload");
          }
        }}>이미지 등록</button>
      </fieldset>
      {creation === "generate"
        ? <>
          <fieldset className={styles.characterRoleOptions} disabled={Boolean(action)}>{children}</fieldset>
          <button type="button" disabled={loading || Boolean(action) || !role.trim()}
            onClick={() => void addCharacter()}>{action === "generate" ? "캐릭터 생성 중…" : "캐릭터 생성"}</button>
        </>
        : <div className={styles.characterUpload}>
          <label>이미지 파일
            <input ref={fileInput} type="file" accept="image/jpeg,image/png,.jpg,.jpeg,.png" disabled={Boolean(action)}
              onChange={(event) => setPendingFile(event.currentTarget.files?.[0] ?? null)} />
          </label>
          <button type="button" disabled={loading || !pendingFile || Boolean(action)}
            onClick={() => { if (pendingFile) void addCharacter(pendingFile); }}>
            {action === "upload" ? "등록 중…" : "등록"}
          </button>
        </div>}
    </>}
    {notice && <p role="status">{notice}</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
  </div>;
}
