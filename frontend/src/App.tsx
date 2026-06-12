import { PointerEvent as ReactPointerEvent, ReactNode, useEffect, useRef, useState } from "react";
import {
  Link,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Album as AlbumIcon,
  Archive,
  ChevronDown,
  ChevronUp,
  CircleCheck,
  Clapperboard,
  Copy,
  Disc3,
  Download,
  FileAudio,
  FileText,
  Film,
  Gauge,
  Image as ImageIcon,
  Library,
  ListMusic,
  LoaderCircle,
  Menu,
  Maximize2,
  MoreHorizontal,
  Music2,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Save,
  Search,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Upload,
  WandSparkles,
  X,
  Zap,
} from "lucide-react";
import {
  Album,
  api,
  assetUrl,
  Asset,
  Generation,
  iconAssetUrl,
  Job,
  lyricsUrl,
  Track,
} from "./api";

const qk = {
  albums: ["albums"] as const,
  album: (id: string) => ["albums", id] as const,
  tracks: (id: string) => ["albums", id, "tracks"] as const,
  generations: (id: string) => ["tracks", id, "generations"] as const,
  covers: (id: string) => ["albums", id, "covers"] as const,
  templatePreviews: (id: string) => ["albums", id, "template-previews"] as const,
  suno: ["system", "suno"] as const,
  job: (id: string) => ["jobs", id] as const,
};

function formatDate(value: string) {
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    draft: "초안",
    planning: "기획 중",
    lyrics_ready: "가사 준비",
    generating: "생성 중",
    queued: "대기 중",
    submitted: "제출됨",
    streaming: "처리 중",
    partially_complete: "일부 완료",
    complete: "완료",
    succeeded: "완료",
    failed: "실패",
    pending: "대기 중",
    running: "진행 중",
  };
  return labels[status] || status;
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : "알 수 없는 오류가 발생했습니다.";
}

function Button({
  children,
  variant = "primary",
  icon,
  loading,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  icon?: ReactNode;
  loading?: boolean;
}) {
  return (
    <button className={`button ${variant}`} {...props} disabled={loading || props.disabled}>
      {loading ? <LoaderCircle size={17} className="spin" /> : icon}
      {children}
    </button>
  );
}

function StatusBadge({ status }: { status: string }) {
  return <span className={`status-badge status-${status}`}>{statusLabel(status)}</span>;
}

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="error-notice">{errorText(error)}</div>;
}

function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">{icon}</div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}

function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

function useAlbumId() {
  return useParams().albumId || "";
}

function useAlbum(albumId: string) {
  return useQuery({
    queryKey: qk.album(albumId),
    queryFn: () => api.getAlbum(albumId),
    enabled: Boolean(albumId),
  });
}

function useTracks(albumId: string) {
  return useQuery({
    queryKey: qk.tracks(albumId),
    queryFn: () => api.listTracks(albumId),
    enabled: Boolean(albumId),
  });
}

function useJob(jobId: string | null) {
  return useQuery({
    queryKey: qk.job(jobId || ""),
    queryFn: () => api.getJob(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const job = query.state.data;
      return job && ["succeeded", "failed"].includes(job.status) ? false : 3000;
    },
  });
}

function JobPanel({ job }: { job?: Job }) {
  if (!job) return null;
  return (
    <div className={`job-panel ${job.status}`}>
      <div className="job-icon">
        {job.status === "succeeded" ? (
          <CircleCheck size={22} />
        ) : job.status === "failed" ? (
          <X size={22} />
        ) : (
          <LoaderCircle size={22} className="spin" />
        )}
      </div>
      <div className="job-copy">
        <strong>
          {job.status === "succeeded"
            ? "작업이 완료되었습니다."
            : job.status === "failed"
              ? "작업에 실패했습니다."
              : "AI 작업을 진행하고 있습니다."}
        </strong>
        <span>{job.error_message || `${statusLabel(job.status)} · ${job.progress || 0}%`}</span>
      </div>
      {!["succeeded", "failed"].includes(job.status) && (
        <div className="job-progress">
          <span style={{ width: `${Math.max(job.progress || 8, 8)}%` }} />
        </div>
      )}
    </div>
  );
}

function AppShell({ children }: { children: ReactNode }) {
  const { albumId } = useParams();
  const location = useLocation();
  const [mobileNav, setMobileNav] = useState(false);
  const albums = useQuery({ queryKey: qk.albums, queryFn: api.listAlbums });
  const album = useAlbum(albumId || "");
  const suno = useQuery({
    queryKey: qk.suno,
    queryFn: api.sunoStatus,
    refetchInterval: 60_000,
  });

  const menu = albumId
    ? [
        { to: `/albums/${albumId}/plan`, label: "앨범 만들기", icon: <Disc3 size={19} /> },
        { to: `/albums/${albumId}/tracks`, label: "노래 만들기", icon: <Zap size={19} /> },
        { to: `/albums/${albumId}/video`, label: "루프 영상 만들기", icon: <Film size={19} /> },
        { to: `/albums/${albumId}/album-video`, label: "전체 영상 만들기", icon: <Clapperboard size={19} /> },
        { to: `/albums/${albumId}/export`, label: "결과 · 내보내기", icon: <Archive size={19} /> },
      ]
    : [];

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
        <Link className="brand" to="/albums" onClick={() => setMobileNav(false)}>
          <span className="brand-mark"><Music2 size={22} /></span>
          <span><strong>Tubemaster</strong><small>AI PLAYLIST STUDIO</small></span>
        </Link>

        <nav className="sidebar-nav">
          <span className="nav-label">WORKSPACE</span>
          <NavLink to="/albums" className={({ isActive }) => isActive && !albumId ? "active" : ""}>
            <Library size={19} /> 앨범 프로젝트
          </NavLink>
          {albumId && (
            <>
              <span className="nav-label project-label">AI 제작</span>
              {menu.map((item) => (
                <NavLink key={item.to} to={item.to} onClick={() => setMobileNav(false)}>
                  {item.icon} {item.label}
                </NavLink>
              ))}
            </>
          )}
        </nav>

        <div className="recent-albums">
          <span className="nav-label">RECENT ALBUMS</span>
          {(albums.data || []).slice(0, 4).map((item) => (
            <Link key={item.id} to={`/albums/${item.id}/plan`} className={item.id === albumId ? "selected" : ""}>
              <span className="album-dot" />
              <span>{item.title}</span>
            </Link>
          ))}
        </div>

        <div className="connection-card">
          <div><span className="connection-dot online" /> Backend 연결됨</div>
          <div>
            <span className={`connection-dot ${suno.data?.connected ? "online" : "offline"}`} />
            Suno {suno.data?.connected ? "연결됨" : "연결 확인 필요"}
          </div>
        </div>
      </aside>

      {mobileNav && <button className="nav-backdrop" onClick={() => setMobileNav(false)} />}

      <div className="app-main">
        <header className="topbar">
          <button className="mobile-menu" onClick={() => setMobileNav(true)}><Menu /></button>
          <div className="topbar-title">
            <span>{album.data?.title || (location.pathname === "/albums" ? "앨범 프로젝트" : "AI Playlist Studio")}</span>
            {album.data && <StatusBadge status={album.data.status} />}
          </div>
          <div className="topbar-meta">
            <span className="credit-pill"><Gauge size={17} /> {suno.data?.credits_left?.toLocaleString() ?? "—"} 크레딧</span>
          </div>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}

function AlbumListPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const albums = useQuery({ queryKey: qk.albums, queryFn: api.listAlbums });
  const remove = useMutation({
    mutationFn: api.deleteAlbum,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: qk.albums }),
  });
  const filtered = (albums.data || []).filter((album) =>
    `${album.title} ${album.artist_name || ""}`.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <>
      <PageHeader
        eyebrow="YOUR MUSIC WORKSPACE"
        title="앨범 프로젝트"
        description="아이디어부터 음원과 영상까지, 하나의 프로젝트에서 완성하세요."
        actions={<Button icon={<Plus size={18} />} onClick={() => navigate("/albums/new")}>새 앨범 만들기</Button>}
      />
      <div className="toolbar">
        <label className="search-field"><Search size={18} /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="앨범 또는 아티스트 검색" /></label>
        <div className="toolbar-caption">{filtered.length}개의 프로젝트</div>
      </div>
      <ErrorNotice error={albums.error || remove.error} />
      {albums.isLoading ? (
        <div className="album-grid">{Array.from({ length: 6 }).map((_, i) => <div className="album-card skeleton" key={i} />)}</div>
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={<Disc3 size={38} />}
          title={search ? "검색 결과가 없습니다." : "첫 앨범을 만들어 보세요."}
          description={search ? "다른 검색어를 입력해 보세요." : "음악 스타일을 정하면 AI가 트랙과 가사를 함께 기획합니다."}
          action={!search && <Button icon={<Plus size={18} />} onClick={() => navigate("/albums/new")}>앨범 만들기</Button>}
        />
      ) : (
        <div className="album-grid">
          {filtered.map((album, index) => (
            <article className="album-card" key={album.id} onClick={() => navigate(`/albums/${album.id}/plan`)}>
              <div className={`album-art art-${index % 5}`}>
                <Disc3 size={54} />
                <span>{album.genre || "AI PLAYLIST"}</span>
              </div>
              <div className="album-card-body">
                <div className="album-card-title"><div><h3>{album.title}</h3><p>{album.artist_name || "Unknown Artist"}</p></div><StatusBadge status={album.status} /></div>
                <div className="album-card-footer"><span>{album.track_count} tracks</span><span>{formatDate(album.updated_at)}</span></div>
              </div>
              <button
                className="card-menu"
                title="앨범 삭제"
                onClick={(e) => {
                  e.stopPropagation();
                  if (confirm(`"${album.title}" 앨범을 삭제할까요?`)) remove.mutate(album.id);
                }}
              ><Trash2 size={16} /></button>
            </article>
          ))}
        </div>
      )}
    </>
  );
}

const defaultAlbum = {
  title: "",
  artist_name: "",
  description: "",
  genre: "K-Pop",
  vocal_style: "soft female vocal",
  tempo: "90-110 BPM",
  lyrics_language: "ko",
  mood: "calm and gentle",
  instruments: ["synthesizer", "piano", "soft drums"],
  keywords: "",
  additional_instructions: "",
  track_count: 10,
};

const albumGenres = [
  "K-Pop",
  "J-Pop",
  "City Pop (시티팝)",
  "Pop Ballad (팝 발라드)",
  "Pop",
  "R&B / Soul",
  "Hip-Hop / Rap",
  "EDM / Dance",
  "House",
  "Lo-Fi / Chill",
  "Rock",
  "Indie Pop",
  "Folk / Acoustic",
  "Jazz",
  "Disco / Funk",
  "Dream Pop (드림팝)",
  "Synthwave (신스웨이브)",
  "발라드",
  "트로트",
  "OST / 시네마틱",
] as const;

const albumLanguages = [
  { value: "ko", label: "한국어" },
  { value: "en", label: "영어" },
  { value: "ja", label: "일본어" },
  { value: "zh", label: "중국어" },
  { value: "es", label: "스페인어" },
  { value: "pt", label: "포르투갈어" },
  { value: "ko-en mixed", label: "한국어 + 영어 혼합" },
  { value: "en with Korean phrases", label: "영어 + 한국어 포인트" },
] as const;

const albumMoods = [
  { value: "bright and cheerful", label: "밝고 경쾌한" },
  { value: "romantic and warm", label: "로맨틱하고 따뜻한" },
  { value: "sad and melancholic", label: "슬프고 우울한" },
  { value: "dreamy and mysterious", label: "몽환적이고 신비로운" },
  { value: "energetic and exciting", label: "에너제틱하고 신나는" },
  { value: "calm and gentle", label: "차분하고 잔잔한" },
  { value: "nostalgic and retro", label: "추억에 젖은, 레트로한" },
  { value: "passionate and intense", label: "열정적이고 강렬한" },
  { value: "cool and chic", label: "쿨하고 시크한" },
  { value: "retro and upbeat", label: "레트로하고 신나는" },
  { value: "urban and sophisticated", label: "어반하고 세련된" },
  { value: "playful and humorous", label: "장난기 있고 유쾌한" },
] as const;

const albumVocals = [
  { value: "female solo vocal", label: "여성 솔로" },
  { value: "male solo vocal", label: "남성 솔로" },
  { value: "soft female vocal", label: "부드러운 여성 보컬" },
  { value: "powerful female vocal", label: "파워풀 여성 보컬" },
  { value: "husky male vocal", label: "허스키한 남성 보컬" },
  { value: "smooth male vocal", label: "스무스한 남성 보컬" },
  { value: "mixed-gender duet vocals", label: "혼성 듀엣" },
  { value: "female duet vocals", label: "여성 듀엣" },
  { value: "girl group harmony vocals", label: "걸그룹 하모니" },
  { value: "boy group harmony vocals", label: "보이그룹 하모니" },
  { value: "mixed group vocals", label: "혼성 그룹 보컬" },
  { value: "dreamy whisper vocals", label: "몽환적 위스퍼 보컬" },
  { value: "sweet youthful female vocal", label: "사랑스러운 소녀 보컬" },
  { value: "instrumental", label: "연주곡" },
] as const;

const albumInstrumentPresets = [
  {
    label: "K-Pop · 신스, 피아노, 소프트 드럼",
    instruments: ["synthesizer", "piano", "soft drums"],
  },
  {
    label: "팝 발라드 · 피아노, 스트링, 어쿠스틱 기타",
    instruments: ["piano", "lush strings", "acoustic guitar", "soft drums"],
  },
  {
    label: "시티팝 · 일렉트릭 피아노, 신스, 펑크 기타",
    instruments: ["electric piano", "analog synthesizer", "funk guitar", "electric bass", "vintage drums"],
  },
  {
    label: "R&B / Soul · 로즈 피아노, 베이스, 소울 기타",
    instruments: ["Rhodes piano", "deep bass", "soul guitar", "warm drums"],
  },
  {
    label: "Hip-Hop · 808 베이스, 드럼 머신, 신스",
    instruments: ["808 bass", "drum machine", "dark synthesizer", "sampled keys"],
  },
  {
    label: "EDM / Dance · 리드 신스, 서브 베이스, 전자 드럼",
    instruments: ["lead synthesizer", "sub bass", "electronic drums", "synth pads"],
  },
  {
    label: "House · 피아노 코드, 하우스 베이스, 드럼 머신",
    instruments: ["house piano", "groovy bass", "drum machine", "synth stabs"],
  },
  {
    label: "Lo-Fi · 로즈 피아노, 재즈 기타, 빈티지 드럼",
    instruments: ["Rhodes piano", "jazz guitar", "warm bass", "dusty vintage drums"],
  },
  {
    label: "Rock · 일렉트릭 기타, 베이스, 라이브 드럼",
    instruments: ["electric guitar", "bass guitar", "live drums"],
  },
  {
    label: "Indie Pop · 클린 기타, 신스, 어쿠스틱 드럼",
    instruments: ["clean electric guitar", "soft synthesizer", "melodic bass", "acoustic drums"],
  },
  {
    label: "Folk / Acoustic · 통기타, 피아노, 첼로",
    instruments: ["acoustic guitar", "piano", "cello", "light percussion"],
  },
  {
    label: "Jazz · 피아노, 콘트라베이스, 브러시 드럼, 색소폰",
    instruments: ["jazz piano", "upright bass", "brush drums", "saxophone"],
  },
  {
    label: "Disco / Funk · 펑크 기타, 슬랩 베이스, 브라스",
    instruments: ["funk guitar", "slap bass", "disco strings", "brass section", "disco drums"],
  },
  {
    label: "Dream Pop · 드림 신스, 리버브 기타, 소프트 드럼",
    instruments: ["dreamy synthesizer", "reverb electric guitar", "synth bass", "soft drums"],
  },
  {
    label: "Synthwave · 아날로그 신스, 아르페지에이터, 게이티드 드럼",
    instruments: ["analog synthesizer", "arpeggiator", "synth bass", "gated electronic drums"],
  },
  {
    label: "트로트 · 아코디언, 브라스, 기타, 리듬 섹션",
    instruments: ["accordion", "brass section", "electric guitar", "trot rhythm section"],
  },
  {
    label: "OST / 시네마틱 · 오케스트라, 피아노, 타악기",
    instruments: ["grand piano", "cinematic strings", "orchestral brass", "cinematic percussion"],
  },
] as const;

const instrumentPresetValue = (instruments: readonly string[]) => instruments.join("|");

