"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { request, type Reference } from "../../../lib/api";

type Preview = { shortcode: string; canonicalUrl: string; previewStatus: string };

export default function SavedReferencesPage() {
  const router = useRouter();
  const [items, setItems] = useState<Reference[]>([]);
  const [query, setQuery] = useState("");
  const [url, setUrl] = useState("");
  const [notes, setNotes] = useState("");
  const [category, setCategory] = useState("");
  const [preview, setPreview] = useState<Preview>();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [listError, setListError] = useState("");
  const [revision, refresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    request<Reference[]>("/references?q=" + encodeURIComponent(query), { signal: controller.signal })
      .then((rows) => { setItems(rows); setListError(""); })
      .catch(() => { if (!controller.signal.aborted) setListError("목록을 불러오지 못했습니다."); });
    return () => controller.abort();
  }, [query, revision]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setNotice("");
    try { await action(); }
    catch (error) { setNotice(error instanceof Error ? error.message : "요청에 실패했습니다."); }
    finally { setBusy(false); }
  }

  async function check(event: FormEvent) {
    event.preventDefault();
    setPreview(undefined);
    await run(async () => setPreview(await request<Preview>("/references/instagram/preview", {
      method: "POST", body: JSON.stringify({ url }),
    })));
  }

  async function start(referenceId: string) {
    const result = await request<{ projectId: string }>(
      "/references/" + encodeURIComponent(referenceId) + "/planning-project", { method: "POST" },
    );
    router.push("/projects/" + result.projectId + "/analysis");
  }

  async function save(createProject: boolean) {
    await run(async () => {
      const result = await request<{ referenceId: string; created: boolean }>(
        "/references/import/instagram", {
          method: "POST", body: JSON.stringify({ url, category, notes }),
        },
      );
      refresh((value) => value + 1);
      if (createProject) await start(result.referenceId);
      else {
        setNotice(result.created ? "레퍼런스를 저장했습니다." : "이미 저장한 릴스입니다.");
        setPreview(undefined);
        setUrl("");
        setNotes("");
        setCategory("");
      }
    });
  }

  return <main>
    <Link href="/references">← 숏폼 레퍼런스</Link>
    <h1>링크 가져오기 및 저장한 레퍼런스</h1>
    <section className="panel">
      <h2>릴스 링크 가져오기</h2>
      <form onSubmit={check}>
        <label>공개 릴스 URL<input required type="url" value={url} disabled={busy}
          placeholder="https://www.instagram.com/reel/…/"
          onChange={(event) => { setUrl(event.target.value); setPreview(undefined); }} /></label>
        <button disabled={busy}>미리보기 확인</button>
      </form>
      {preview && <>
        {preview.previewStatus === "available"
          ? <iframe title="인스타그램 릴스 원본" src={preview.canonicalUrl + "embed/"} loading="lazy" />
          : <p>{preview.previewStatus === "unavailable" ? "미리보기 사용 불가" : "미리보기를 잠시 후 다시 확인해 주세요."}</p>}
        <label>카테고리<input value={category} maxLength={60} disabled={busy} onChange={(e) => setCategory(e.target.value)} /></label>
        <label>관찰 메모<textarea value={notes} maxLength={3000} disabled={busy} onChange={(e) => setNotes(e.target.value)} /></label>
        <div className="actions">
          <button disabled={busy} onClick={() => save(false)}>레퍼런스 저장</button>
          <button disabled={busy} onClick={() => save(true)}>저장하고 기획 시작</button>
        </div>
      </>}
      <p role="status">{notice}</p>
    </section>
    <section>
      <h2>저장한 레퍼런스</h2>
      <label>검색<input type="search" value={query} placeholder="카테고리 또는 메모" onChange={(e) => setQuery(e.target.value)} /></label>
      {listError && <p role="alert">{listError}</p>}
      {!listError && items.length === 0 && <p>표시할 레퍼런스가 없습니다.</p>}
      <div className="grid">{items.map((item) => <article className="panel" key={item.shortcode}>
        <h3>{item.category}</h3>
        <a href={item.url} target="_blank" rel="noreferrer">인스타그램 원본 ↗</a>
        <p>{item.notes}</p>
        <button disabled={busy} onClick={() => run(() => start(item.shortcode))}>이 영상으로 기획</button>
      </article>)}</div>
    </section>
  </main>;
}