function AlbumCreatePage() {
  const navigate = useNavigate();
  const [form, setForm] = useState(defaultAlbum);
  const [customGenre, setCustomGenre] = useState(false);
  const [customLanguage, setCustomLanguage] = useState(false);
  const [customMood, setCustomMood] = useState(false);
  const [customVocal, setCustomVocal] = useState(false);
  const [customInstruments, setCustomInstruments] = useState(false);
  const create = useMutation({
    mutationFn: () => api.createAlbum(form),
    onSuccess: (album) => navigate(`/albums/${album.id}/plan`),
  });
  const set = (key: string, value: unknown) => setForm((prev) => ({ ...prev, [key]: value }));

  return (
    <>
      <PageHeader eyebrow="NEW ALBUM" title="새 앨범 만들기" description="원하는 음악의 방향을 정하면 AI가 완성도 높은 앨범 기획으로 확장합니다." />
      <form className="create-layout" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <section className="panel form-panel">
          <SectionTitle icon={<AlbumIcon />} title="기본 정보" />
          <div className="form-grid">
            <Field label="앨범 제목" wide><input required value={form.title} onChange={(e) => set("title", e.target.value)} placeholder="예: 비 오는 날의 기억" /></Field>
            <Field label="아티스트명"><input value={form.artist_name} onChange={(e) => set("artist_name", e.target.value)} placeholder="Playlist Studio" /></Field>
            <Field label="트랙 수"><input type="number" min={1} max={30} value={form.track_count} onChange={(e) => set("track_count", Number(e.target.value))} /></Field>
            <Field label="앨범 설명" wide><textarea rows={2} value={form.description} onChange={(e) => set("description", e.target.value)} placeholder="앨범의 전체 이야기와 방향을 적어주세요." /></Field>
          </div>
          <SectionTitle icon={<SlidersHorizontal />} title="음악 스타일" />
          <div className="form-grid">
            <Field label="장르">
              <select
                value={customGenre ? "__custom__" : form.genre}
                onChange={(e) => {
                  if (e.target.value === "__custom__") {
                    setCustomGenre(true);
                    set("genre", "");
                  } else {
                    setCustomGenre(false);
                    set("genre", e.target.value);
                  }
                }}
              >
                {albumGenres.map((genre) => <option key={genre} value={genre}>{genre}</option>)}
                <option value="__custom__">직접 작성하기</option>
              </select>
              {customGenre && (
                <input
                  required
                  autoFocus
                  value={form.genre}
                  onChange={(e) => set("genre", e.target.value)}
                  placeholder="원하는 장르를 입력하세요."
                />
              )}
            </Field>
            <Field label="보컬">
              <select
                value={customVocal ? "__custom__" : form.vocal_style}
                onChange={(e) => {
                  if (e.target.value === "__custom__") {
                    setCustomVocal(true);
                    set("vocal_style", "");
                  } else {
                    setCustomVocal(false);
                    set("vocal_style", e.target.value);
                  }
                }}
              >
                {albumVocals.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
                <option value="__custom__">직접 작성하기</option>
              </select>
              {customVocal && <input required autoFocus value={form.vocal_style} onChange={(e) => set("vocal_style", e.target.value)} placeholder="원하는 보컬 스타일을 입력하세요." />}
            </Field>
            <Field label="템포"><select value={form.tempo} onChange={(e) => set("tempo", e.target.value)}><option>60-90 BPM</option><option>90-110 BPM</option><option>110-140 BPM</option></select></Field>
            <Field label="가사 언어">
              <select
                value={customLanguage ? "__custom__" : form.lyrics_language}
                onChange={(e) => {
                  if (e.target.value === "__custom__") {
                    setCustomLanguage(true);
                    set("lyrics_language", "");
                  } else {
                    setCustomLanguage(false);
                    set("lyrics_language", e.target.value);
                  }
                }}
              >
                {albumLanguages.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
                <option value="__custom__">직접 작성하기</option>
              </select>
              {customLanguage && <input required autoFocus value={form.lyrics_language} onChange={(e) => set("lyrics_language", e.target.value)} placeholder="사용할 언어를 입력하세요." />}
            </Field>
            <Field label="분위기" wide>
              <select
                value={customMood ? "__custom__" : form.mood}
                onChange={(e) => {
                  if (e.target.value === "__custom__") {
                    setCustomMood(true);
                    set("mood", "");
                  } else {
                    setCustomMood(false);
                    set("mood", e.target.value);
                  }
                }}
              >
                {albumMoods.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
                <option value="__custom__">직접 작성하기</option>
              </select>
              {customMood && <input required autoFocus value={form.mood} onChange={(e) => set("mood", e.target.value)} placeholder="원하는 분위기를 입력하세요." />}
            </Field>
            <Field label="악기 조합" wide>
              <select
                value={customInstruments ? "__custom__" : instrumentPresetValue(form.instruments)}
                onChange={(e) => {
                  if (e.target.value === "__custom__") {
                    setCustomInstruments(true);
                    set("instruments", []);
                  } else {
                    const preset = albumInstrumentPresets.find(
                      ({ instruments }) => instrumentPresetValue(instruments) === e.target.value,
                    );
                    setCustomInstruments(false);
                    set("instruments", preset ? [...preset.instruments] : []);
                  }
                }}
              >
                {albumInstrumentPresets.map(({ label, instruments }) => (
                  <option key={label} value={instrumentPresetValue(instruments)}>{label}</option>
                ))}
                <option value="__custom__">직접 작성하기</option>
              </select>
              {customInstruments && (
                <input
                  required
                  autoFocus
                  value={form.instruments.join(", ")}
                  onChange={(e) => set("instruments", e.target.value.split(",").map((value) => value.trim()).filter(Boolean))}
                  placeholder="예: piano, acoustic guitar, cello"
                />
              )}
            </Field>
          </div>
          <SectionTitle icon={<Sparkles />} title="기획 요청" />
          <div className="form-grid">
            <Field label="주제 / 키워드" wide><textarea rows={3} value={form.keywords} onChange={(e) => set("keywords", e.target.value)} placeholder="비, 오래된 친구, 카페, 추억" /></Field>
            <Field label="추가 요청사항" wide><textarea rows={3} value={form.additional_instructions} onChange={(e) => set("additional_instructions", e.target.value)} placeholder="후렴을 쉽게 기억할 수 있게 구성해 주세요." /></Field>
          </div>
          <ErrorNotice error={create.error} />
          <div className="form-actions"><Button type="button" variant="ghost" onClick={() => navigate("/albums")}>취소</Button><Button type="submit" loading={create.isPending} icon={<Plus size={18} />}>초안 만들기</Button></div>
        </section>
        <aside className="panel summary-panel">
          <span className="eyebrow">ALBUM PREVIEW</span>
          <div className="preview-disc"><Disc3 size={78} /></div>
          <h2>{form.title || "새로운 앨범"}</h2>
          <p>{form.artist_name || "Artist name"}</p>
          <dl>
            <div><dt>장르</dt><dd>{form.genre}</dd></div>
            <div><dt>보컬</dt><dd>{albumVocals.find(({ value }) => value === form.vocal_style)?.label || form.vocal_style}</dd></div>
            <div><dt>언어</dt><dd>{albumLanguages.find(({ value }) => value === form.lyrics_language)?.label || form.lyrics_language}</dd></div>
            <div><dt>분위기</dt><dd>{albumMoods.find(({ value }) => value === form.mood)?.label || form.mood}</dd></div>
            <div><dt>템포</dt><dd>{form.tempo}</dd></div>
            <div><dt>구성</dt><dd>{form.track_count} tracks</dd></div>
          </dl>
          <div className="ai-summary"><WandSparkles size={20} /><p>Gemini가 트랙 제목, 가사, Suno용 영문 스타일과 이미지 프롬프트를 만듭니다.</p></div>
        </aside>
      </form>
    </>
  );
}

function SectionTitle({ icon, title }: { icon: ReactNode; title: string }) {
  return <div className="section-title"><span>{icon}</span><h2>{title}</h2></div>;
}

function Field({ label, wide, children }: { label: string; wide?: boolean; children: ReactNode }) {
  return <label className={`field ${wide ? "wide" : ""}`}><span>{label}</span>{children}</label>;
}

function TrackEditor({ track, onSaved }: { track: Track; onSaved: () => void }) {
  const [open, setOpen] = useState(track.sequence === 1);
  const [tab, setTab] = useState<"lyrics" | "style" | "image">("lyrics");
  const [lyrics, setLyrics] = useState(track.lyrics);
  const [style, setStyle] = useState(track.style_prompt);
  const [instruction, setInstruction] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const saveLyrics = useMutation({ mutationFn: () => api.saveLyrics(track.id, lyrics), onSuccess: onSaved });
  const saveStyle = useMutation({ mutationFn: () => api.saveStyle(track.id, style), onSuccess: onSaved });
  const regenerate = useMutation({
    mutationFn: () => api.regenerateLyrics(track.id, instruction, tab === "style"),
    onSuccess: (value) => setJobId(value.job_id),
  });
  const job = useJob(jobId);
  useEffect(() => {
    if (job.data?.status === "succeeded") onSaved();
  }, [job.data?.status]);
  useEffect(() => { setLyrics(track.lyrics); setStyle(track.style_prompt); }, [track]);

  return (
    <article className={`track-editor ${open ? "open" : ""}`}>
      <button className="track-editor-head" onClick={() => setOpen(!open)}>
        <span className="track-number">{track.sequence}</span>
        <span className="track-title"><strong>{track.title}</strong><small>{track.concept || "트랙 콘셉트"}</small></span>
        <StatusBadge status={track.status} />
        {open ? <ChevronUp size={19} /> : <ChevronDown size={19} />}
      </button>
      {open && (
        <div className="track-editor-body">
          <div className="editor-tabs">
            <button className={tab === "lyrics" ? "active" : ""} onClick={() => setTab("lyrics")}>가사</button>
            <button className={tab === "style" ? "active" : ""} onClick={() => setTab("style")}>영문 스타일</button>
            <button className={tab === "image" ? "active" : ""} onClick={() => setTab("image")}>이미지 프롬프트</button>
          </div>
          {tab === "lyrics" && <textarea className="lyrics-editor" value={lyrics} onChange={(e) => setLyrics(e.target.value)} />}
          {tab === "style" && <textarea className="style-editor" value={style} onChange={(e) => setStyle(e.target.value)} />}
          {tab === "image" && <div className="prompt-readonly">{track.image_prompt || "이미지 프롬프트가 아직 없습니다."}</div>}
          <div className="editor-footer">
            <span>{tab === "lyrics" ? `${lyrics.length.toLocaleString()}자` : tab === "style" ? `${style.length.toLocaleString()}자` : "Gemini image prompt"}</span>
            <div>
              {tab === "lyrics" && <a className="button ghost" href={lyricsUrl(track.id)}><Download size={16} /> TXT</a>}
              {tab !== "image" && <Button variant="secondary" loading={regenerate.isPending} icon={<RefreshCw size={16} />} onClick={() => regenerate.mutate()}>AI 재생성</Button>}
              {tab === "lyrics" && <Button loading={saveLyrics.isPending} icon={<Save size={16} />} onClick={() => saveLyrics.mutate()}>가사 저장</Button>}
              {tab === "style" && <Button loading={saveStyle.isPending} icon={<Save size={16} />} onClick={() => saveStyle.mutate()}>스타일 저장</Button>}
            </div>
          </div>
          {(regenerate.error || saveLyrics.error || saveStyle.error) && <ErrorNotice error={regenerate.error || saveLyrics.error || saveStyle.error} />}
          <JobPanel job={job.data} />
        </div>
      )}
    </article>
  );
}

function AlbumPlanPage() {
  const albumId = useAlbumId();
  const queryClient = useQueryClient();
  const album = useAlbum(albumId);
  const tracks = useTracks(albumId);
  const [jobId, setJobId] = useState<string | null>(null);
  const plan = useMutation({
    mutationFn: () => api.planAlbum(albumId),
    onSuccess: (result) => setJobId(result.job_id),
  });
  const job = useJob(jobId);
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: qk.album(albumId) });
    queryClient.invalidateQueries({ queryKey: qk.tracks(albumId) });
  };
  useEffect(() => {
    if (job.data?.status === "succeeded") refresh();
  }, [job.data?.status]);

  if (album.isLoading) return <LoadingPage />;
  if (!album.data) return <ErrorNotice error={album.error} />;

  return (
    <>
      <PageHeader
        eyebrow="STEP 01 · ALBUM PLANNING"
        title="앨범 만들기"
        description="앨범의 음악적 방향을 바탕으로 Gemini가 플레이리스트와 가사를 기획합니다."
        actions={<Button loading={plan.isPending || ["pending", "running"].includes(job.data?.status || "")} icon={<WandSparkles size={18} />} onClick={() => { if (!tracks.data?.length || confirm("새 기획을 생성하면 현재 트랙이 교체될 수 있습니다. 계속할까요?")) plan.mutate(); }}>AI 앨범 기획</Button>}
      />
      <JobPanel job={job.data} />
      <ErrorNotice error={plan.error || tracks.error} />
      <section className="panel style-overview">
        <div className="style-tags">
          <span>{album.data.genre || "장르 미지정"}</span>
          <span>{album.data.vocal_style || "보컬 미지정"}</span>
          <span>{album.data.tempo || "템포 미지정"}</span>
          <span>{album.data.mood || "분위기 미지정"}</span>
        </div>
        <div className="keyword-line"><Sparkles size={17} /><strong>주제와 키워드</strong><span>{album.data.keywords || "키워드를 입력해 주세요."}</span></div>
      </section>
      {album.data.style_prompt && (
        <section className="panel common-style">
          <div><span className="eyebrow">COMMON SUNO STYLE</span><p>{album.data.style_prompt}</p></div>
          <Button variant="secondary" icon={<Copy size={16} />} onClick={() => navigator.clipboard.writeText(album.data!.style_prompt)}>복사</Button>
        </section>
      )}
      <div className="section-heading"><div><h2>완성된 플레이리스트</h2><p>트랙별 가사와 영문 스타일을 검토하고 수정하세요.</p></div><span>{tracks.data?.length || 0} tracks</span></div>
      {!tracks.data?.length ? (
        <EmptyState icon={<ListMusic size={38} />} title="아직 기획된 트랙이 없습니다." description="AI 앨범 기획을 실행하면 트랙과 가사가 이곳에 표시됩니다." />
      ) : (
        <div className="track-editor-list">{tracks.data.map((track) => <TrackEditor key={track.id} track={track} onSaved={refresh} />)}</div>
      )}
    </>
  );
}

type GenerationDetailTab = "lyrics" | "style";

function GenerationDetailPanel({
  track,
  activeTab,
  onTabChange,
  onClose,
}: {
  track: Track;
  activeTab: GenerationDetailTab;
  onTabChange: (tab: GenerationDetailTab) => void;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const content = activeTab === "lyrics" ? track.lyrics : track.style_prompt;
  const styleTags = track.style_prompt
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);

  useEffect(() => {
    setCopied(false);
  }, [activeTab, track.id]);

  const copyContent = async () => {
    await navigator.clipboard.writeText(content);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <aside className="detail-drawer" aria-labelledby={`detail-title-${track.id}`}>
      <div className="detail-drawer-head">
        <div>
          <span className="detail-kicker">TRACK {String(track.sequence).padStart(2, "0")}</span>
          <h2 id={`detail-title-${track.id}`}>{track.title}</h2>
        </div>
        <button className="icon-button" onClick={onClose} aria-label="닫기" title="닫기"><X size={20} /></button>
      </div>
      <div className="detail-tabs" role="tablist" aria-label="트랙 상세 정보">
        <button className={activeTab === "lyrics" ? "active" : ""} onClick={() => onTabChange("lyrics")} role="tab" aria-selected={activeTab === "lyrics"}>
          <FileText size={16} /> 가사
        </button>
        <button className={activeTab === "style" ? "active" : ""} onClick={() => onTabChange("style")} role="tab" aria-selected={activeTab === "style"}>
          <Sparkles size={16} /> 스타일
        </button>
      </div>
      <div className="detail-drawer-toolbar">
        <span>{content.length.toLocaleString()}자</span>
        <button className="detail-copy-button" onClick={copyContent}><Copy size={15} /> {copied ? "복사됨" : "복사"}</button>
      </div>
      <div className="detail-drawer-content">
        {activeTab === "lyrics" ? (
          <pre className="lyrics-preview">{track.lyrics || "등록된 가사가 없습니다."}</pre>
        ) : (
          <>
            {!!styleTags.length && (
              <div className="style-detail-tags">
                {styleTags.map((tag, index) => <span key={`${tag}-${index}`}>{tag}</span>)}
              </div>
            )}
            <section className="style-original">
              <h3>스타일 원문</h3>
              <p>{track.style_prompt || "등록된 스타일이 없습니다."}</p>
            </section>
          </>
        )}
      </div>
    </aside>
  );
}

function GenerationPanel({
  track,
  album,
  selected,
  onSelectTrack,
  detailTab,
  onOpenDetail,
}: {
  track: Track;
  album: Album;
  selected: boolean;
  onSelectTrack: (checked: boolean) => void;
  detailTab: GenerationDetailTab | null;
  onOpenDetail: (tab: GenerationDetailTab) => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(track.sequence === 1);
  const [jobId, setJobId] = useState<string | null>(null);
  const generations = useQuery({ queryKey: qk.generations(track.id), queryFn: () => api.listGenerations(track.id), enabled: open });
  const generate = useMutation({
    mutationFn: () => api.generateTrack(track.id),
    onSuccess: (result) => { setJobId(result.job_id); setOpen(true); },
  });
  const choose = useMutation({
    mutationFn: (id: string) => api.selectGeneration(track.id, id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: qk.generations(track.id) });
      queryClient.invalidateQueries({ queryKey: qk.tracks(track.album_id) });
    },
  });
  const job = useJob(jobId);
  useEffect(() => {
    if (job.data?.status === "succeeded") {
      queryClient.invalidateQueries({ queryKey: qk.generations(track.id) });
      queryClient.invalidateQueries({ queryKey: qk.album(track.album_id) });
    }
  }, [job.data?.status]);
  const audioAsset = (generation: Generation) =>
    album.assets?.find((asset) => asset.type === "audio" && asset.generation_id === generation.id);

  return (
    <article className={`generation-track ${open ? "open" : ""}`}>
      <div className="generation-head">
        <input type="checkbox" checked={selected} onChange={(e) => onSelectTrack(e.target.checked)} aria-label={`${track.title} 선택`} />
        <span className="track-number">{track.sequence}</span>
        <button className="generation-title" onClick={() => setOpen(!open)}><strong>{track.title}</strong><small>{track.lyrics.length.toLocaleString()}자 · Custom Mode</small></button>
        <StatusBadge status={track.selected_generation_id ? "complete" : job.data?.status || track.status} />
        {!track.selected_generation_id && <Button variant="secondary" loading={generate.isPending || ["pending", "running"].includes(job.data?.status || "")} icon={<Zap size={16} />} onClick={() => generate.mutate()}>노래 만들기</Button>}
        <button className="collapse-button" onClick={() => setOpen(!open)}>{open ? <ChevronUp /> : <ChevronDown />}</button>
      </div>
      {open && (
        <div className="generation-body">
          <JobPanel job={job.data} />
          <ErrorNotice error={generate.error || generations.error || choose.error} />
          <div className="input-summary">
            <button className={detailTab === "lyrics" ? "active" : ""} onClick={() => onOpenDetail("lyrics")}><FileText size={16} /> 가사 {track.lyrics.length.toLocaleString()}자</button>
            <button className={detailTab === "style" ? "active" : ""} onClick={() => onOpenDetail("style")}><Sparkles size={16} /> 스타일 {track.style_prompt.length.toLocaleString()}자</button>
          </div>
          {generations.isLoading ? <div className="candidate-grid"><div className="candidate-card skeleton" /><div className="candidate-card skeleton" /></div> :
            !generations.data?.length ? (
              <div className="candidate-empty"><Music2 size={28} /><span>아직 생성된 음원 후보가 없습니다.</span></div>
            ) : (
              <div className="candidate-grid">
                {generations.data.map((candidate) => {
                  const local = audioAsset(candidate);
                  const source = local ? assetUrl(local.id) : candidate.audio_url || "";
                  return (
                    <div className={`candidate-card ${candidate.is_selected ? "selected" : ""}`} key={candidate.id}>
                      <div className="candidate-cover">
                        {candidate.image_url ? (
                          <>
                            <img className="candidate-cover-backdrop" src={candidate.image_url} alt="" aria-hidden="true" />
                            <img className="candidate-cover-image" src={candidate.image_url} alt={`${candidate.title || track.title} 커버`} />
                          </>
                        ) : <Disc3 size={42} />}
                        {candidate.is_selected && <span className="selected-stamp"><CircleCheck size={15} /> 최종 선택</span>}
                      </div>
                      <h4>{candidate.title || `Candidate ${candidate.clip_id.slice(0, 5)}`}</h4>
                      <p>{candidate.tags || track.style_prompt}</p>
                      {source ? <audio controls preload="none" src={source} /> : <span className="audio-wait">오디오 준비 중</span>}
                      <div className="candidate-actions">
                        {source && <a href={source} download className="button ghost"><Download size={16} /> MP3</a>}
                        <Button variant={candidate.is_selected ? "secondary" : "primary"} disabled={candidate.is_selected} onClick={() => choose.mutate(candidate.id)}>{candidate.is_selected ? "선택됨" : "최종 선택"}</Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          {!!generations.data?.length && <div className="regenerate-row"><Button variant="ghost" icon={<RefreshCw size={16} />} onClick={() => generate.mutate()}>후보 다시 만들기</Button></div>}
        </div>
      )}
    </article>
  );
}

function TrackGenerationPage() {
  const albumId = useAlbumId();
  const queryClient = useQueryClient();
  const album = useAlbum(albumId);
  const tracks = useTracks(albumId);
  const [selected, setSelected] = useState<string[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const [detail, setDetail] = useState<{ trackId: string; tab: GenerationDetailTab } | null>(null);
  const generate = useMutation({
    mutationFn: () => api.generateAlbum(albumId, selected),
    onSuccess: (result) => setJobId(result.job_id),
  });
  const job = useJob(jobId);
  useEffect(() => {
    if (job.data?.status === "succeeded") {
      queryClient.invalidateQueries({ queryKey: qk.album(albumId) });
      queryClient.invalidateQueries({ queryKey: qk.tracks(albumId) });
      queryClient.invalidateQueries({
        predicate: (query) => query.queryKey[0] === "tracks",
      });
    }
  }, [job.data?.status]);

  if (album.isLoading || tracks.isLoading) return <LoadingPage />;
  if (!album.data) return <ErrorNotice error={album.error} />;
  const detailTrack = detail
    ? (tracks.data || []).find((track) => track.id === detail.trackId)
    : undefined;

  return (
    <>
      <PageHeader
        eyebrow="STEP 02 · MUSIC GENERATION"
        title="노래 만들기"
        description="확정한 가사와 영문 스타일로 Suno 음원을 만들고 최종 후보를 선택하세요."
        actions={<Button disabled={!selected.length} loading={generate.isPending || ["pending", "running"].includes(job.data?.status || "")} icon={<Zap size={18} />} onClick={() => generate.mutate()}>선택 {selected.length}곡 생성</Button>}
      />
      <JobPanel job={job.data} />
      <ErrorNotice error={generate.error || tracks.error} />
      <div className="generation-toolbar">
        <span>{tracks.data?.length || 0} tracks</span>
        <button onClick={() => setSelected(selected.length === tracks.data?.length ? [] : (tracks.data || []).map((track) => track.id))}>{selected.length === tracks.data?.length ? "전체 해제" : "전체 선택"}</button>
      </div>
      <div className={`generation-workspace ${detailTrack ? "with-detail" : ""}`}>
        <div className="generation-list">
          {(tracks.data || []).map((track) => (
            <GenerationPanel
              key={track.id}
              track={track}
              album={album.data!}
              selected={selected.includes(track.id)}
              onSelectTrack={(checked) => setSelected((prev) => checked ? [...prev, track.id] : prev.filter((id) => id !== track.id))}
              detailTab={detail?.trackId === track.id ? detail.tab : null}
              onOpenDetail={(tab) => setDetail({ trackId: track.id, tab })}
            />
          ))}
        </div>
        {detailTrack && detail && (
          <GenerationDetailPanel
            track={detailTrack}
            activeTab={detail.tab}
            onTabChange={(tab) => setDetail({ trackId: detailTrack.id, tab })}
            onClose={() => setDetail(null)}
          />
        )}
      </div>
    </>
  );
}

const defaultCompose = {
  crop: "fill",
  brightness: 0,
  contrast: 0,
  saturation: 0,
  blur: 0,
  overlay_color: "#21000f",
  overlay_opacity: 0.15,
  title: "PLAY LIST",
  artist_name: "",
  title_position: "bottom-left",
  title_x: 18,
  title_y: 82,
  title_anchor_text: "",
  font_family: "malgun",
  text_color: "#ffffff",
  title_size: 72,
  artist_x: 18,
  artist_y: 88,
  artist_font_family: "malgun",
  artist_color: "#ffffff",
  artist_size: 28,
  icon: "",
  icon_image: "",
  icon_x: 50,
  icon_y: 18,
  icon_size: 64,
  show_visualizer: true,
  visualizer_x: 88,
  visualizer_y: 82,
  visualizer_width: 11,
  visualizer_height: 90,
  visualizer_style: "bars",
  visualizer_color: "#ffffff",
};

function composeWithDefaults(value?: Partial<typeof defaultCompose> | null) {
  return {
    ...defaultCompose,
    ...value,
    visualizer_color: value?.visualizer_color || value?.text_color || defaultCompose.visualizer_color,
  };
}

function measureVideoTextWidth(
  text: string,
  fontFamily: string,
  fontSize: number,
  fontWeight: number,
  letterSpacing: number,
) {
  if (!text || typeof document === "undefined") return 0;
  const context = document.createElement("canvas").getContext("2d");
  if (!context) return 0;
  context.font = `${fontWeight} ${fontSize}px ${fontFamily}`;
  return Array.from(text).reduce((width, character, index, characters) => (
    width + context.measureText(character).width
    + (index < characters.length - 1 ? letterSpacing : 0)
  ), 0);
}

function titleStartX(compose: typeof defaultCompose) {
  if (!compose.title_anchor_text) return compose.title_x;
  const anchorWidth = measureVideoTextWidth(
    compose.title_anchor_text,
    videoFonts[compose.font_family] || videoFonts.malgun,
    compose.title_size,
    700,
    compose.title_size * 0.12,
  );
  return compose.title_x - (anchorWidth / VIDEO_CANVAS_WIDTH) * 50;
}

const videoFonts: Record<string, string> = {
  malgun: '"Malgun Gothic", "Noto Sans KR", sans-serif',
  noto_sans_kr: '"Noto Sans KR", "Malgun Gothic", sans-serif',
  noto_serif_kr: '"Noto Serif KR", "Batang", serif',
  nanum_gothic: '"NanumGothic", "Malgun Gothic", sans-serif',
  nanum_pen: '"Nanum Pen Script", "NanumPen", "Malgun Gothic", cursive',
  han_dotum: '"Hancom Dotum", "HANDotum", "Malgun Gothic", sans-serif',
  han_batang: '"Hancom Batang", "HANBatang", "Batang", serif',
  batang: '"Batang", "Noto Serif KR", serif',
  arial: 'Arial, "Malgun Gothic", sans-serif',
  roboto: 'Roboto, "Noto Sans KR", "Malgun Gothic", sans-serif',
  bebas: '"Bebas Neue", "Noto Sans KR", "Malgun Gothic", sans-serif',
  anton: 'Anton, "Noto Sans KR", "Malgun Gothic", sans-serif',
  cinzel: 'Cinzel, "Noto Serif KR", "Batang", serif',
  georgia: 'Georgia, "Noto Serif KR", "Batang", serif',
  impact: 'Impact, "Noto Sans KR", "Malgun Gothic", sans-serif',
  consolas: 'Consolas, "Noto Sans KR", "Malgun Gothic", monospace',
};

const videoFontOptions = [
  ["malgun", "맑은 고딕"],
  ["noto_sans_kr", "Noto Sans KR"],
  ["noto_serif_kr", "Noto Serif KR"],
  ["nanum_gothic", "나눔고딕"],
  ["nanum_pen", "나눔펜"],
  ["han_dotum", "한컴 돋움"],
  ["han_batang", "한컴 바탕"],
  ["batang", "바탕"],
  ["arial", "Arial"],
  ["roboto", "Roboto"],
  ["bebas", "Bebas Neue"],
  ["anton", "Anton"],
  ["cinzel", "Cinzel"],
  ["georgia", "Georgia"],
  ["impact", "Impact"],
  ["consolas", "Consolas"],
] as const;

const videoIcons = ["", "♪", "♫", "★", "♥", "☾", "☀", "☁", "✦", "✿", "●", "◆"];
const VIDEO_CANVAS_WIDTH = 1920;
const VIDEO_CANVAS_HEIGHT = 1080;
const VISUALIZER_GAP = 8;
const VISUALIZER_BARS = [7, 18, 11, 15, 9] as const;
const RENDER_FRAME_PREFIX = "loop-render-frame-";
type VideoEditorTab = "image" | "text" | "icon" | "visualizer" | "effects";
type TextEditorTarget = "title" | "artist";
type VideoWorkspace = "production" | "templates";
type TemplateTitleSource = "track" | "template" | "hidden";
type TemplateArtistSource = "album" | "template" | "hidden";
type BatchEditMode = "saved_then_template" | "template_only" | "saved_only";
type BatchImageMode = "generate_per_track" | "generate_shared" | "selected_then_generate_per_track" | "shared_existing";
type BatchActivityTrack = {
  track_id: string;
  title: string;
  status: string;
  message: string;
};

function scaleVideoPixels(value: number, previewScale: number, minimum: number) {
  return Math.max(minimum, value * previewScale);
}

function visualizerBarHeight(value: number) {
  return `${Math.max(10, value * 4)}%`;
}

function TemplateCompositePreview({
  compose,
  backgroundAssetId,
  scale,
  className = "",
}: {
  compose: typeof defaultCompose;
  backgroundAssetId?: string | null;
  scale: number;
  className?: string;
}) {
  return (
    <div
      className={`template-preview-canvas ${className}`}
      style={{
        backgroundImage: backgroundAssetId
          ? `linear-gradient(${hexToRgba(compose.overlay_color, compose.overlay_opacity)}, ${hexToRgba(compose.overlay_color, compose.overlay_opacity)}), url(${assetUrl(backgroundAssetId)})`
          : `linear-gradient(135deg, #42101f, #16030b)`,
      }}
    >
      {compose.title && (
        <strong className={compose.title_anchor_text ? "left-anchored-title" : ""} style={{
          left: `${compose.title_anchor_text ? titleStartX(compose) : compose.title_x}%`,
          top: `${compose.title_y}%`,
          color: compose.text_color,
          fontFamily: videoFonts[compose.font_family] || videoFonts.malgun,
          fontSize: `${Math.max(5, compose.title_size * scale)}px`,
        }}>{compose.title}</strong>
      )}
      {compose.artist_name && (
        <span style={{
          left: `${compose.artist_x}%`,
          top: `${compose.artist_y}%`,
          color: compose.artist_color,
          fontFamily: videoFonts[compose.artist_font_family] || videoFonts.malgun,
          fontSize: `${Math.max(4, compose.artist_size * scale)}px`,
        }}>{compose.artist_name}</span>
      )}
      {(compose.icon || compose.icon_image) && (
        <span
          className="template-dialog-icon"
          style={{
            left: `${compose.icon_x}%`,
            top: `${compose.icon_y}%`,
            color: compose.text_color,
            fontFamily: videoFonts[compose.font_family] || videoFonts.malgun,
            fontSize: `${Math.max(6, compose.icon_size * scale)}px`,
            width: compose.icon_image ? `${Math.max(8, compose.icon_size * scale)}px` : undefined,
            height: compose.icon_image ? `${Math.max(8, compose.icon_size * scale)}px` : undefined,
          }}
        >
          {compose.icon_image
            ? <img src={iconAssetUrl(compose.icon_image)} alt="" />
            : compose.icon}
        </span>
      )}
      {compose.show_visualizer && (
        <span
          className={`template-dialog-visualizer ${compose.visualizer_style}`}
          style={{
            left: `${compose.visualizer_x}%`,
            top: `${compose.visualizer_y}%`,
            width: `${compose.visualizer_width}%`,
            height: `${Math.max(6, compose.visualizer_height * scale)}px`,
            color: compose.visualizer_color,
          }}
        >
          {VISUALIZER_BARS.map((height, index) => (
            <i key={index} style={{ height: visualizerBarHeight(height) }} />
          ))}
        </span>
      )}
    </div>
  );
}

function isRenderFrameAsset(asset?: Asset) {
  if (!asset) return false;
  return asset.original_name.startsWith(RENDER_FRAME_PREFIX)
    || asset.original_name.startsWith("loop-preview-")
    || asset.metadata?.render_frame === true;
}

function hexToRgba(hex: string, alpha = 1) {
  const normalized = hex.replace("#", "").trim();
  const value = normalized.length === 3
    ? normalized.split("").map((char) => char + char).join("")
    : normalized.padEnd(6, "0").slice(0, 6);
  const number = Number.parseInt(value, 16);
  const r = (number >> 16) & 255;
  const g = (number >> 8) & 255;
  const b = number & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function drawLetterSpacedText(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  letterSpacing: number,
) {
  const chars = Array.from(text);
  const textWidth = chars.reduce((sum, char, index) => (
    sum + ctx.measureText(char).width + (index < chars.length - 1 ? letterSpacing : 0)
  ), 0);
  let cursor = x - textWidth / 2;
  chars.forEach((char, index) => {
    ctx.fillText(char, cursor, y);
    cursor += ctx.measureText(char).width + (index < chars.length - 1 ? letterSpacing : 0);
  });
}

function drawLeftAnchoredLetterSpacedText(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  letterSpacing: number,
) {
  let cursor = x;
  Array.from(text).forEach((char, index, chars) => {
    ctx.fillText(char, cursor, y);
    cursor += ctx.measureText(char).width + (index < chars.length - 1 ? letterSpacing : 0);
  });
}

async function fetchImageBitmap(src: string) {
  const url = src.startsWith("http") ? src : new URL(src, window.location.origin).href;
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error("이미지를 불러오지 못했습니다.");
    return createImageBitmap(await response.blob());
  } catch (error) {
    if (url.includes(":8000/") && window.location.port !== "8000") {
      throw new Error(
        "이미지 합성에 실패했습니다. API가 8000 포트로 직접 호출되어 CORS에 막혔을 수 있습니다. "
        + "frontend/.env에 VITE_API_BASE_URL을 넣지 말고 npm run dev를 재시작한 뒤 다시 시도하세요.",
        { cause: error },
      );
    }
    throw error;
  }
}

function drawCoverImage(
  ctx: CanvasRenderingContext2D,
  image: ImageBitmap,
  compose: typeof defaultCompose,
) {
  const scale = Math.max(VIDEO_CANVAS_WIDTH / image.width, VIDEO_CANVAS_HEIGHT / image.height);
  const width = image.width * scale;
  const height = image.height * scale;
  const x = (VIDEO_CANVAS_WIDTH - width) / 2;
  const y = (VIDEO_CANVAS_HEIGHT - height) / 2;
  ctx.save();
  ctx.filter = [
    `brightness(${Math.max(0, 1 + compose.brightness / 100)})`,
    `contrast(${Math.max(0, 1 + compose.contrast / 100)})`,
    `saturate(${Math.max(0, 1 + compose.saturation / 100)})`,
    `blur(${Math.max(0, compose.blur)}px)`,
  ].join(" ");
  ctx.drawImage(image, x, y, width, height);
  ctx.restore();
  ctx.fillStyle = hexToRgba(compose.overlay_color, compose.overlay_opacity);
  ctx.fillRect(0, 0, VIDEO_CANVAS_WIDTH, VIDEO_CANVAS_HEIGHT);
}

function drawTextOverlays(ctx: CanvasRenderingContext2D, compose: typeof defaultCompose) {
  ctx.save();
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.shadowColor = "rgba(0,0,0,.55)";
  ctx.shadowBlur = 5;
  ctx.shadowOffsetY = 2;

  if (compose.title) {
    const fontSize = compose.title_size;
    ctx.font = `700 ${fontSize}px ${videoFonts[compose.font_family] || videoFonts.malgun}`;
    ctx.fillStyle = compose.text_color;
    const titleX = VIDEO_CANVAS_WIDTH * (
      compose.title_anchor_text ? titleStartX(compose) : compose.title_x
    ) / 100;
    const drawTitle = compose.title_anchor_text
      ? drawLeftAnchoredLetterSpacedText
      : drawLetterSpacedText;
    drawTitle(
      ctx,
      compose.title,
      titleX,
      VIDEO_CANVAS_HEIGHT * compose.title_y / 100,
      fontSize * 0.12,
    );
  }

  if (compose.artist_name) {
    const fontSize = compose.artist_size;
    ctx.font = `400 ${fontSize}px ${videoFonts[compose.artist_font_family] || videoFonts.malgun}`;
    ctx.fillStyle = hexToRgba(compose.artist_color, 0.82);
    drawLetterSpacedText(
      ctx,
      compose.artist_name,
      VIDEO_CANVAS_WIDTH * compose.artist_x / 100,
      VIDEO_CANVAS_HEIGHT * compose.artist_y / 100,
      fontSize * 0.16,
    );
  }

  ctx.restore();
}

function roundedRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.fill();
}

function drawVisualizer(ctx: CanvasRenderingContext2D, compose: typeof defaultCompose) {
  if (!compose.show_visualizer) return;
  const width = VIDEO_CANVAS_WIDTH * compose.visualizer_width / 100;
  const height = compose.visualizer_height;
  const centerX = VIDEO_CANVAS_WIDTH * compose.visualizer_x / 100;
  const centerY = VIDEO_CANVAS_HEIGHT * compose.visualizer_y / 100;
  const left = centerX - width / 2;
  const top = centerY - height / 2;

  ctx.save();
  ctx.fillStyle = hexToRgba(compose.visualizer_color, 0.82);
  ctx.shadowColor = "rgba(0,0,0,.55)";
  ctx.shadowBlur = 5;
  ctx.shadowOffsetY = 2;

  if (compose.visualizer_style === "dots") {
    const dotSize = Math.min(14, Math.max(8, width / VISUALIZER_BARS.length * 0.45));
    const gap = Math.max(6, (width - dotSize * VISUALIZER_BARS.length) / Math.max(1, VISUALIZER_BARS.length - 1));
    VISUALIZER_BARS.forEach((value, index) => {
      const x = left + index * (dotSize + gap) + dotSize / 2;
      const y = centerY + (50 - Math.max(10, value * 4)) * height / 400;
      ctx.beginPath();
      ctx.arc(x, y, dotSize / 2, 0, Math.PI * 2);
      ctx.fill();
    });
  } else {
    const gap = compose.visualizer_style === "wave" ? 0 : VISUALIZER_GAP;
    const barWidth = Math.max(2, (width - gap * (VISUALIZER_BARS.length - 1)) / VISUALIZER_BARS.length);
    VISUALIZER_BARS.forEach((value, index) => {
      const barHeight = height * Math.max(10, value * 4) / 100 * (compose.visualizer_style === "wave" ? 0.45 : 1);
      const x = left + index * (barWidth + gap);
      const y = compose.visualizer_style === "wave"
        ? centerY - barHeight / 2
        : top + height - barHeight;
      roundedRect(ctx, x, y, barWidth, barHeight, compose.visualizer_style === "wave" ? barWidth / 2 : 4);
    });
  }

  ctx.restore();
}

async function composePreviewFrame(
  compose: typeof defaultCompose,
  coverSrc: string,
) {
  const canvas = document.createElement("canvas");
  canvas.width = VIDEO_CANVAS_WIDTH;
  canvas.height = VIDEO_CANVAS_HEIGHT;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("브라우저 캔버스를 초기화하지 못했습니다.");

  const cover = await fetchImageBitmap(coverSrc);
  drawCoverImage(ctx, cover, compose);
  if (compose.icon_image) {
    const icon = await fetchImageBitmap(iconAssetUrl(compose.icon_image));
    const size = compose.icon_size;
    ctx.drawImage(
      icon,
      VIDEO_CANVAS_WIDTH * compose.icon_x / 100 - size / 2,
      VIDEO_CANVAS_HEIGHT * compose.icon_y / 100 - size / 2,
      size,
      size,
    );
  } else if (compose.icon) {
    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = compose.text_color;
    ctx.font = `400 ${compose.icon_size}px ${videoFonts[compose.font_family] || videoFonts.malgun}`;
    ctx.fillText(compose.icon, VIDEO_CANVAS_WIDTH * compose.icon_x / 100, VIDEO_CANVAS_HEIGHT * compose.icon_y / 100);
    ctx.restore();
  }
  drawTextOverlays(ctx, compose);

  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((value) => value ? resolve(value) : reject(new Error("미리보기 이미지를 만들지 못했습니다.")), "image/png");
  });
  return new File([blob], `${RENDER_FRAME_PREFIX}${Date.now()}.png`, { type: "image/png" });
}

function VideoCreationPage() {
  const albumId = useAlbumId();
  const queryClient = useQueryClient();
  const album = useAlbum(albumId);
  const tracks = useTracks(albumId);
  const covers = useQuery({ queryKey: qk.covers(albumId), queryFn: () => api.listCovers(albumId) });
  const templatePreviews = useQuery({
    queryKey: qk.templatePreviews(albumId),
    queryFn: () => api.listTemplatePreviews(albumId),
  });
  const videoImageIcons = useQuery({
    queryKey: ["system", "video-icons"],
    queryFn: api.listVideoIcons,
  });
  const videoTemplates = useQuery({
    queryKey: ["albums", albumId, "video-templates"],
    queryFn: () => api.listVideoTemplates(albumId),
  });
  const eligible = (tracks.data || []).filter((track) => track.selected_generation_id);
  const [trackId, setTrackId] = useState("");
  const [coverId, setCoverId] = useState("");
  const [instruction, setInstruction] = useState("");
  const [candidateCount, setCandidateCount] = useState(1);
  const [compose, setCompose] = useState(defaultCompose);
  const [jobId, setJobId] = useState<string | null>(null);
  const [renderedAssetId, setRenderedAssetId] = useState("");
  const [previewMode, setPreviewMode] = useState<"design" | "video">("design");
  const [editorTab, setEditorTab] = useState<VideoEditorTab>("image");
  const [textEditorTarget, setTextEditorTarget] = useState<TextEditorTarget>("title");
  const [templateName, setTemplateName] = useState("");
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [workspace, setWorkspace] = useState<VideoWorkspace>("production");
  const [templateTitleSource, setTemplateTitleSource] = useState<TemplateTitleSource>("track");
  const [templateArtistSource, setTemplateArtistSource] = useState<TemplateArtistSource>("album");
  const [batchSelected, setBatchSelected] = useState<string[]>([]);
  const [templateDialogOpen, setTemplateDialogOpen] = useState(false);
  const [dialogTemplateId, setDialogTemplateId] = useState("");
  const [saveTemplateDialogOpen, setSaveTemplateDialogOpen] = useState(false);
  const [saveTemplateName, setSaveTemplateName] = useState("");
  const [batchWizardOpen, setBatchWizardOpen] = useState(false);
  const [batchPreviewTemplateId, setBatchPreviewTemplateId] = useState("");
  const [batchWizardStep, setBatchWizardStep] = useState(1);
  const [batchWizardRunning, setBatchWizardRunning] = useState(false);
  const [batchEditMode, setBatchEditMode] = useState<BatchEditMode>("template_only");
  const [batchMissingEditAction, setBatchMissingEditAction] = useState<"template" | "exclude">("template");
  const [batchFallbackTemplateId, setBatchFallbackTemplateId] = useState("");
  const [batchImageMode, setBatchImageMode] = useState<BatchImageMode>("generate_per_track");
  const [batchSharedImageId, setBatchSharedImageId] = useState("");
  const [batchImageInstruction, setBatchImageInstruction] = useState("");
  const [batchCandidateCount, setBatchCandidateCount] = useState(1);
  const [batchRetryImages, setBatchRetryImages] = useState(true);
  const [batchOverwriteVideos, setBatchOverwriteVideos] = useState(true);
  const [batchContinueOnError, setBatchContinueOnError] = useState(true);
  const [previewWidth, setPreviewWidth] = useState(960);
  const previewRef = useRef<HTMLDivElement | null>(null);
  const dragging = useRef<"title" | "artist" | "visualizer" | "icon" | null>(null);
  const dragStart = useRef<{
    pointerX: number;
    pointerY: number;
    elementX: number;
    elementY: number;
    moved: boolean;
  } | null>(null);
  const job = useJob(jobId);
  const assets = album.data?.assets || [];
  const videos = assets.filter((asset) => asset.type === "video");
  const editableCovers = (covers.data || []).filter((cover) => !isRenderFrameAsset(cover));
  const previewImages = templatePreviews.data || [];
  const videoForTrack = (id: string) => videos.find((asset) => asset.track_id === id);
  const completedCount = eligible.filter((track) => videoForTrack(track.id)).length;
  const selectedTrack = eligible.find((track) => track.id === trackId);
  const selectedCover = (workspace === "templates" ? previewImages : editableCovers)
    .find((cover) => cover.id === coverId);
  const currentVideo = trackId ? videoForTrack(trackId) : undefined;
  const isJobActive = ["pending", "running"].includes(job.data?.status || "");
  const previewScale = previewWidth / VIDEO_CANVAS_WIDTH;

  useEffect(() => { if (!trackId && eligible[0]) setTrackId(eligible[0].id); }, [eligible, trackId]);
  useEffect(() => {
    if (album.isLoading || covers.isLoading || templatePreviews.isLoading) return;
    if (workspace === "templates") {
      const selectedTemplate = videoTemplates.data?.find((template) => template.id === selectedTemplateId);
      const previewId = selectedTemplate?.preview_asset_id
        && previewImages.some((asset) => asset.id === selectedTemplate.preview_asset_id)
        ? selectedTemplate.preview_asset_id
        : previewImages[0]?.id || "";
      setCoverId(previewId);
      setRenderedAssetId("");
      setPreviewMode("design");
      return;
    }
    if (!trackId) return;
    const trackVideo = videoForTrack(trackId);
    const renderedFrameId = trackVideo?.metadata?.image_asset_id;
    const renderedFrame = typeof renderedFrameId === "string"
      ? assets.find((asset) => asset.id === renderedFrameId)
      : undefined;
    const videoCoverId = trackVideo?.metadata?.source_image_asset_id
      || renderedFrame?.metadata?.source_image_asset_id
      || renderedFrameId;
    const videoCover = typeof videoCoverId === "string"
      ? editableCovers.find((cover) => cover.id === videoCoverId)
      : undefined;
    const selectedAlbumCover = editableCovers.find((cover) => cover.id === album.data?.selected_cover_asset_id);
    const recoveryCoverId =
      videoCover?.id ||
      selectedAlbumCover?.id ||
      editableCovers.find((cover) => cover.track_id === trackId)?.id ||
      editableCovers.find(
        (cover) => cover.metadata?.compose && typeof cover.metadata.compose === "object",
      )?.id ||
      editableCovers[0]?.id ||
      "";
    const recoveryCover = editableCovers.find((cover) => cover.id === recoveryCoverId);
    const savedCompose = trackVideo?.metadata?.compose || recoveryCover?.metadata?.compose;

    setCoverId(recoveryCoverId);
    setRenderedAssetId(trackVideo?.id || "");
    setPreviewMode(trackVideo ? "video" : "design");
    if (savedCompose && typeof savedCompose === "object") {
      setCompose(composeWithDefaults(savedCompose as Partial<typeof defaultCompose>));
    } else {
      setCompose({
        ...defaultCompose,
        artist_name: album.data?.artist_name || "",
      });
    }
  }, [
    trackId,
    album.data?.artist_name,
    album.isLoading,
    covers.isLoading,
    templatePreviews.isLoading,
    workspace,
    selectedTemplateId,
    selectedTrack?.title,
  ]);

  useEffect(() => {
    if (job.data?.status === "succeeded") {
      queryClient.invalidateQueries({ queryKey: qk.covers(albumId) });
      queryClient.invalidateQueries({ queryKey: qk.templatePreviews(albumId) });
      queryClient.invalidateQueries({ queryKey: qk.album(albumId) });
      const assetIds = job.data.result?.asset_ids;
      const assetId = job.data.result?.asset_id;
      if (Array.isArray(assetIds) && assetIds[0]) {
        setCoverId(String(assetIds[0]));
        setPreviewMode("design");
      }
      if (assetId) {
        setRenderedAssetId(String(assetId));
        setPreviewMode("video");
      }
    }
  }, [job.data?.status, albumId, queryClient]);

  useEffect(() => {
    const preview = previewRef.current;
    if (!preview) return;
    const updateWidth = () => setPreviewWidth(preview.getBoundingClientRect().width);
    updateWidth();
    const observer = new ResizeObserver(updateWidth);
    observer.observe(preview);
    return () => observer.disconnect();
  }, [previewMode, selectedCover?.id]);

  const createImages = useMutation({
    mutationFn: () => workspace === "templates"
      ? api.generateTemplatePreviews(albumId, { instruction, aspect_ratio: "16:9", candidate_count: candidateCount })
      : api.generateCovers(albumId, { track_id: trackId || null, instruction, aspect_ratio: "16:9", candidate_count: candidateCount }),
    onSuccess: (result) => setJobId(result.job_id),
  });
  const upload = useMutation({
    mutationFn: (file: File) => workspace === "templates"
      ? api.uploadTemplatePreview(albumId, file)
      : api.uploadCover(albumId, file),
    onSuccess: (asset) => {
      setCoverId(asset.id);
      setRenderedAssetId("");
      setPreviewMode("design");
      queryClient.invalidateQueries({ queryKey: qk.covers(albumId) });
      queryClient.invalidateQueries({ queryKey: qk.templatePreviews(albumId) });
    },
  });
  const selectCover = useMutation({
    mutationFn: (id: string) => api.selectCover(albumId, id),
    onSuccess: (_, id) => {
      const nextCover = covers.data?.find((cover) => cover.id === id);
      const savedCompose = nextCover?.metadata?.compose;
      setCoverId(id);
      setRenderedAssetId("");
      setPreviewMode("design");
      setCompose(
        savedCompose && typeof savedCompose === "object"
          ? composeWithDefaults(savedCompose as Partial<typeof defaultCompose>)
          : { ...defaultCompose, artist_name: album.data?.artist_name || "" },
      );
      queryClient.invalidateQueries({ queryKey: qk.album(albumId) });
    },
  });
  const selectEditorImage = (id: string) => {
    if (workspace === "templates") {
      setCoverId(id);
      setRenderedAssetId("");
      setPreviewMode("design");
      return;
    }
    selectCover.mutate(id);
  };
  const saveCompose = useMutation({
    mutationFn: async () => {
      const saved = await api.composeImage(albumId, coverId, {
        ...compose,
        artist_name: compose.artist_name,
      });
      await api.selectCover(albumId, coverId);
      return saved;
    },
    onSuccess: () => {
      setPreviewMode("design");
      queryClient.invalidateQueries({ queryKey: qk.covers(albumId) });
      queryClient.invalidateQueries({ queryKey: qk.album(albumId) });
    },
  });
  const render = useMutation({
    mutationFn: async () => {
      await api.composeImage(albumId, coverId, {
        ...compose,
        artist_name: compose.artist_name,
      });
      const previewFrame = await composePreviewFrame(compose, assetUrl(coverId));
      const previewAsset = await api.uploadCover(albumId, previewFrame);
      return api.renderVideo(albumId, {
        mode: "static_loop",
        track_id: trackId,
        image_asset_id: previewAsset.id,
        resolution: "1920x1080",
        show_title: false,
        show_lyrics: false,
        show_visualizer: compose.show_visualizer,
        visualizer_style: compose.visualizer_style,
        visualizer_x: compose.visualizer_x,
        visualizer_y: compose.visualizer_y,
        visualizer_width: compose.visualizer_width,
        visualizer_height: compose.visualizer_height,
        visualizer_color: compose.visualizer_color,
        visualizer_opacity: 0.82,
        visualizer_background_color: "transparent",
        visualizer_background_opacity: 0,
        visualizer_show_background: false,
        visualizer_bar_count: VISUALIZER_BARS.length,
        visualizer_gap: VISUALIZER_GAP,
        visualizer_bars: [...VISUALIZER_BARS],
        compose: {
          ...compose,
          title: "",
          artist_name: "",
          icon: "",
          icon_image: "",
          show_visualizer: compose.show_visualizer,
          visualizer_color: compose.visualizer_color,
          visualizer_opacity: 0.82,
          visualizer_background_color: "transparent",
          visualizer_background_opacity: 0,
          visualizer_show_background: false,
          visualizer_bar_count: VISUALIZER_BARS.length,
          visualizer_gap: VISUALIZER_GAP,
          visualizer_bars: [...VISUALIZER_BARS],
        },
        loop_motion: "slow_zoom",
        fade_in_seconds: 1,
        fade_out_seconds: 1,
      });
    },
    onSuccess: (result) => setJobId(result.job_id),
  });
  const composeForTemplateSave = () => ({
    ...compose,
    title: templateTitleSource === "track"
      ? compose.title_anchor_text || compose.title
      : compose.title,
    title_anchor_text: "",
  });
  const createTemplate = useMutation({
    mutationFn: () => api.createVideoTemplate(albumId, {
      name: templateName.trim(),
      compose: composeForTemplateSave(),
      image_instruction: instruction,
      title_source: templateTitleSource,
      artist_source: templateArtistSource,
      preview_asset_id: coverId || null,
    }),
    onSuccess: (template) => {
      setSelectedTemplateId(template.id);
      setTemplateName(template.name);
      queryClient.invalidateQueries({
        queryKey: ["albums", albumId, "video-templates"],
      });
    },
  });
  const updateTemplate = useMutation({
    mutationFn: () => api.updateVideoTemplate(selectedTemplateId, {
      name: templateName.trim(),
      compose: composeForTemplateSave(),
      image_instruction: instruction,
      title_source: templateTitleSource,
      artist_source: templateArtistSource,
      preview_asset_id: coverId || null,
    }),
    onSuccess: () => queryClient.invalidateQueries({
      queryKey: ["albums", albumId, "video-templates"],
    }),
  });
  const deleteTemplate = useMutation({
    mutationFn: () => api.deleteVideoTemplate(selectedTemplateId),
    onSuccess: () => {
      setSelectedTemplateId("");
      setTemplateName("");
      queryClient.invalidateQueries({
        queryKey: ["albums", albumId, "video-templates"],
      });
    },
  });
  const saveCurrentAsTemplate = useMutation({
    mutationFn: () => api.createVideoTemplate(albumId, {
      name: saveTemplateName.trim(),
      compose: { ...compose, title_anchor_text: "" },
      image_instruction: "",
      title_source: "track",
      artist_source: compose.artist_name ? "template" : "hidden",
      preview_asset_id: null,
    }),
    onSuccess: (template) => {
      setSaveTemplateDialogOpen(false);
      setSaveTemplateName("");
      setSelectedTemplateId(template.id);
      queryClient.invalidateQueries({
        queryKey: ["albums", albumId, "video-templates"],
      });
    },
  });
  const renderBatch = useMutation({
    mutationFn: () => api.renderVideosBatch(albumId, {
      track_ids: batchSelected,
      template_id: batchFallbackTemplateId || null,
      edit_mode: batchEditMode,
      missing_edit_action: batchMissingEditAction,
      image_mode: batchImageMode,
      shared_image_asset_id: batchSharedImageId || null,
      image_instruction: batchImageInstruction,
      candidate_count: batchCandidateCount,
      retry_image_failures: batchRetryImages,
      overwrite_existing: batchOverwriteVideos,
      continue_on_error: batchContinueOnError,
    }),
    onSuccess: (result) => {
      setJobId(result.job_id);
      setBatchWizardRunning(true);
    },
  });

  const composeFromTemplate = (
    template: NonNullable<typeof videoTemplates.data>[number],
    useTrackValues: boolean,
    targetTrack = selectedTrack,
  ) => {
    const next = composeWithDefaults(template.compose as Partial<typeof defaultCompose>);
    const templateTitle = next.title;
    if (!useTrackValues) {
      if (template.title_source === "track") {
        next.title_anchor_text = templateTitle;
        next.title = targetTrack?.title || "트랙 제목 미리보기";
      }
      if (template.title_source === "hidden") next.title = "";
      if (template.artist_source === "album") next.artist_name = album.data?.artist_name || "앨범 아티스트";
      if (template.artist_source === "hidden") next.artist_name = "";
      return next;
    }
    if (template.title_source === "track") {
      next.title_anchor_text = templateTitle;
      next.title = targetTrack?.title || "트랙 제목";
    }
    if (template.title_source === "hidden") next.title = "";
    if (template.artist_source === "album") next.artist_name = album.data?.artist_name || "";
    if (template.artist_source === "hidden") next.artist_name = "";
    return next;
  };

  const applyTemplate = (templateId: string, targetWorkspace = workspace) => {
    const template = videoTemplates.data?.find((item) => item.id === templateId);
    if (!template) return;
    setSelectedTemplateId(template.id);
    setTemplateName(template.name);
    setInstruction(template.image_instruction || "");
    setTemplateTitleSource(template.title_source || "track");
    setTemplateArtistSource(template.artist_source || "album");
    setCompose(composeFromTemplate(template, targetWorkspace === "production"));
    if (targetWorkspace === "templates") {
      setCoverId(template.preview_asset_id || previewImages[0]?.id || "");
    }
    setRenderedAssetId("");
    setPreviewMode("design");
  };

  const openTemplateWorkspace = (templateId?: string) => {
    setWorkspace("templates");
    setPreviewMode("design");
    if (templateId) {
      applyTemplate(templateId, "templates");
      return;
    }
    setSelectedTemplateId("");
    setTemplateName("");
    setTemplateTitleSource("track");
    setTemplateArtistSource("album");
    setCompose({
      ...defaultCompose,
      title: "트랙 제목 미리보기",
      artist_name: album.data?.artist_name || "아티스트",
    });
  };

  const openProductionWorkspace = () => {
    setWorkspace("production");
  };

  const updateCompose = <K extends keyof typeof defaultCompose>(
    key: K,
    value: (typeof defaultCompose)[K],
  ) => {
    setCompose((current) => ({ ...current, [key]: value }));
    setRenderedAssetId("");
    setPreviewMode("design");
  };

  const selectTrack = (id: string) => {
    setTrackId(id);
    setJobId(null);
  };

  const moveElement = (
    element: "title" | "artist" | "visualizer" | "icon",
    event: ReactPointerEvent<HTMLElement>,
  ) => {
    const start = dragStart.current;
    if (dragging.current !== element || !start) return;
    const preview = event.currentTarget.parentElement;
    if (!preview) return;
    const bounds = preview.getBoundingClientRect();
    const deltaX = event.clientX - start.pointerX;
    const deltaY = event.clientY - start.pointerY;
    if (!start.moved && Math.hypot(deltaX, deltaY) < 4) return;
    start.moved = true;
    const x = Math.max(0, Math.min(100, start.elementX + (deltaX / bounds.width) * 100));
    const y = Math.max(0, Math.min(100, start.elementY + (deltaY / bounds.height) * 100));
    setCompose((current) => ({
      ...current,
      [`${element}_x`]: Number(x.toFixed(2)),
      [`${element}_y`]: Number(y.toFixed(2)),
    }));
    setRenderedAssetId("");
    setPreviewMode("design");
  };

  const startDragging = (
    element: "title" | "artist" | "visualizer" | "icon",
    event: ReactPointerEvent<HTMLElement>,
  ) => {
    if (element === "title" || element === "artist") {
      setEditorTab("text");
      setTextEditorTarget(element);
    } else {
      setEditorTab(element);
    }
    const [elementX, elementY] = element === "title"
      ? [compose.title_x, compose.title_y]
      : element === "artist"
        ? [compose.artist_x, compose.artist_y]
        : element === "icon"
          ? [compose.icon_x, compose.icon_y]
          : [compose.visualizer_x, compose.visualizer_y];
    dragging.current = element;
    dragStart.current = {
      pointerX: event.clientX,
      pointerY: event.clientY,
      elementX,
      elementY,
      moved: false,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const stopDragging = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    dragging.current = null;
    dragStart.current = null;
  };

  const moveToNextIncomplete = () => {
    if (!eligible.length) return;
    const currentIndex = eligible.findIndex((track) => track.id === trackId);
    const ordered = [
      ...eligible.slice(currentIndex + 1),
      ...eligible.slice(0, currentIndex + 1),
    ];
    const next = ordered.find((track) => !videoForTrack(track.id));
    if (next) selectTrack(next.id);
  };
  const dialogTemplate = videoTemplates.data?.find((template) => template.id === dialogTemplateId);
  const dialogCompose = dialogTemplate
    ? composeFromTemplate(dialogTemplate, true)
    : defaultCompose;
  const savedEditForTrack = (id: string) => editableCovers.find(
    (asset) => asset.track_id === id
      && asset.metadata?.compose
      && typeof asset.metadata.compose === "object",
  );
  const selectedImageForTrack = (id: string) => editableCovers.find(
    (asset) => asset.track_id === id,
  );
  const wizardTracks = eligible.filter((track) => batchSelected.includes(track.id));
  const batchPreviewTemplate = videoTemplates.data?.find(
    (template) => template.id === batchPreviewTemplateId,
  );
  const batchPreviewCompose = batchPreviewTemplate
    ? composeFromTemplate(batchPreviewTemplate, true, wizardTracks[0])
    : defaultCompose;
  const batchPlan = wizardTracks.map((track) => {
    const existingVideo = videoForTrack(track.id);
    const savedEdit = savedEditForTrack(track.id);
    const selectedImage = selectedImageForTrack(track.id);
    const skippedForVideo = Boolean(existingVideo && !batchOverwriteVideos);
    const usesSavedEdit = batchEditMode !== "template_only" && Boolean(savedEdit);
    const missingEdit = !usesSavedEdit && (
      batchEditMode === "saved_only"
      || (batchEditMode === "saved_then_template" && batchMissingEditAction === "exclude")
    );
    const editLabel = usesSavedEdit
      ? "직접 편집"
      : missingEdit
        ? "이번 작업 제외"
        : videoTemplates.data?.find((template) => template.id === batchFallbackTemplateId)?.name || "템플릿 필요";
    const imageLabel = batchImageMode === "generate_per_track"
      ? "새로 생성"
      : batchImageMode === "generate_shared"
        ? "공통 새 이미지"
        : batchImageMode === "shared_existing"
          ? "공통 기존 이미지"
          : selectedImage
            ? "기존 곡 이미지"
            : "새로 생성";
    return {
      track,
      existingVideo,
      savedEdit,
      selectedImage,
      skipped: skippedForVideo || missingEdit,
      editLabel,
      imageLabel,
      videoLabel: existingVideo
        ? batchOverwriteVideos ? "다시 만들기" : "건너뛰기"
        : "새 영상",
    };
  });
  const batchRunnablePlan = batchPlan.filter((item) => !item.skipped);
  const savedEditCount = batchPlan.filter((item) => item.savedEdit).length;
  const missingSavedEditCount = batchPlan.length - savedEditCount;
  const estimatedImageCount = batchImageMode === "generate_shared"
    ? Number(batchRunnablePlan.length > 0)
    : batchImageMode === "generate_per_track"
      ? batchRunnablePlan.length
      : batchImageMode === "selected_then_generate_per_track"
        ? batchRunnablePlan.filter((item) => !item.selectedImage).length
        : 0;
  const reusedImageCount = batchImageMode === "shared_existing"
    ? batchRunnablePlan.length
    : batchImageMode === "selected_then_generate_per_track"
      ? batchRunnablePlan.filter((item) => item.selectedImage).length
      : 0;
  const wizardNeedsTemplate = batchEditMode === "template_only"
    || (batchEditMode === "saved_then_template"
      && batchMissingEditAction === "template"
      && batchPlan.some((item) => !item.savedEdit));
  const wizardCanContinue = batchWizardStep === 1
    ? batchSelected.length > 0
    : batchWizardStep === 2
      ? !wizardNeedsTemplate || Boolean(batchFallbackTemplateId)
      : batchWizardStep === 3
        ? batchImageMode !== "shared_existing" || Boolean(batchSharedImageId)
        : batchRunnablePlan.length > 0;
  const batchActivity = job.data?.payload?.activity as {
    current_index?: number;
    total?: number;
    tracks?: BatchActivityTrack[];
  } | undefined;

  if (album.isLoading || tracks.isLoading) return <LoadingPage />;

  return (
    <>
      <PageHeader
        eyebrow="STEP 03 · LOOP VIDEO"
        title="플레이 루프 영상 작업실"
        description={workspace === "production"
          ? "트랙별 템플릿을 선택하거나 여러 곡을 한 번에 제작하세요."
          : "반복해서 사용할 편집 디자인과 곡별 텍스트 적용 규칙을 만드세요."}
        actions={
          <div className="video-workspace-switch">
            <button
              type="button"
              className={workspace === "production" ? "active" : ""}
              onClick={openProductionWorkspace}
            >
              <Film size={15} /> 영상 제작
            </button>
            <button
              type="button"
              className={workspace === "templates" ? "active" : ""}
              onClick={() => openTemplateWorkspace(selectedTemplateId || undefined)}
            >
              <SlidersHorizontal size={15} /> 템플릿 관리
            </button>
            {workspace === "production" && (
              <div className="video-progress-pill">
                <CircleCheck size={15} />
                <span>{completedCount} / {eligible.length} 완료</span>
              </div>
            )}
          </div>
        }
      />
      <JobPanel job={job.data} />
      <ErrorNotice error={
        createImages.error || upload.error || selectCover.error || saveCompose.error
        || render.error || videoImageIcons.error || videoTemplates.error || templatePreviews.error
        || createTemplate.error || updateTemplate.error
        || deleteTemplate.error || saveCurrentAsTemplate.error || renderBatch.error
      } />

      {!eligible.length && workspace === "production" ? (
        <EmptyState
          icon={<Music2 size={36} />}
          title="선택된 음원이 없습니다."
          description="노래 만들기에서 트랙별 최종 후보를 먼저 선택하세요."
          action={<Link className="button primary" to={`/albums/${albumId}/tracks`}>노래 만들기로 이동</Link>}
        />
      ) : (
        <>
          {workspace === "production" && <section className="panel video-batch-panel">
            <div>
              <span className="studio-kicker">AUTO PRODUCTION</span>
              <h2>선택 곡 자동 영상 제작</h2>
              <p>각 곡의 이미지 생성부터 템플릿 적용, 영상 렌더링까지 순서대로 실행합니다.</p>
            </div>
            <div className="video-batch-controls">
              <button
                type="button"
                className="button ghost"
                onClick={() => setBatchSelected(
                  batchSelected.length === eligible.length
                    ? []
                    : eligible.map((track) => track.id),
                )}
              >
                {batchSelected.length === eligible.length ? "전체 해제" : "모두 선택"}
              </button>
              <Button
                disabled={!batchSelected.length}
                icon={<Clapperboard size={17} />}
                onClick={() => {
                  setBatchWizardStep(1);
                  setBatchWizardRunning(false);
                  setBatchOverwriteVideos(true);
                  setBatchEditMode("template_only");
                  setBatchMissingEditAction("template");
                  setBatchImageMode("generate_per_track");
                  setBatchFallbackTemplateId((current) => current || videoTemplates.data?.[0]?.id || "");
                  setBatchWizardOpen(true);
                }}
              >
                선택 {batchSelected.length}곡 만들기
              </Button>
            </div>
          </section>}

          <div className="video-studio">
          <aside className="panel video-track-queue">
            <div className="studio-panel-head">
              <div>
                <span className="studio-kicker">{workspace === "production" ? "TRACK QUEUE" : "TEMPLATE LIBRARY"}</span>
                {workspace === "production" && <h2>트랙 목록</h2>}
              </div>
              {workspace === "production" && <b>{completedCount}/{eligible.length}</b>}
            </div>
            {workspace === "production" && (
              <div className="queue-progress"><span style={{ width: `${eligible.length ? completedCount / eligible.length * 100 : 0}%` }} /></div>
            )}
            {workspace === "templates" && (
              <div className="template-library-create">
                <Button variant="secondary" icon={<Plus size={15} />} onClick={() => openTemplateWorkspace()}>
                  새 템플릿
                </Button>
              </div>
            )}
            <div className={`video-track-list ${workspace === "templates" ? "template-library-list" : ""}`}>
              {workspace === "production" ? eligible.map((track) => {
                const video = videoForTrack(track.id);
                const active = track.id === trackId;
                const rendering = active && isJobActive && job.data?.type === "video_render";
                return (
                  <div key={track.id} className={`video-track-item ${active ? "active" : ""}`}>
                    <input
                      type="checkbox"
                      checked={batchSelected.includes(track.id)}
                      onChange={(e) => setBatchSelected((current) => (
                        e.target.checked
                          ? [...current, track.id]
                          : current.filter((id) => id !== track.id)
                      ))}
                      aria-label={`${track.title} 일괄 제작 선택`}
                    />
                    <button type="button" onClick={() => selectTrack(track.id)}>
                      <span className="track-number">{track.sequence}</span>
                      <span className="video-track-copy">
                        <strong>{track.title}</strong>
                        <small className={video ? "complete" : rendering ? "working" : ""}>
                          {video ? "영상 완료" : rendering ? "렌더링 중" : "영상 미생성"}
                        </small>
                      </span>
                      {video ? <CircleCheck size={18} /> : <Film size={17} />}
                    </button>
                  </div>
                );
              }) : (videoTemplates.data || []).map((template) => (
                <button
                  key={template.id}
                  type="button"
                  className={`template-library-item ${selectedTemplateId === template.id ? "active" : ""}`}
                  onClick={() => applyTemplate(template.id, "templates")}
                >
                  <span className="template-library-icon"><SlidersHorizontal size={16} /></span>
                  <span>
                    <strong>{template.name}</strong>
                    <small>
                      제목 {template.title_source === "track" ? "트랙값" : template.title_source === "hidden" ? "숨김" : "고정"}
                      {" · "}
                      아티스트 {template.artist_source === "album" ? "앨범값" : template.artist_source === "hidden" ? "숨김" : "고정"}
                    </small>
                  </span>
                </button>
              ))}
              {workspace === "templates" && !videoTemplates.isLoading && !videoTemplates.data?.length && (
                <p className="template-empty">아직 템플릿이 없습니다.<br />아래 버튼으로 첫 템플릿을 만드세요.</p>
              )}
            </div>
            {workspace === "production" && (
              <Button
                variant="ghost"
                disabled={completedCount === eligible.length}
                onClick={moveToNextIncomplete}
              >
                다음 미완료 곡
              </Button>
            )}
          </aside>

          <main className="panel video-canvas-panel">
            <div className="studio-panel-head preview-head">
              <div>
                <span className="studio-kicker">{workspace === "production" ? "NOW EDITING" : "TEMPLATE PREVIEW"}</span>
                <h2>{workspace === "production"
                  ? `${selectedTrack?.sequence}. ${selectedTrack?.title}`
                  : templateName || "새 편집 템플릿"}</h2>
              </div>
              {workspace === "production" && currentVideo && (
                <div className="preview-toggle">
                  <button className={previewMode === "design" ? "active" : ""} onClick={() => setPreviewMode("design")}>편집</button>
                  <button className={previewMode === "video" ? "active" : ""} onClick={() => setPreviewMode("video")}>완성 영상</button>
                </div>
              )}
            </div>

            {previewMode === "video" && (renderedAssetId || currentVideo) ? (
              <video className="video-player studio-player" controls src={assetUrl(renderedAssetId || currentVideo!.id)} />
            ) : selectedCover ? (
              <div
                ref={previewRef}
                className="composer-preview studio-preview"
                style={{
                  backgroundImage: `linear-gradient(${hexToRgba(compose.overlay_color, compose.overlay_opacity)}, ${hexToRgba(compose.overlay_color, compose.overlay_opacity)}), url(${assetUrl(selectedCover.id)})`,
                  filter: `brightness(${1 + compose.brightness / 100}) contrast(${1 + compose.contrast / 100}) saturate(${1 + compose.saturation / 100}) blur(${compose.blur}px)`,
                }}
              >
                <div
                  className={`preview-title draggable-overlay ${compose.title_anchor_text ? "left-anchored-title" : ""} ${editorTab === "text" && textEditorTarget === "title" ? "selected-overlay" : ""}`}
                  style={{
                    left: `${compose.title_anchor_text ? titleStartX(compose) : compose.title_x}%`,
                    top: `${compose.title_y}%`,
                    color: compose.text_color,
                    fontFamily: videoFonts[compose.font_family] || videoFonts.malgun,
                  }}
                  onPointerDown={(event) => startDragging("title", event)}
                  onPointerMove={(event) => moveElement("title", event)}
                  onPointerUp={stopDragging}
                  onPointerCancel={stopDragging}
                  title="드래그하여 제목 위치 이동"
                >
                  <strong style={{ fontSize: `${scaleVideoPixels(compose.title_size, previewScale, 8)}px` }}>{compose.title}</strong>
                </div>
                {compose.artist_name && (
                  <div
                    className={`preview-artist draggable-overlay ${editorTab === "text" && textEditorTarget === "artist" ? "selected-overlay" : ""}`}
                    style={{
                      left: `${compose.artist_x}%`,
                      top: `${compose.artist_y}%`,
                      color: compose.artist_color,
                      fontFamily: videoFonts[compose.artist_font_family] || videoFonts.malgun,
                      fontSize: `${scaleVideoPixels(compose.artist_size, previewScale, 6)}px`,
                    }}
                    onPointerDown={(event) => startDragging("artist", event)}
                    onPointerMove={(event) => moveElement("artist", event)}
                    onPointerUp={stopDragging}
                    onPointerCancel={stopDragging}
                    title="드래그하여 아티스트 위치 이동"
                  >
                    {compose.artist_name}
                  </div>
                )}
                {(compose.icon || compose.icon_image) && (
                  <div
                    className={`preview-icon draggable-overlay ${editorTab === "icon" ? "selected-overlay" : ""}`}
                    style={{
                      left: `${compose.icon_x}%`,
                      top: `${compose.icon_y}%`,
                      color: compose.text_color,
                      fontFamily: videoFonts[compose.font_family] || videoFonts.malgun,
                      fontSize: `${scaleVideoPixels(compose.icon_size, previewScale, 8)}px`,
                      width: compose.icon_image ? `${scaleVideoPixels(compose.icon_size, previewScale, 16)}px` : undefined,
                      height: compose.icon_image ? `${scaleVideoPixels(compose.icon_size, previewScale, 16)}px` : undefined,
                    }}
                    onPointerDown={(event) => startDragging("icon", event)}
                    onPointerMove={(event) => moveElement("icon", event)}
                    onPointerUp={stopDragging}
                    onPointerCancel={stopDragging}
                    title="드래그하여 아이콘 위치 이동"
                  >
                    {compose.icon_image
                      ? <img src={iconAssetUrl(compose.icon_image)} alt="" draggable={false} />
                      : compose.icon}
                  </div>
                )}
                {compose.show_visualizer && (
                  <div
                    className={`fake-visualizer draggable-overlay ${compose.visualizer_style} ${editorTab === "visualizer" ? "selected-overlay" : ""}`}
                    style={{
                      left: `${compose.visualizer_x}%`,
                      top: `${compose.visualizer_y}%`,
                      width: `${compose.visualizer_width}%`,
                      height: `${scaleVideoPixels(compose.visualizer_height, previewScale, 12)}px`,
                      gap: compose.visualizer_style === "wave" ? 0 : `${scaleVideoPixels(VISUALIZER_GAP, previewScale, 1)}px`,
                      color: compose.visualizer_color,
                    }}
                    onPointerDown={(event) => startDragging("visualizer", event)}
                    onPointerMove={(event) => moveElement("visualizer", event)}
                    onPointerUp={stopDragging}
                    onPointerCancel={stopDragging}
                    title="드래그하여 비주얼라이저 위치 이동"
                  >
                    {VISUALIZER_BARS.map((height, i) => <i key={i} style={{ height: visualizerBarHeight(height) }} />)}
                  </div>
                )}
              </div>
            ) : (
              <div className="studio-empty-preview">
                <ImageIcon size={42} />
                <strong>이미지를 선택하세요</strong>
                <span>오른쪽 패널에서 AI 이미지를 만들거나 파일을 업로드할 수 있습니다.</span>
              </div>
            )}

            <div className="canvas-footer">
              <div>
                <span>출력</span>
                <strong>1920 × 1080 · Static Loop</strong>
              </div>
              {(renderedAssetId || currentVideo) && (
                <a className="button secondary" href={assetUrl(renderedAssetId || currentVideo!.id)} download>
                  <Download size={17} /> MP4 다운로드
                </a>
              )}
            </div>
          </main>

          <aside className={`video-settings-column ${workspace === "templates" ? "template-settings-column" : ""}`}>
            <section className={`panel studio-editor-panel ${workspace === "templates" ? "template-editor-panel" : ""}`}>
              <div className="studio-editor-heading">
                <SectionTitle icon={<SlidersHorizontal />} title="편집" />
                <span>미리보기 요소를 눌러 바로 편집하세요.</span>
              </div>
              {workspace === "templates" ? (
                <div className="template-manager template-manager-detailed">
                  <div className="template-manager-heading">
                    <span className="studio-kicker">TEMPLATE SETTINGS</span>
                    <strong>{selectedTemplateId ? "템플릿 수정" : "새 템플릿 만들기"}</strong>
                  </div>
                  <Field label="템플릿 이름">
                    <input
                      value={templateName}
                      onChange={(e) => setTemplateName(e.target.value)}
                      placeholder="예: 비 오는 밤 기본 디자인"
                    />
                  </Field>
                  <div className="template-manager-actions">
                    <Button
                      variant={selectedTemplateId ? "ghost" : "secondary"}
                      disabled={!templateName.trim()}
                      loading={selectedTemplateId ? updateTemplate.isPending : createTemplate.isPending}
                      icon={<Save size={15} />}
                      onClick={() => selectedTemplateId ? updateTemplate.mutate() : createTemplate.mutate()}
                    >
                      {selectedTemplateId ? "변경 저장" : "템플릿 만들기"}
                    </Button>
                    {selectedTemplateId && (
                      <button
                        type="button"
                        className="icon-button template-delete"
                        title="템플릿 삭제"
                        onClick={() => {
                          if (confirm("이 편집 템플릿을 삭제할까요?")) deleteTemplate.mutate();
                        }}
                      >
                        <Trash2 size={16} />
                      </button>
                    )}
                  </div>
                </div>
              ) : (
                <div className="template-manager production-template-actions">
                  <Button
                    variant="secondary"
                    icon={<SlidersHorizontal size={15} />}
                    onClick={() => {
                      const firstId = videoTemplates.data?.[0]?.id || "";
                      setDialogTemplateId(selectedTemplateId || firstId);
                      setTemplateDialogOpen(true);
                    }}
                  >
                    템플릿 적용
                  </Button>
                  <Button
                    variant="ghost"
                    icon={<Save size={15} />}
                    onClick={() => {
                      setSaveTemplateName(`${selectedTrack?.title || "새"} 템플릿`);
                      setSaveTemplateDialogOpen(true);
                    }}
                  >
                    현재 편집을 템플릿으로 저장
                  </Button>
                </div>
              )}
              <div className="studio-editor-tabs" role="tablist" aria-label="영상 편집 도구">
                {([
                  ["image", "이미지"],
                  ["text", "텍스트"],
                  ["icon", "아이콘"],
                  ["visualizer", "비주얼"],
                  ["effects", "효과"],
                ] as const).map(([tab, label]) => (
                  <button
                    key={tab}
                    type="button"
                    className={editorTab === tab ? "active" : ""}
                    onClick={() => setEditorTab(tab)}
                  >
                    {label}
                  </button>
                ))}
              </div>

              <div className="studio-editor-content">
                {editorTab === "image" && (
                  <div className="studio-tool-pane">
                    {workspace === "templates" && (
                      <div className="template-preview-note">
                        <ImageIcon size={16} />
                        <span>아래 이미지는 템플릿 분위기를 확인하는 참고 배경입니다. 실제 영상 제작 이미지와 별도로 저장됩니다.</span>
                      </div>
                    )}
                    <Field label="추가 이미지 요청">
                      <input value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="예: 비 오는 카페, 따뜻한 실내 조명" />
                    </Field>
                    <div className="image-action-row">
                      <select value={candidateCount} onChange={(e) => setCandidateCount(Number(e.target.value))}>
                        <option value={1}>1개</option><option value={2}>2개</option><option value={4}>4개</option>
                      </select>
                      <Button loading={createImages.isPending || (isJobActive && job.data?.type === "cover_generate")} icon={<WandSparkles size={16} />} onClick={() => createImages.mutate()}>AI 생성</Button>
                      <label className="button secondary upload-button"><Upload size={16} /> 업로드<input type="file" accept="image/*" onChange={(e) => { const file = e.target.files?.[0]; if (file) upload.mutate(file); }} /></label>
                    </div>
                    <div className="studio-cover-strip">
                      {(workspace === "templates" ? previewImages : editableCovers).map((cover) => (
                        <button key={cover.id} className={coverId === cover.id ? "selected" : ""} onClick={() => selectEditorImage(cover.id)}>
                          <img src={assetUrl(cover.id)} alt={cover.original_name} />
                          {coverId === cover.id && <CircleCheck size={15} />}
                        </button>
                      ))}
                      {workspace === "templates"
                        ? !templatePreviews.isLoading && !previewImages.length && <span className="muted">아직 템플릿 참고 이미지가 없습니다.</span>
                        : !covers.isLoading && !editableCovers.length && <span className="muted">아직 생성된 이미지가 없습니다.</span>}
                    </div>
                  </div>
                )}

                {editorTab === "text" && (
                  <div className="studio-tool-pane">
                    <div className="editor-subtabs">
                      <button type="button" className={textEditorTarget === "title" ? "active" : ""} onClick={() => setTextEditorTarget("title")}>제목</button>
                      <button type="button" className={textEditorTarget === "artist" ? "active" : ""} onClick={() => setTextEditorTarget("artist")}>아티스트</button>
                    </div>
                    {textEditorTarget === "title" ? (
                      <>
                        {workspace === "templates" && (
                          <Field label="영상에 표시할 제목">
                            <select
                              value={templateTitleSource}
                              onChange={(e) => {
                                const source = e.target.value as TemplateTitleSource;
                                setTemplateTitleSource(source);
                                setCompose((current) => {
                                  if (source === "track") {
                                    return {
                                      ...current,
                                      title_anchor_text: current.title_anchor_text || current.title || "PLAY LIST",
                                      title: selectedTrack?.title || "트랙 제목 미리보기",
                                    };
                                  }
                                  if (source === "hidden") {
                                    return { ...current, title: "", title_anchor_text: "" };
                                  }
                                  return {
                                    ...current,
                                    title: current.title_anchor_text || current.title || "PLAY LIST",
                                    title_anchor_text: "",
                                  };
                                });
                                setRenderedAssetId("");
                                setPreviewMode("design");
                              }}
                            >
                              <option value="track">각 노래의 트랙 제목</option>
                              <option value="template">템플릿의 고정 제목</option>
                              <option value="hidden">제목 표시 안 함</option>
                            </select>
                          </Field>
                        )}
                        <Field label={workspace === "templates" && templateTitleSource !== "template" ? "미리보기 문자열" : "문자열"}>
                          <input
                            value={compose.title}
                            disabled={workspace === "templates" && templateTitleSource !== "template"}
                            onChange={(e) => updateCompose("title", e.target.value)}
                          />
                        </Field>
                        <div className="inline-fields">
                          <Field label="글꼴">
                            <select value={compose.font_family} onChange={(e) => updateCompose("font_family", e.target.value)}>
                              {videoFontOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                            </select>
                          </Field>
                          <Field label="색상"><input type="color" value={compose.text_color} onChange={(e) => updateCompose("text_color", e.target.value)} /></Field>
                        </div>
                        <Field label="크기"><input type="number" min={24} max={240} value={compose.title_size} onChange={(e) => updateCompose("title_size", Number(e.target.value))} /></Field>
                      </>
                    ) : (
                      <>
                        {workspace === "templates" && (
                          <Field label="영상에 표시할 아티스트">
                            <select
                              value={templateArtistSource}
                              onChange={(e) => {
                                const source = e.target.value as TemplateArtistSource;
                                setTemplateArtistSource(source);
                                if (source === "album") updateCompose("artist_name", album.data?.artist_name || "앨범 아티스트");
                                if (source === "hidden") updateCompose("artist_name", "");
                                if (source === "template" && !compose.artist_name) updateCompose("artist_name", "ARTIST");
                              }}
                            >
                              <option value="album">앨범 아티스트</option>
                              <option value="template">템플릿의 고정 아티스트</option>
                              <option value="hidden">아티스트 표시 안 함</option>
                            </select>
                          </Field>
                        )}
                        <Field label={workspace === "templates" && templateArtistSource !== "template" ? "미리보기 문자열" : "문자열"}>
                          <input
                            value={compose.artist_name}
                            disabled={workspace === "templates" && templateArtistSource !== "template"}
                            onChange={(e) => updateCompose("artist_name", e.target.value)}
                            placeholder="비워두면 표시하지 않음"
                          />
                        </Field>
                        <div className="inline-fields">
                          <Field label="글꼴">
                            <select value={compose.artist_font_family} onChange={(e) => updateCompose("artist_font_family", e.target.value)}>
                              {videoFontOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                            </select>
                          </Field>
                          <Field label="색상"><input type="color" value={compose.artist_color} onChange={(e) => updateCompose("artist_color", e.target.value)} /></Field>
                        </div>
                        <Field label="크기"><input type="number" min={12} max={120} value={compose.artist_size} onChange={(e) => updateCompose("artist_size", Number(e.target.value))} /></Field>
                      </>
                    )}
                    <p className="drag-help">미리보기에서 선택한 문자를 드래그해 위치를 조정하세요.</p>
                  </div>
                )}

                {editorTab === "icon" && (
                  <div className="studio-tool-pane">
                    <Field label="아이콘 / 기호">
                      <div className="icon-picker">
                        {videoIcons.map((icon) => (
                          <button
                            key={icon || "none"}
                            type="button"
                            className={!compose.icon_image && compose.icon === icon ? "selected" : ""}
                            onClick={() => {
                              updateCompose("icon", icon);
                              updateCompose("icon_image", "");
                            }}
                          >
                            {icon || "없음"}
                          </button>
                        ))}
                      </div>
                    </Field>
                    <Field label="이미지 아이콘">
                      <div className="image-icon-picker">
                        {(videoImageIcons.data || []).map(({ filename, label }) => (
                          <button
                            key={filename}
                            type="button"
                            className={compose.icon_image === filename ? "selected" : ""}
                            title={label}
                            onClick={() => {
                              updateCompose("icon_image", filename);
                              updateCompose("icon", "");
                            }}
                          >
                            <img src={iconAssetUrl(filename)} alt={label} />
                            <span>{label}</span>
                          </button>
                        ))}
                        {!videoImageIcons.isLoading && !videoImageIcons.data?.length && (
                          <span className="muted">아이콘 폴더에 이미지가 없습니다.</span>
                        )}
                      </div>
                    </Field>
                    {(compose.icon || compose.icon_image) && (
                      <label className="range-field compact">
                        <span>아이콘 크기 <b>{compose.icon_size}</b></span>
                        <input type="range" min={16} max={240} value={compose.icon_size} onChange={(e) => updateCompose("icon_size", Number(e.target.value))} />
                      </label>
                    )}
                    <p className="drag-help">미리보기의 아이콘을 드래그해 위치를 조정하세요.</p>
                  </div>
                )}

                {editorTab === "visualizer" && (
                  <div className="studio-tool-pane">
                    <label className="check-row"><input type="checkbox" checked={compose.show_visualizer} onChange={(e) => updateCompose("show_visualizer", e.target.checked)} /> 비주얼라이저 표시</label>
                    {compose.show_visualizer && (
                      <>
                        <div className="inline-fields">
                          <Field label="스타일">
                            <select value={compose.visualizer_style} onChange={(e) => updateCompose("visualizer_style", e.target.value)}>
                              <option value="bars">막대</option><option value="wave">파형</option><option value="dots">점</option>
                            </select>
                          </Field>
                          <Field label="색상"><input type="color" value={compose.visualizer_color} onChange={(e) => updateCompose("visualizer_color", e.target.value)} /></Field>
                        </div>
                        <Field label="너비 (%)"><input type="number" min={5} max={80} value={compose.visualizer_width} onChange={(e) => updateCompose("visualizer_width", Number(e.target.value))} /></Field>
                        <label className="range-field compact">
                          <span>높이 <b>{compose.visualizer_height}</b></span>
                          <input type="range" min={30} max={500} value={compose.visualizer_height} onChange={(e) => updateCompose("visualizer_height", Number(e.target.value))} />
                        </label>
                      </>
                    )}
                    <p className="drag-help">미리보기의 비주얼라이저를 드래그해 위치를 조정하세요.</p>
                  </div>
                )}

                {editorTab === "effects" && (
                  <div className="studio-tool-pane">
                    <div className="inline-fields">
                      <Field label="오버레이"><input type="color" value={compose.overlay_color} onChange={(e) => updateCompose("overlay_color", e.target.value)} /></Field>
                      <Field label="투명도"><input type="number" min={0} max={1} step={0.05} value={compose.overlay_opacity} onChange={(e) => updateCompose("overlay_opacity", Number(e.target.value))} /></Field>
                    </div>
                    {(["brightness", "contrast", "saturation", "blur"] as const).map((key) => (
                      <label className="range-field compact" key={key}>
                        <span>{({ brightness: "밝기", contrast: "대비", saturation: "채도", blur: "블러" })[key]} <b>{compose[key]}</b></span>
                        <input type="range" min={key === "blur" ? 0 : -50} max={key === "blur" ? 20 : 50} value={compose[key]} onChange={(e) => updateCompose(key, Number(e.target.value))} />
                      </label>
                    ))}
                  </div>
                )}
              </div>

              <div className="studio-editor-actions">
                {workspace === "production" && (
                  <Button variant="secondary" loading={saveCompose.isPending} disabled={!coverId} icon={<Save size={16} />} onClick={() => saveCompose.mutate()}>편집 저장</Button>
                )}
              </div>
            </section>

            {workspace === "production" && <section className="panel studio-render-card">
              <div>
                <span className="studio-kicker">OUTPUT</span>
                <h3>{currentVideo ? "영상을 다시 만들까요?" : "영상 생성 준비"}</h3>
                <p>{selectedTrack?.title} · Fade In/Out 1초</p>
              </div>
              <Button
                loading={render.isPending || (isJobActive && job.data?.type === "video_render")}
                disabled={!trackId || !coverId}
                icon={<Clapperboard size={18} />}
                onClick={() => render.mutate()}
              >
                {currentVideo ? "영상 다시 만들기" : "영상 만들기"}
              </Button>
            </section>}
          </aside>
        </div>
        </>
      )}
      {batchWizardOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => !batchWizardRunning && setBatchWizardOpen(false)}>
          <section className="panel batch-wizard-dialog" role="dialog" aria-modal="true" aria-label="선택 곡 영상 만들기" onMouseDown={(event) => event.stopPropagation()}>
            <header className="modal-header batch-wizard-header">
              <div>
                <span className="studio-kicker">BATCH VIDEO WIZARD</span>
                <h2>{batchWizardRunning ? "영상을 차근차근 만들고 있어요" : "선택한 곡을 한 번에 만들어요"}</h2>
                <p>{batchWizardRunning ? "이 창을 닫아도 작업은 계속됩니다." : "어려운 설정은 줄이고, 필요한 선택만 네 단계로 안내할게요."}</p>
              </div>
              <button type="button" className="icon-button" aria-label="닫기" onClick={() => setBatchWizardOpen(false)}><X size={18} /></button>
            </header>

            {!batchWizardRunning ? (
              <>
                <div className="batch-wizard-steps">
                  {["곡 선택", "편집 방식", "이미지 방식", "확인"].map((label, index) => {
                    const step = index + 1;
                    return (
                      <button
                        key={label}
                        type="button"
                        className={`${batchWizardStep === step ? "active" : ""} ${batchWizardStep > step ? "done" : ""}`}
                        onClick={() => step < batchWizardStep && setBatchWizardStep(step)}
                      >
                        <span>{batchWizardStep > step ? <CircleCheck size={14} /> : step}</span>
                        <b>{label}</b>
                      </button>
                    );
                  })}
                </div>
                <div className="batch-wizard-content">
                  {batchWizardStep === 1 && (
                    <div className="wizard-section">
                      <div className="wizard-section-head">
                        <div><h3>어떤 곡을 만들까요?</h3><p>이번 작업에 포함할 곡을 편하게 골라주세요.</p></div>
                        <button type="button" className="button ghost" onClick={() => setBatchSelected(batchSelected.length === eligible.length ? [] : eligible.map((track) => track.id))}>
                          {batchSelected.length === eligible.length ? "전체 해제" : "전체 선택"}
                        </button>
                      </div>
                      <div className="wizard-track-list">
                        {eligible.map((track) => {
                          const video = videoForTrack(track.id);
                          return (
                            <label key={track.id} className={batchSelected.includes(track.id) ? "selected" : ""}>
                              <input
                                type="checkbox"
                                checked={batchSelected.includes(track.id)}
                                onChange={(event) => setBatchSelected((current) => event.target.checked ? [...current, track.id] : current.filter((id) => id !== track.id))}
                              />
                              <span className="track-number">{track.sequence}</span>
                              <span><strong>{track.title}</strong><small>{video ? "완성된 영상이 있어요" : "새 영상 제작 가능"}</small></span>
                              {video ? <CircleCheck size={17} /> : <Film size={17} />}
                            </label>
                          );
                        })}
                      </div>
                      <div className="wizard-choice-row">
                        <button type="button" className={batchOverwriteVideos ? "selected" : ""} onClick={() => setBatchOverwriteVideos(true)}>
                          <RefreshCw size={20} /><strong>선택한 곡 모두 다시 만들기</strong><span>기존 영상도 새 설정으로 교체해요.</span>
                        </button>
                        <button type="button" className={!batchOverwriteVideos ? "selected" : ""} onClick={() => setBatchOverwriteVideos(false)}>
                          <CircleCheck size={20} /><strong>완성 영상은 건너뛰기</strong><span>이미 만든 영상은 그대로 두어요.</span>
                        </button>
                      </div>
                      <div className="wizard-friendly-summary"><Sparkles size={17} /><span>현재 선택한 {batchSelected.length}곡 중 <b>{batchPlan.filter((item) => !item.skipped).length}곡</b>을 제작할 예정이에요. 영상 제작은 Suno 음원 크레딧을 추가로 사용하지 않습니다.</span></div>
                    </div>
                  )}

                  {batchWizardStep === 2 && (
                    <div className="wizard-section">
                      <div className="wizard-section-head"><div><h3>영상 디자인을 어떻게 맞출까요?</h3><p>직접 꾸민 곡은 그대로 살릴지, 모든 곡을 같은 모습으로 맞출지만 선택하면 됩니다.</p></div></div>
                      <div className="wizard-choice-grid edit-mode-simple">
                        <button type="button" className={batchEditMode === "template_only" ? "selected" : ""} onClick={() => setBatchEditMode("template_only")}>
                          <SlidersHorizontal size={22} />
                          <strong>모든 곡을 같은 디자인으로 통일</strong>
                          <span>직접 편집한 내용도 사용하지 않고, 선택한 템플릿 하나로 전체 분위기를 맞춰요.</span>
                          <em>추천</em>
                        </button>
                        <button
                          type="button"
                          className={batchEditMode === "saved_then_template" ? "selected" : ""}
                          onClick={() => {
                            setBatchEditMode("saved_then_template");
                            setBatchMissingEditAction("template");
                          }}
                        >
                          <Save size={22} />
                          <strong>직접 편집한 곡은 그대로 유지</strong>
                          <span>
                            직접 꾸민 {savedEditCount}곡은 현재 모습으로 만들고,
                             나머지 {missingSavedEditCount}곡에는 아래 템플릿을 적용해요.
                           </span>
                        </button>
                      </div>
                      {wizardNeedsTemplate && (
                        <div className="wizard-template-select">
                          <div className="wizard-template-select-head">
                            <strong>{batchEditMode === "template_only" ? "전체 곡에 적용할 템플릿" : `${missingSavedEditCount}곡에 적용할 템플릿`}</strong>
                            <span>이미지를 눌러 선택하세요.</span>
                          </div>
                          <div className="wizard-template-cards">
                            {(videoTemplates.data || []).map((template) => {
                              const templateCompose = composeFromTemplate(template, true, wizardTracks[0]);
                              return (
                                <div
                                  key={template.id}
                                  className={`wizard-template-card ${batchFallbackTemplateId === template.id ? "selected" : ""}`}
                                >
                                  <button
                                    type="button"
                                    className="wizard-template-select-button"
                                    onClick={() => setBatchFallbackTemplateId(template.id)}
                                  >
                                    <span className="wizard-template-image">
                                      <TemplateCompositePreview
                                        compose={templateCompose}
                                        backgroundAssetId={template.preview_asset_id}
                                        scale={0.11}
                                        className="wizard-template-composite"
                                      />
                                      {batchFallbackTemplateId === template.id && (
                                        <CircleCheck className="wizard-template-selected" size={18} />
                                      )}
                                    </span>
                                    <strong>{template.name}</strong>
                                  </button>
                                  <button
                                    type="button"
                                    className="wizard-template-zoom"
                                    aria-label={`${template.name} 크게 보기`}
                                    title="크게 보기"
                                    onClick={() => setBatchPreviewTemplateId(template.id)}
                                  >
                                    <Maximize2 size={15} />
                                  </button>
                                </div>
                              );
                            })}
                            {!videoTemplates.isLoading && !videoTemplates.data?.length && (
                              <div className="template-dialog-empty">먼저 편집 템플릿을 하나 만들어 주세요.</div>
                            )}
                          </div>
                        </div>
                      )}
                      <div className="wizard-plan-chips">
                        {batchPlan.map((item) => (
                          <span key={item.track.id} className={item.editLabel === "템플릿 필요" ? "warning" : ""}>
                            <b>{item.track.title}</b>{item.editLabel}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  {batchWizardStep === 3 && (
                    <div className="wizard-section">
                      <div className="wizard-section-head"><div><h3>영상 배경 이미지는 어떻게 준비할까요?</h3><p>곡마다 다른 장면을 만들거나, 한 장으로 앨범의 통일감을 줄 수 있어요.</p></div></div>
                      <div className="wizard-choice-grid image-mode-grid">
                        <button type="button" className={batchImageMode === "generate_per_track" ? "selected" : ""} onClick={() => setBatchImageMode("generate_per_track")}>
                          <ImageIcon size={22} /><strong>곡마다 새 이미지</strong><span>각 곡의 프롬프트에 맞춰 서로 다른 이미지를 만들어요.</span><em>추천</em>
                        </button>
                        <button type="button" className={batchImageMode === "selected_then_generate_per_track" ? "selected" : ""} onClick={() => setBatchImageMode("selected_then_generate_per_track")}>
                          <CircleCheck size={22} /><strong>기존 곡 이미지 우선</strong><span>이미지가 있는 곡은 재사용하고, 없는 곡만 새로 만들어요.</span>
                        </button>
                        <button type="button" className={batchImageMode === "generate_shared" ? "selected" : ""} onClick={() => setBatchImageMode("generate_shared")}>
                          <Sparkles size={22} /><strong>공통 이미지 하나 생성</strong><span>한 장을 새로 만들어 모든 곡에 함께 사용해요.</span>
                        </button>
                        <button type="button" className={batchImageMode === "shared_existing" ? "selected" : ""} onClick={() => setBatchImageMode("shared_existing")}>
                          <Library size={22} /><strong>기존 이미지 하나 선택</strong><span>갤러리의 한 장을 모든 곡에 공통으로 사용해요.</span>
                        </button>
                      </div>
                      {batchImageMode === "shared_existing" && (
                        <div className="wizard-image-gallery">
                          {editableCovers.map((asset) => (
                            <button key={asset.id} type="button" className={batchSharedImageId === asset.id ? "selected" : ""} onClick={() => setBatchSharedImageId(asset.id)}>
                              <img src={assetUrl(asset.id)} alt={asset.original_name} />
                              {batchSharedImageId === asset.id && <CircleCheck size={17} />}
                            </button>
                          ))}
                        </div>
                      )}
                      {batchImageMode !== "shared_existing" && (
                        <div className="wizard-generation-options">
                          <Field label="모든 이미지에 덧붙일 요청">
                            <input value={batchImageInstruction} onChange={(event) => setBatchImageInstruction(event.target.value)} placeholder="예: 따뜻한 필름 톤, 인물은 화면 중앙을 피하기" />
                          </Field>
                          <Field label="곡별 후보 수">
                            <select value={batchCandidateCount} onChange={(event) => setBatchCandidateCount(Number(event.target.value))}>
                              <option value={1}>1개, 빠르게</option><option value={2}>2개</option><option value={4}>4개, 다양하게</option>
                            </select>
                          </Field>
                          <label className="check-row"><input type="checkbox" checked={batchRetryImages} onChange={(event) => setBatchRetryImages(event.target.checked)} /> 이미지 생성 실패 시 한 번 더 시도</label>
                        </div>
                      )}
                      <div className="wizard-friendly-summary"><ImageIcon size={17} /><span>새 이미지 요청은 약 <b>{estimatedImageCount}회</b>, 기존 이미지는 <b>{reusedImageCount}곡</b>에서 재사용할 예정이에요.</span></div>
                    </div>
                  )}

                  {batchWizardStep === 4 && (
                    <div className="wizard-section">
                      <div className="wizard-section-head"><div><h3>준비가 끝났어요</h3><p>아래 계획대로 진행할게요. 마지막으로 가볍게 확인해 주세요.</p></div></div>
                      <div className="wizard-plan-table">
                        <div className="head"><span>곡</span><span>편집</span><span>이미지</span><span>기존 영상</span></div>
                        {batchPlan.map((item) => (
                          <div key={item.track.id} className={item.skipped ? "skipped" : ""}>
                            <span><b>{item.track.sequence}</b>{item.track.title}</span>
                            <span>{item.editLabel}</span><span>{item.imageLabel}</span><span>{item.videoLabel}</span>
                          </div>
                        ))}
                      </div>
                      <div className="wizard-summary-cards">
                        <span><Clapperboard size={18} /><b>{batchRunnablePlan.length}</b>영상 제작</span>
                        <span><ImageIcon size={18} /><b>{estimatedImageCount}</b>이미지 생성</span>
                        <span><Library size={18} /><b>{reusedImageCount}</b>이미지 재사용</span>
                        <span><Pause size={18} /><b>{batchPlan.length - batchRunnablePlan.length}</b>건너뜀</span>
                      </div>
                      <label className="check-row wizard-continue-check"><input type="checkbox" checked={batchContinueOnError} onChange={(event) => setBatchContinueOnError(event.target.checked)} /> 한 곡이 실패해도 다음 곡은 계속 만들기</label>
                    </div>
                  )}
                </div>
                <footer className="modal-actions batch-wizard-actions">
                  <Button variant="ghost" onClick={() => batchWizardStep === 1 ? setBatchWizardOpen(false) : setBatchWizardStep((step) => step - 1)}>
                    {batchWizardStep === 1 ? "나중에 하기" : "이전"}
                  </Button>
                  {batchWizardStep < 4 ? (
                    <Button disabled={!wizardCanContinue} onClick={() => setBatchWizardStep((step) => step + 1)}>다음</Button>
                  ) : (
                    <Button loading={renderBatch.isPending} disabled={!wizardCanContinue} icon={<Clapperboard size={17} />} onClick={() => renderBatch.mutate()}>
                      {batchRunnablePlan.length}곡 영상 만들기
                    </Button>
                  )}
                </footer>
              </>
            ) : (
              <div className="batch-progress-view">
                <div className="batch-progress-hero">
                  {job.data?.status === "succeeded" ? <CircleCheck size={34} /> : job.data?.status === "failed" ? <X size={34} /> : <LoaderCircle size={34} className="spin" />}
                  <div><strong>{job.data?.status === "succeeded" ? "모든 작업을 확인했어요" : job.data?.status === "failed" ? "작업이 중단됐어요" : `전체 ${Math.min((batchActivity?.current_index || 0) + 1, batchActivity?.total || batchSelected.length)} / ${batchActivity?.total || batchSelected.length}`}</strong><span>{job.data?.status === "succeeded" ? "완성된 영상은 트랙 목록에서 바로 확인할 수 있어요." : job.data?.error_message || "이미지와 편집, 렌더링을 순서대로 진행하고 있어요."}</span></div>
                </div>
                <div className="batch-progress-bar"><span style={{ width: `${job.data?.progress || 3}%` }} /></div>
                <div className="batch-progress-list">
                  {(batchActivity?.tracks || wizardTracks.map((track) => ({ track_id: track.id, title: track.title, status: "waiting", message: "대기 중" }))).map((item) => (
                    <div key={item.track_id} className={item.status}>
                      <span className="batch-status-icon">
                        {item.status === "completed" ? <CircleCheck size={17} /> : item.status === "failed" ? <X size={17} /> : item.status === "waiting" || item.status === "skipped" ? <Pause size={17} /> : <LoaderCircle size={17} className="spin" />}
                      </span>
                      <span><strong>{item.title}</strong><small>{item.message}</small></span>
                      <b>{({
                        waiting: "대기",
                        image_generating: "이미지",
                        image_ready: "이미지 준비",
                        template_applying: "편집 적용",
                        rendering: "렌더링",
                        completed: "완료",
                        failed: "실패",
                        skipped: "건너뜀",
                      } as Record<string, string>)[item.status] || item.status}</b>
                    </div>
                  ))}
                </div>
                <footer className="modal-actions">
                  <Button variant="ghost" onClick={() => setBatchWizardOpen(false)}>{["succeeded", "failed"].includes(job.data?.status || "") ? "닫기" : "백그라운드에서 계속"}</Button>
                </footer>
              </div>
            )}
          </section>
        </div>
      )}
      {batchPreviewTemplate && (
        <div className="modal-backdrop template-zoom-backdrop" role="presentation" onMouseDown={() => setBatchPreviewTemplateId("")}>
          <section
            className="panel template-zoom-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={`${batchPreviewTemplate.name} 템플릿 크게 보기`}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header className="modal-header">
              <div>
                <span className="studio-kicker">TEMPLATE PREVIEW</span>
                <h2>{batchPreviewTemplate.name}</h2>
                <p>제목, 아티스트, 아이콘과 비주얼라이저가 영상에 배치되는 모습을 확인하세요.</p>
              </div>
              <button type="button" className="icon-button" aria-label="닫기" onClick={() => setBatchPreviewTemplateId("")}><X size={18} /></button>
            </header>
            <div className="template-zoom-content">
              <TemplateCompositePreview
                compose={batchPreviewCompose}
                backgroundAssetId={batchPreviewTemplate.preview_asset_id}
                scale={0.42}
                className="template-zoom-canvas"
              />
              <div className="template-dialog-summary">
                <span>제목 <b>{batchPreviewTemplate.title_source === "track" ? "곡마다 트랙 제목" : batchPreviewTemplate.title_source === "hidden" ? "표시 안 함" : "템플릿 고정"}</b></span>
                <span>아티스트 <b>{batchPreviewTemplate.artist_source === "album" ? "앨범 아티스트" : batchPreviewTemplate.artist_source === "hidden" ? "표시 안 함" : "템플릿 고정"}</b></span>
              </div>
            </div>
            <footer className="modal-actions">
              <Button variant="ghost" onClick={() => setBatchPreviewTemplateId("")}>닫기</Button>
              <Button
                icon={<CircleCheck size={16} />}
                onClick={() => {
                  setBatchFallbackTemplateId(batchPreviewTemplate.id);
                  setBatchPreviewTemplateId("");
                }}
              >
                이 템플릿 선택
              </Button>
            </footer>
          </section>
        </div>
      )}
      {templateDialogOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setTemplateDialogOpen(false)}>
          <section className="panel template-apply-dialog" role="dialog" aria-modal="true" aria-label="편집 템플릿 적용" onMouseDown={(event) => event.stopPropagation()}>
            <header className="modal-header">
              <div>
                <span className="studio-kicker">APPLY TEMPLATE</span>
                <h2>편집 템플릿 적용</h2>
                <p>미리보기를 확인한 후 현재 곡 편집에 적용하세요.</p>
              </div>
              <button type="button" className="icon-button" aria-label="닫기" onClick={() => setTemplateDialogOpen(false)}><X size={18} /></button>
            </header>
            <div className="template-dialog-body">
              <div className="template-dialog-list">
                {(videoTemplates.data || []).map((template) => (
                  <button
                    key={template.id}
                    type="button"
                    className={dialogTemplateId === template.id ? "active" : ""}
                    onClick={() => setDialogTemplateId(template.id)}
                  >
                    <span className="template-library-icon"><SlidersHorizontal size={16} /></span>
                    <span><strong>{template.name}</strong><small>{template.title_source === "track" ? "트랙 제목" : template.title_source === "hidden" ? "제목 숨김" : "고정 제목"}</small></span>
                    {dialogTemplateId === template.id && <CircleCheck size={17} />}
                  </button>
                ))}
                {!videoTemplates.isLoading && !videoTemplates.data?.length && (
                  <div className="template-dialog-empty">사용할 템플릿이 없습니다.</div>
                )}
              </div>
              <div className="template-dialog-preview">
                {dialogTemplate ? (
                  <>
                    <TemplateCompositePreview
                      compose={dialogCompose}
                      backgroundAssetId={dialogTemplate.preview_asset_id}
                      scale={0.28}
                    />
                    <div className="template-dialog-summary">
                      <span>제목 <b>{dialogTemplate.title_source === "track" ? "현재 트랙 제목" : dialogTemplate.title_source === "hidden" ? "표시 안 함" : "템플릿 고정"}</b></span>
                      <span>아티스트 <b>{dialogTemplate.artist_source === "album" ? "앨범 아티스트" : dialogTemplate.artist_source === "hidden" ? "표시 안 함" : "템플릿 고정"}</b></span>
                    </div>
                  </>
                ) : (
                  <div className="template-dialog-empty">왼쪽에서 템플릿을 선택하세요.</div>
                )}
              </div>
            </div>
            <footer className="modal-actions">
              <Button variant="ghost" onClick={() => setTemplateDialogOpen(false)}>취소</Button>
              <Button
                disabled={!dialogTemplate}
                icon={<CircleCheck size={16} />}
                onClick={() => {
                  if (!dialogTemplate) return;
                  applyTemplate(dialogTemplate.id, "production");
                  setTemplateDialogOpen(false);
                }}
              >
                현재 편집에 적용
              </Button>
            </footer>
          </section>
        </div>
      )}
      {saveTemplateDialogOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setSaveTemplateDialogOpen(false)}>
          <section className="panel save-template-dialog" role="dialog" aria-modal="true" aria-label="현재 편집을 템플릿으로 저장" onMouseDown={(event) => event.stopPropagation()}>
            <header className="modal-header">
              <div><span className="studio-kicker">SAVE AS TEMPLATE</span><h2>현재 편집을 템플릿으로 저장</h2></div>
              <button type="button" className="icon-button" aria-label="닫기" onClick={() => setSaveTemplateDialogOpen(false)}><X size={18} /></button>
            </header>
            <Field label="템플릿 이름">
              <input value={saveTemplateName} onChange={(event) => setSaveTemplateName(event.target.value)} autoFocus />
            </Field>
            <p className="modal-help">현재 제목 위치, 글꼴, 아이콘, 비주얼라이저와 이미지 효과가 저장됩니다. 제목은 적용할 곡의 트랙 제목으로 자동 변경됩니다.</p>
            <footer className="modal-actions">
              <Button variant="ghost" onClick={() => setSaveTemplateDialogOpen(false)}>취소</Button>
              <Button loading={saveCurrentAsTemplate.isPending} disabled={!saveTemplateName.trim()} icon={<Save size={16} />} onClick={() => saveCurrentAsTemplate.mutate()}>템플릿 저장</Button>
            </footer>
          </section>
        </div>
      )}
    </>
  );
}

function formatDuration(value: number) {
  const total = Math.max(0, Math.round(value || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function AlbumVideoPage() {
  const albumId = useAlbumId();
  const queryClient = useQueryClient();
  const album = useAlbum(albumId);
  const tracks = useTracks(albumId);
  const [orderedAssetIds, setOrderedAssetIds] = useState<string[]>([]);
  const [transition, setTransition] = useState<"none" | "fade">("fade");
  const [transitionSeconds, setTransitionSeconds] = useState(1);
  const [resolution, setResolution] = useState<"1920x1080" | "1280x720">("1920x1080");
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(jobId);
  const trackOrder = new Map((tracks.data || []).map((track) => [track.id, track.sequence]));
  const trackById = new Map((tracks.data || []).map((track) => [track.id, track]));
  const trackVideos = (album.data?.assets || [])
    .filter((asset) => asset.type === "video" && asset.track_id)
    .sort((left, right) => right.created_at.localeCompare(left.created_at))
    .filter((asset, index, all) => all.findIndex((item) => item.track_id === asset.track_id) === index)
    .sort((left, right) => (trackOrder.get(left.track_id || "") || 0) - (trackOrder.get(right.track_id || "") || 0));
  const videoById = new Map(trackVideos.map((video) => [video.id, video]));
  const displayedVideos = [
    ...orderedAssetIds.map((id) => videoById.get(id)).filter((video): video is Asset => Boolean(video)),
    ...trackVideos.filter((video) => !orderedAssetIds.includes(video.id)),
  ];
  const albumVideo = (album.data?.assets || [])
    .filter((asset) => asset.type === "album_video")
    .sort((left, right) => right.created_at.localeCompare(left.created_at))[0];
  const orderKey = trackVideos.map((video) => video.id).join(",");

  useEffect(() => {
    setOrderedAssetIds((current) => {
      const available = new Set(trackVideos.map((video) => video.id));
      const preserved = current.filter((id) => available.has(id));
      const added = trackVideos.map((video) => video.id).filter((id) => !preserved.includes(id));
      return [...preserved, ...added];
    });
  }, [orderKey]);

  useEffect(() => {
    if (job.data?.status === "succeeded") {
      queryClient.invalidateQueries({ queryKey: qk.album(albumId) });
    }
  }, [albumId, job.data?.status, queryClient]);

  const combine = useMutation({
    mutationFn: () => api.combineAlbumVideos(albumId, {
      video_asset_ids: orderedAssetIds,
      transition,
      transition_seconds: transition === "fade" ? transitionSeconds : 0,
      resolution,
    }),
    onSuccess: (accepted) => setJobId(accepted.job_id),
  });

  const move = (index: number, offset: number) => {
    const nextIndex = index + offset;
    if (nextIndex < 0 || nextIndex >= orderedAssetIds.length) return;
    setOrderedAssetIds((current) => {
      const next = [...current];
      [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
      return next;
    });
  };
  const toggle = (assetId: string) => {
    setOrderedAssetIds((current) =>
      current.includes(assetId)
        ? current.filter((id) => id !== assetId)
        : [...current, assetId],
    );
  };
  const activeJob = job.data && !["succeeded", "failed"].includes(job.data.status);
  const resultAssetId = typeof job.data?.result?.asset_id === "string"
    ? job.data.result.asset_id
    : albumVideo?.id;

  if (album.isLoading || tracks.isLoading) return <LoadingPage />;
  if (!album.data) return <ErrorNotice error={album.error} />;

  return (
    <>
      <PageHeader
        eyebrow="STEP 04 · ALBUM VIDEO"
        title="전체 영상 만들기"
        description="완성된 트랙 영상을 원하는 순서로 연결해 하나의 영상으로 만드세요."
      />
      <ErrorNotice error={combine.error || job.error} />
      <JobPanel job={job.data} />
      <div className="album-video-layout">
        <section className="panel album-video-tracks">
          <div className="section-heading">
            <div><h2>트랙 영상 선택</h2><p>앨범 순서가 기본이며 제외하거나 순서를 변경할 수 있습니다.</p></div>
            <span className="selection-count">{orderedAssetIds.length} / {trackVideos.length}</span>
          </div>
          {!trackVideos.length ? (
            <EmptyState
              icon={<Film size={38} />}
              title="완성된 트랙 영상이 없습니다."
              description="먼저 루프 영상 만들기에서 트랙별 영상을 제작하세요."
              action={<Link className="button primary" to={`/albums/${albumId}/video`}>루프 영상 만들기</Link>}
            />
          ) : (
            <div className="album-video-order">
              {displayedVideos.map((video) => {
                const selectedIndex = orderedAssetIds.indexOf(video.id);
                const selected = selectedIndex >= 0;
                const track = trackById.get(video.track_id || "");
                return (
                  <article key={video.id} className={selected ? "selected" : ""}>
                    <label>
                      <input type="checkbox" checked={selected} onChange={() => toggle(video.id)} />
                      <span className="track-number">{track?.sequence || "-"}</span>
                      <span><strong>{track?.title || video.original_name}</strong><small>{selected ? `${selectedIndex + 1}번째 재생` : "제외됨"}</small></span>
                    </label>
                    <div className="order-buttons">
                      <button type="button" aria-label="위로 이동" disabled={!selected || selectedIndex === 0} onClick={() => move(selectedIndex, -1)}><ChevronUp size={17} /></button>
                      <button type="button" aria-label="아래로 이동" disabled={!selected || selectedIndex === orderedAssetIds.length - 1} onClick={() => move(selectedIndex, 1)}><ChevronDown size={17} /></button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
        <aside className="panel album-video-settings">
          <SectionTitle icon={<SlidersHorizontal />} title="연결 설정" />
          <Field label="곡 사이 전환">
            <select value={transition} onChange={(event) => setTransition(event.target.value as "none" | "fade")}>
              <option value="fade">페이드</option>
              <option value="none">전환 없음</option>
            </select>
          </Field>
          {transition === "fade" && <Field label="페이드 시간">
            <select value={transitionSeconds} onChange={(event) => setTransitionSeconds(Number(event.target.value))}>
              <option value={0.5}>0.5초</option>
              <option value={1}>1초</option>
              <option value={1.5}>1.5초</option>
              <option value={2}>2초</option>
            </select>
          </Field>}
          <Field label="출력 해상도">
            <select value={resolution} onChange={(event) => setResolution(event.target.value as "1920x1080" | "1280x720")}>
              <option value="1920x1080">1920 × 1080</option>
              <option value="1280x720">1280 × 720</option>
            </select>
          </Field>
          <div className="album-video-summary">
            <span>선택 영상</span><strong>{orderedAssetIds.length}개</strong>
            <span>연결 방식</span><strong>{transition === "fade" ? `페이드 ${transitionSeconds}초` : "전환 없음"}</strong>
          </div>
          <Button
            icon={<Clapperboard size={18} />}
            loading={combine.isPending || Boolean(activeJob)}
            disabled={!orderedAssetIds.length}
            onClick={() => combine.mutate()}
          >
            전체 영상 만들기
          </Button>
        </aside>
      </div>
      {resultAssetId && (
        <section className="panel album-video-result">
          <div className="section-heading">
            <div><span className="eyebrow">FINAL VIDEO</span><h2>전체 영상이 완성되었습니다.</h2><p>{orderedAssetIds.length || trackVideos.length}개 트랙을 연결한 앨범 영상입니다.</p></div>
            <a className="button primary" href={assetUrl(resultAssetId)} download><Download size={18} /> MP4 다운로드</a>
          </div>
          <video className="video-player" controls src={assetUrl(resultAssetId)} />
          {albumVideo && typeof albumVideo.metadata?.duration_seconds === "number" && (
            <div className="album-video-meta">{formatDuration(albumVideo.metadata.duration_seconds as number)} · {albumVideo.size_bytes ? `${(albumVideo.size_bytes / 1024 / 1024).toFixed(1)} MB` : ""}</div>
          )}
        </section>
      )}
    </>
  );
}

function ExportPage() {
  const albumId = useAlbumId();
  const album = useAlbum(albumId);
  const tracks = useTracks(albumId);
  const [archive, setArchive] = useState<Asset | null>(null);
  const createArchive = useMutation({ mutationFn: () => api.createArchive(albumId), onSuccess: setArchive });
  const selectedTracks = (tracks.data || []).filter((track) => track.selected_generation_id);
  const cover = album.data?.assets?.find((asset) => asset.id === album.data?.selected_cover_asset_id);
  const videos = album.data?.assets?.filter((asset) => asset.type === "video") || [];
  const albumVideo = (album.data?.assets || [])
    .filter((asset) => asset.type === "album_video")
    .sort((left, right) => right.created_at.localeCompare(left.created_at))[0];
  const audioFor = (track: Track) => album.data?.assets?.find((asset) => asset.type === "audio" && asset.generation_id === track.selected_generation_id);
  const videoFor = (track: Track) => videos
    .filter((video) => video.track_id === track.id)
    .sort((left, right) => right.created_at.localeCompare(left.created_at))[0];

  if (album.isLoading || tracks.isLoading) return <LoadingPage />;
  if (!album.data) return <ErrorNotice error={album.error} />;

  return (
    <>
      <PageHeader eyebrow="STEP 05 · EXPORT" title="결과 및 내보내기" description="완성된 음악, 가사와 영상을 트랙별로 확인하고 다운로드하세요." actions={<Button loading={createArchive.isPending} icon={<Archive size={18} />} onClick={() => createArchive.mutate()}>앨범 ZIP 만들기</Button>} />
      <ErrorNotice error={createArchive.error} />
      <div className="export-overview">
        <section className="panel album-result">
          <div className="result-cover">{cover ? <img src={assetUrl(cover.id)} alt="" /> : <Disc3 size={64} />}</div>
          <div><span className="eyebrow">FINAL ALBUM</span><h2>{album.data.title}</h2><p>{album.data.artist_name || "Unknown Artist"}</p><div className="style-tags"><span>{album.data.genre}</span><span>{album.data.track_count} tracks</span></div></div>
        </section>
        <section className="panel completion-panel">
          <SectionTitle icon={<CircleCheck />} title="앨범 완성도" />
          <ProgressRow label="가사" value={(tracks.data || []).filter((t) => t.lyrics).length} total={tracks.data?.length || 0} />
          <ProgressRow label="최종 음원" value={selectedTracks.length} total={tracks.data?.length || 0} />
          <ProgressRow label="커버 이미지" value={cover ? 1 : 0} total={1} />
          <ProgressRow label="루프 영상" value={videos.length ? 1 : 0} total={1} />
          <ProgressRow label="전체 영상" value={albumVideo ? 1 : 0} total={1} />
        </section>
      </div>
      <section className="panel export-section">
        <div className="section-heading"><div><h2>트랙 결과</h2><p>최종 선택된 음원, 가사와 영상을 내려받을 수 있습니다.</p></div></div>
        <div className="export-tracks">
          {(tracks.data || []).map((track) => {
            const audio = audioFor(track);
            const video = videoFor(track);
            return <div key={track.id}><span className="track-number">{track.sequence}</span><strong>{track.title}</strong><span className="export-status">{audio ? "음원 준비됨" : "음원 미선택"} · {video ? "영상 준비됨" : "영상 미생성"}</span>{audio && <a className="button ghost" href={assetUrl(audio.id)} download><FileAudio size={16} /> MP3</a>}<a className="button ghost" href={lyricsUrl(track.id)}><FileText size={16} /> 가사</a>{video && <a className="button ghost" href={assetUrl(video.id)} download><Film size={16} /> MP4</a>}{!audio && <Link className="button secondary" to={`/albums/${albumId}/tracks`}>노래 만들기</Link>}</div>;
          })}
        </div>
      </section>
      <section className="panel package-panel export-package-panel">
        <SectionTitle icon={<Clapperboard />} title="전체 영상" />
        <p>트랙별 영상을 순서대로 연결한 최종 MP4입니다.</p>
        {albumVideo
          ? <a className="button primary download-wide" href={assetUrl(albumVideo.id)} download><Download size={18} /> {albumVideo.original_name}</a>
          : <Link className="button secondary download-wide" to={`/albums/${albumId}/album-video`}><Clapperboard size={18} /> 전체 영상 만들기</Link>}
      </section>
      <section className="panel package-panel export-package-panel"><SectionTitle icon={<Archive />} title="앨범 패키지" /><p>선택된 MP3, 전체 가사와 메타데이터를 ZIP으로 묶습니다.</p><div className="package-count"><strong>{selectedTracks.length}</strong><span>MP3</span><strong>{tracks.data?.length || 0}</strong><span>가사</span></div>{archive ? <a className="button primary download-wide" href={assetUrl(archive.id)} download><Download size={18} /> {archive.original_name}</a> : <Button loading={createArchive.isPending} icon={<Archive size={18} />} onClick={() => createArchive.mutate()}>ZIP 만들기</Button>}</section>
    </>
  );
}

function ProgressRow({ label, value, total }: { label: string; value: number; total: number }) {
  const percent = total ? Math.round((value / total) * 100) : 0;
  return <div className="progress-row"><div><span>{label}</span><b>{value} / {total}</b></div><div className="progress-track"><span style={{ width: `${percent}%` }} /></div></div>;
}

function LoadingPage() {
  return <div className="loading-page"><LoaderCircle className="spin" size={34} /><span>작업 공간을 불러오는 중입니다.</span></div>;
}

function NotFoundPage() {
  return <EmptyState icon={<Disc3 size={40} />} title="페이지를 찾을 수 없습니다." description="요청한 페이지가 없거나 이동되었습니다." action={<Link className="button primary" to="/albums">앨범 목록으로</Link>} />;
}

function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/albums" replace />} />
      <Route path="/albums" element={<AppShell><AlbumListPage /></AppShell>} />
      <Route path="/albums/new" element={<AppShell><AlbumCreatePage /></AppShell>} />
      <Route path="/albums/:albumId/plan" element={<AppShell><AlbumPlanPage /></AppShell>} />
      <Route path="/albums/:albumId/tracks" element={<AppShell><TrackGenerationPage /></AppShell>} />
      <Route path="/albums/:albumId/video" element={<AppShell><VideoCreationPage /></AppShell>} />
      <Route path="/albums/:albumId/album-video" element={<AppShell><AlbumVideoPage /></AppShell>} />
      <Route path="/albums/:albumId/export" element={<AppShell><ExportPage /></AppShell>} />
      <Route path="*" element={<AppShell><NotFoundPage /></AppShell>} />
    </Routes>
  );
}

export default App;
