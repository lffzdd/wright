import { ArrowClockwise, ArrowLeft, CaretDoubleUp, CaretRight, ChatCircleDots, CopySimple, Crosshair, File, FileCode, FileImage, FileLock, FileText, Folder, FolderOpen, GitDiff, MagnifyingGlass, TextAlignLeft, X } from "@phosphor-icons/react";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useT } from "../../i18n";
import { CodeView } from "../code/CodeView";
import { languageForPath, languageLabel } from "../code/highlight";

export type TreeEntry = { name: string; path: string; kind: string; size?: number | null };
export type AccessedFile = { path: string; access?: string; call_id?: string; tool?: string; tools?: Record<string, number>; last_calls?: Record<string, string> };
type FileMeta = { size?: number; additions?: number; deletions?: number };
type Branch = { status: "loading" } | { status: "error"; error: string } | { status: "ready"; entries: TreeEntry[] };
type AccessFilter = "all" | "write" | "read";
type RowExtra = {
  label?: string; title?: string; displayName?: string; kind?: string; detail?: string; size?: string; disabled?: boolean; ariaLabel?: string; className?: string;
  count?: number; highlight?: string; isDir?: boolean; used?: boolean; onActivate?: () => void;
};

const splitPath = (path: string) => {
  const index = path.replace(/\/+$/, "").lastIndexOf("/");
  return index < 0 ? { dir: "", name: path } : { dir: path.slice(0, index + 1), name: path.slice(index + 1) };
};
const sortEntries = (entries: TreeEntry[]) => [...entries].sort((a, b) => (a.kind === "dir" ? 0 : 1) - (b.kind === "dir" ? 0 : 1) || a.name.localeCompare(b.name));
const formatSize = (size: number) => size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
/** Width of a folder caret plus its gap, so file rows line up with folder rows. */
const CARET_SLOT = 16;
/** Lines past this are cut from the preview to keep the panel responsive. */
const PREVIEW_LINES = 4000;
/** Session rows shown before "Show all", so the project tree stays in view. */
const SESSION_VISIBLE_ROWS = 8;

/** Cache and tool folders that are hidden unless the user asks to see them. */
const CACHE_NAMES = new Set([".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".parcel-cache", ".turbo", ".next", ".specstory", ".DS_Store"]);
const isCachePath = (path: string) => path.split("/").some((part) => CACHE_NAMES.has(part));
/** Directories from the root down to `path`; `includeSelf` adds the path itself. */
const chainOf = (path: string, includeSelf: boolean) => {
  const parts = path.split("/").filter(Boolean);
  const count = includeSelf ? parts.length : parts.length - 1;
  return Array.from({ length: Math.max(0, count) }, (_, index) => parts.slice(0, index + 1).join("/"));
};

const extOf = (name: string) => name.includes(".") && !name.startsWith(".") ? name.split(".").pop()!.toLowerCase() : "";
const EXT_GROUPS: Record<string, string> = {
  ts: "code", tsx: "code", js: "code", jsx: "code", py: "code", go: "code", rs: "code", java: "code", kt: "code", swift: "code",
  c: "code", cc: "code", cpp: "code", h: "code", rb: "code", sh: "code",
  css: "style", scss: "style", less: "style", html: "style",
  md: "doc", mdx: "doc", txt: "doc", rst: "doc", pdf: "doc",
  json: "config", yaml: "config", yml: "config", toml: "config", ini: "config", lock: "config", env: "config", cfg: "config", xml: "config",
  png: "image", jpg: "image", jpeg: "image", gif: "image", svg: "image", webp: "image", ico: "image",
};
/** One fixed-width icon per file, tinted by kind, so names line up with folder rows. */
const FileIcon = ({ name }: { name: string }) => {
  const ext = extOf(name);
  const group = EXT_GROUPS[ext] ?? "other";
  const Icon = ext === "lock" ? FileLock : group === "doc" ? FileText : group === "image" ? FileImage : group === "other" ? File : FileCode;
  return <Icon size={14} className={`files-icon ext-${group}`} aria-hidden="true" />;
};
const FolderIcon = ({ open }: { open?: boolean }) => open ? <FolderOpen size={14} className="files-icon dir" aria-hidden="true" /> : <Folder size={14} className="files-icon dir" aria-hidden="true" />;
/**
 * Session tag for an accessed path, from the strongest tool used on it:
 * any edit or write is MOD, then READ, GREP, LIST. Older payloads without
 * per-tool counts fall back to the last tool and the access kind.
 */
const ACCESS_TAGS: Array<{ label: string; kind: string; tools: string[] }> = [
  { label: "MOD", kind: "modified", tools: ["edit_file", "write_file"] },
  { label: "READ", kind: "read", tools: ["read_file"] },
  { label: "GREP", kind: "search", tools: ["grep"] },
  { label: "LIST", kind: "list", tools: ["list_directory", "glob"] },
];
const accessTag = (file: AccessedFile) => {
  const used = file.tools && Object.keys(file.tools).length ? Object.keys(file.tools) : file.tool ? [file.tool] : [];
  if (file.access === "write") return ACCESS_TAGS[0];
  const byTool = ACCESS_TAGS.find((tag) => tag.tools.some((tool) => used.includes(tool)));
  if (byTool) return byTool;
  if (file.access === "read") return ACCESS_TAGS[1];
  if (file.access === "list") return ACCESS_TAGS[3];
  return { label: (file.access || "use").slice(0, 4).toUpperCase(), kind: "touched", tools: [] };
};
/** The call behind a row's tag: the latest call of the tag's tools (MOD → the latest edit, not a later read). */
const tagCallId = (file: AccessedFile) => {
  const tag = accessTag(file);
  const latest = Object.entries(file.last_calls ?? {}).filter(([tool]) => tag.tools.includes(tool)).pop();
  return latest?.[1] ?? file.call_id;
};
const toolSummary = (file: AccessedFile) => Object.entries(file.tools ?? {}).map(([tool, count]) => `${tool} ×${count}`).join(" · ");
const statusLetter = (status: string) => status.trim().replace("??", "?").charAt(0).toUpperCase() || "?";
const statusClass = (status: string) => {
  const letter = statusLetter(status);
  return letter === "A" || letter === "?" || letter === "U" ? "add" : letter === "D" ? "del" : "mod";
};
const highlightName = (name: string, needle: string) => {
  const query = needle.trim().toLowerCase();
  const index = query ? name.toLowerCase().indexOf(query) : -1;
  if (index < 0) return name;
  return <>{name.slice(0, index)}<mark className="files-hit">{name.slice(index, index + query.length)}</mark>{name.slice(index + query.length)}</>;
};

// Expanded folders per session, kept while the panel is open. Module scope so a
// switch away from a session and back restores the same tree.
const expandedBySession = new Map<string, string[]>();
const readFlag = (key: string) => { try { return localStorage.getItem(key) === "1"; } catch { return false; } };
const writeFlag = (key: string, value: boolean) => { try { localStorage.setItem(key, value ? "1" : "0"); } catch { /* storage unavailable */ } };

export function FilesPanel({ sessionKey, canReadFiles, historyOnly, accessedFiles, accessCounts, fileMeta, readTree, readFile, searchFiles, filePath, focusLine, openFile, changes, openChanges, projectName, showCall }: {
  sessionKey: string;
  canReadFiles: boolean;
  historyOnly: boolean;
  accessedFiles: AccessedFile[];
  /** How many times each (deduplicated) path was touched this session. */
  accessCounts: Record<string, number>;
  fileMeta: (path: string) => FileMeta;
  readTree: (path: string) => Promise<{ entries: TreeEntry[] }>;
  readFile: (path: string) => Promise<{ content: string; binary?: boolean }>;
  searchFiles: (query: string) => Promise<Array<{ path: string }>>;
  filePath: string;
  /** A line to scroll to and mark in the open file. */
  focusLine?: number;
  openFile: (path: string) => void;
  /** Working-tree changes with their git status (M, A, D, ?? …). */
  changes: Array<{ path: string; status: string }>;
  openChanges: (path: string) => void;
  projectName: string;
  /** Scrolls the conversation to a tool call. */
  showCall?: (callId: string) => void;
}) {
  const tr = useT();
  const [branches, setBranches] = useState<Record<string, Branch>>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<{ query: string; paths: string[] } | { query: string; error: string } | null>(null);
  const [preview, setPreview] = useState<{ path: string; text?: string; binary?: boolean; error?: string } | null>(null);
  const previewRef = useRef<HTMLElement>(null);
  const treeRef = useRef<HTMLDivElement>(null);
  const [wrap, setWrapState] = useState(() => { try { return localStorage.getItem("wright.files.wrap") !== "0"; } catch { return true; } });
  const setWrap = (next: boolean) => { setWrapState(next); try { localStorage.setItem("wright.files.wrap", next ? "1" : "0"); } catch { /* storage unavailable */ } };
  const [showCache, setShowCacheState] = useState(() => readFlag("wright.files.showCache"));
  const setShowCache = (next: boolean) => { setShowCacheState(next); writeFlag("wright.files.showCache", next); };
  const [accessFilter, setAccessFilter] = useState<AccessFilter>("all");
  const [revealPath, setRevealPath] = useState<string | null>(null);
  // Long session lists start collapsed so the project tree stays visible; the user's choice is remembered.
  const [sessionOpenPref, setSessionOpenPref] = useState<boolean | null>(() => {
    try { const value = localStorage.getItem("wright.files.sessionOpen"); return value === null ? null : value === "1"; } catch { return null; }
  });
  const sessionOpen = sessionOpenPref ?? false;
  const [sessionShowAll, setSessionShowAll] = useState(false);
  const setSessionOpen = (open: boolean) => {
    setSessionOpenPref(open);
    writeFlag("wright.files.sessionOpen", open);
  };
  const generation = useRef(0);

  const changeStatus = useMemo(() => new Map(changes.map((item) => [item.path, item.status])), [changes]);
  // Folders seen in any loaded listing, so accessed entries that are folders show as DIR.
  const dirPaths = useMemo(() => {
    const dirs = new Set<string>();
    for (const branch of Object.values(branches)) {
      if (branch.status === "ready") for (const entry of branch.entries) if (entry.kind === "dir") dirs.add(entry.path);
    }
    return dirs;
  }, [branches]);

  const updateExpanded = useCallback((next: Set<string>) => {
    setExpanded(next);
    expandedBySession.set(sessionKey, [...next]);
  }, [sessionKey]);

  const load = useCallback((path: string) => {
    const owner = generation.current;
    setBranches((current) => ({ ...current, [path]: { status: "loading" } }));
    readTree(path).then((result) => {
      const entries = sortEntries(Array.isArray(result?.entries) ? result.entries : []);
      if (generation.current === owner) setBranches((current) => ({ ...current, [path]: { status: "ready", entries } }));
    }).catch((error) => {
      if (generation.current === owner) setBranches((current) => ({ ...current, [path]: { status: "error", error: error instanceof Error ? error.message : String(error) } }));
    });
  }, [readTree]);

  // Load each accessed file's parent folder (not expanded) so folder entries can be told apart from files.
  useEffect(() => {
    if (!canReadFiles) return;
    const parents = new Set<string>();
    for (const file of accessedFiles) {
      if (file.access === "list") continue;
      const index = file.path.lastIndexOf("/");
      const parent = index < 0 ? "" : file.path.slice(0, index);
      if (!branches[parent]) parents.add(parent);
    }
    for (const parent of [...parents].slice(0, 40)) load(parent);
  }, [accessedFiles, branches, canReadFiles, load]);

  useEffect(() => {
    ++generation.current;
    const saved = expandedBySession.get(sessionKey) ?? [];
    setBranches({}); setExpanded(new Set(saved)); setQuery(""); setResults(null); setRevealPath(null);
    if (canReadFiles) { load(""); for (const path of saved) load(path); }
  }, [sessionKey, canReadFiles, load]);

  useEffect(() => {
    const needle = query.trim();
    if (!needle || !canReadFiles) { setResults(null); return; }
    let current = true;
    const timer = window.setTimeout(() => {
      searchFiles(needle).then((found) => { if (current) setResults({ query: needle, paths: found.map((item) => item.path) }); })
        .catch((error) => { if (current) setResults({ query: needle, error: error instanceof Error ? error.message : String(error) }); });
    }, 180);
    return () => { current = false; window.clearTimeout(timer); };
  }, [query, canReadFiles, searchFiles]);

  useEffect(() => {
    let current = true;
    setPreview(filePath ? { path: filePath } : null);
    if (filePath && canReadFiles) {
      readFile(filePath).then((file) => { if (current) setPreview({ path: filePath, text: file.binary ? "" : file.content, binary: !!file.binary }); })
        .catch((error) => { if (current) setPreview({ path: filePath, error: error instanceof Error ? error.message : String(error) }); });
    }
    return () => { current = false; };
  }, [filePath, canReadFiles, readFile]);
  useEffect(() => { if (filePath) previewRef.current?.focus(); }, [filePath]);

  // After "show in tree", scroll to and focus the row once its folders have loaded.
  useEffect(() => {
    if (!revealPath || filePath || !treeRef.current) return;
    const row = [...treeRef.current.querySelectorAll<HTMLElement>("[data-path]")].find((item) => item.dataset.path === revealPath);
    if (row) {
      row.scrollIntoView?.({ block: "center" });
      row.focus({ preventScroll: true });
      setRevealPath(null);
      return;
    }
    const pending = chainOf(revealPath, false).some((path) => !branches[path] || branches[path].status === "loading");
    if (!pending) setRevealPath(null);
  }, [revealPath, branches, expanded, query, filePath]);

  const toggle = (path: string) => {
    const next = new Set(expanded);
    if (next.has(path)) next.delete(path);
    else {
      next.add(path);
      if (!branches[path] || branches[path].status === "error") load(path);
    }
    updateExpanded(next);
  };
  const collapseAll = () => updateExpanded(new Set());
  const refresh = () => {
    ++generation.current;
    setBranches({});
    load("");
    for (const path of expanded) load(path);
  };

  // Opens the folders above `path` in the tree, clears the search, and returns to the tree view.
  const reveal = (path: string, isDir: boolean) => {
    const chain = chainOf(path, isDir);
    const next = new Set(expanded);
    for (const folder of chain) {
      next.add(folder);
      if (!branches[folder] || branches[folder].status === "error") load(folder);
    }
    updateExpanded(next);
    setQuery(""); setResults(null);
    setRevealPath(path);
    openFile("");
  };
  const copyPath = (path: string) => {
    try { void navigator.clipboard?.writeText(path).catch(() => undefined); } catch { /* clipboard unavailable */ }
  };

  // Arrow keys move through the tree; Enter and Space already click the focused row.
  const onTreeKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (target.getAttribute("role") !== "treeitem") return;
    const items = [...event.currentTarget.querySelectorAll<HTMLElement>('[role="treeitem"]:not(:disabled)')];
    const index = items.indexOf(target);
    const level = Number(target.getAttribute("aria-level") ?? 1);
    const isDir = target.dataset.dir === "true";
    const open = target.getAttribute("aria-expanded") === "true";
    switch (event.key) {
      case "ArrowDown": items[index + 1]?.focus(); break;
      case "ArrowUp": items[index - 1]?.focus(); break;
      case "Home": items[0]?.focus(); break;
      case "End": items[items.length - 1]?.focus(); break;
      case "ArrowRight":
        if (isDir && !open) target.click();
        else if (isDir) items[index + 1]?.focus();
        break;
      case "ArrowLeft":
        if (isDir && open) target.click();
        else {
          for (let i = index - 1; i >= 0; i--) {
            if (Number(items[i].getAttribute("aria-level")) === level - 1) { items[i].focus(); break; }
          }
        }
        break;
      default: return;
    }
    event.preventDefault();
  };

  if (filePath) {
    const { dir, name } = splitPath(filePath);
    const lines = preview?.text?.replace(/\n$/, "").split("\n") ?? [];
    const shown = lines.length > PREVIEW_LINES ? lines.slice(0, PREVIEW_LINES) : lines;
    const lang = languageForPath(filePath);
    return <section ref={previewRef} tabIndex={-1} className="inspector-file-preview files-preview" aria-label={tr("web.content_preview")}>
      <div className="files-preview-head">
        <button type="button" className="icon-button" onClick={() => openFile("")} aria-label={tr("web.back_to_files")} title={tr("web.back_to_files")}><ArrowLeft size={14} /></button>
        <span className="files-preview-title" title={filePath}><strong className="mono">{name}</strong>{dir && <small className="mono">{dir}</small>}</span>
        {changeStatus.has(filePath) && <button type="button" className="text-button files-diff-link" onClick={() => openChanges(filePath)}><GitDiff size={13} />{tr("web.files.view_diff")}</button>}
        <button type="button" className="icon-button" onClick={() => copyPath(filePath)} aria-label={tr("web.files.copy_path")} title={tr("web.files.copy_path")}><CopySimple size={14} /></button>
        <button type="button" className="icon-button" onClick={() => reveal(filePath, false)} aria-label={tr("web.files.locate")} title={tr("web.files.locate")}><Crosshair size={14} /></button>
        <button type="button" className="icon-button" onClick={() => openFile("")} aria-label={`${tr("web.close")} ${filePath}`}><X size={14} /></button>
      </div>
      {preview?.text && !preview.binary && <div className="files-preview-meta">
        {lang && <span>{languageLabel(lang)}</span>}
        <span>{tr("web.files.line_count", { count: lines.length })}</span>
        <button type="button" className={`files-wrap-toggle${wrap ? " on" : ""}`} aria-pressed={wrap} onClick={() => setWrap(!wrap)} title={tr("web.files.wrap")}><TextAlignLeft size={13} />{tr("web.files.wrap")}</button>
      </div>}
      {preview?.error && <p className="form-error" role="alert">{preview.error}</p>}
      {!preview?.error && preview?.text === undefined && <p className="hint" role="status">{tr("web.inspector_loading")}</p>}
      {preview?.binary && <p className="hint">{tr("web.binary_file")}</p>}
      {preview?.text !== undefined && !preview.binary && (preview.text
        ? <CodeView variant="panel" wrap={wrap} lang={lang} label={filePath} focusLine={focusLine}
          lines={shown.map((text, index) => ({ num: index + 1, text }))} />
        : <p className="empty-small">{tr("web.empty_file")}</p>)}
      {lines.length > PREVIEW_LINES && <p className="hint">{tr("web.files.truncated", { shown: PREVIEW_LINES, total: lines.length })}</p>}
    </section>;
  }

  const fileRow = (path: string, depth?: number, extra?: RowExtra) => {
    const { dir, name } = splitPath(path);
    // Session rows carry their own access tag, so git status letters stay in the tree.
    const status = extra?.label ? undefined : changeStatus.get(path);
    return <button type="button" key={path} data-path={path} data-dir={extra?.isDir ? "true" : undefined}
      className={`files-row${extra?.className ? ` ${extra.className}` : ""}${status ? ` changed ${statusClass(status)}` : ""}`}
      disabled={extra?.disabled || !canReadFiles}
      style={depth !== undefined ? { paddingLeft: 8 + depth * 14 + CARET_SLOT } : undefined}
      role={depth !== undefined ? "treeitem" : undefined} aria-level={depth !== undefined ? depth + 1 : undefined}
      aria-label={extra?.ariaLabel} title={extra?.title ?? path}
      onClick={() => (extra?.onActivate ? extra.onActivate() : openFile(path))}>
      {extra?.label ? <span className={`access-tag ${extra.kind ?? ""}`}>{extra.label}</span> : extra?.isDir ? <FolderIcon /> : <FileIcon name={name} />}
      <span className="files-name"><span>{extra?.displayName ?? (extra?.highlight ? highlightName(name, extra.highlight) : name)}{extra?.label && extra.isDir ? "/" : ""}</span>{depth === undefined && dir && <small>{dir}</small>}</span>
      {extra?.count ? <span className="files-count" title={tr("web.files.access_count", { count: extra.count })}>×{extra.count}</span> : null}
      {!status && extra?.used && <span className="files-used-dot" title={tr("web.files.used")} aria-hidden="true" />}
      {extra?.detail ? <span className="files-detail">{extra.detail}</span> : null}
      {extra?.size ? <span className="files-detail files-size">{extra.size}</span> : null}
      {status && <span className={`files-status ${statusClass(status)}`} title={tr("web.files.changed")}>{statusLetter(status)}</span>}
    </button>;
  };

  const treeFileRow = (entry: TreeEntry, depth: number) => {
    const status = changeStatus.get(entry.path);
    const metadata = fileMeta(entry.path);
    const detail = status && metadata.additions !== undefined ? `+${metadata.additions ?? 0} −${metadata.deletions ?? 0}` : undefined;
    const size = !detail && typeof entry.size === "number" ? formatSize(entry.size) : undefined;
    return fileRow(entry.path, depth, { detail, size, className: "files-tree-file", used: usedPaths.has(entry.path) });
  };

  const tree = (path: string, depth: number): ReactNode => {
    const branch = branches[path];
    if (!branch || branch.status === "loading") return <p className="hint files-tree-note" role="status" style={{ paddingLeft: 8 + depth * 14 }}>{tr("web.inspector_loading")}</p>;
    if (branch.status === "error") return <p className="form-error files-tree-note" role="alert" style={{ marginLeft: 8 + depth * 14 }}>{branch.error}</p>;
    const entries = branch.entries.filter((entry) => showCache || !CACHE_NAMES.has(entry.name));
    if (!entries.length) return <p className="empty-small files-tree-note" style={{ paddingLeft: 8 + depth * 14 }}>{tr("web.dir_empty")}</p>;
    return entries.map((entry) => {
      if (entry.kind !== "dir") return treeFileRow(entry, depth);
      const open = expanded.has(entry.path);
      return <div key={entry.path} role="none">
        <button type="button" className="files-row files-dir" role="treeitem" data-path={entry.path} data-dir="true" aria-level={depth + 1} aria-expanded={open} style={{ paddingLeft: 8 + depth * 14 }} onClick={() => toggle(entry.path)} title={entry.path}>
          <CaretRight size={10} weight="bold" className={`files-caret${open ? " open" : ""}`} />
          <FolderIcon open={open} />
          <span className="files-name"><span>{entry.name}</span></span>
          {/* A collapsed folder hints at what is inside: changes first, then files used this session. */}
          {!open && changedDirs.has(entry.path) ? <span className="files-dir-mark changed" title={tr("web.files.dir_changed")} />
            : !open && usedDirs.has(entry.path) ? <span className="files-dir-mark used" title={tr("web.files.dir_used")} /> : null}
        </button>
        {open && <div role="group">{tree(entry.path, depth + 1)}</div>}
      </div>;
    });
  };

  const usedPaths = new Set(accessedFiles.map((file) => file.path));
  const ancestors = (paths: Iterable<string>) => { const dirs = new Set<string>(); for (const path of paths) for (const dir of chainOf(path, false)) dirs.add(dir); return dirs; };
  const changedDirs = ancestors(changeStatus.keys());
  const usedDirs = ancestors(usedPaths);
  const visibleAccessed = accessedFiles.filter((file) => showCache || !isCachePath(file.path));
  // "Modified" and "Read" follow the tags; GREP and LIST rows only show under "All".
  const filterKind: Record<Exclude<AccessFilter, "all">, string> = { write: "modified", read: "read" };
  const writeCount = visibleAccessed.filter((file) => accessTag(file).kind === "modified").length;
  const readCount = visibleAccessed.filter((file) => accessTag(file).kind === "read").length;
  const sessionRows = [...visibleAccessed].reverse().filter((file) => accessFilter === "all" || accessTag(file).kind === filterKind[accessFilter]);
  const filterChip = (value: AccessFilter, label: string, count: number) => <button type="button" className="files-chip" aria-pressed={accessFilter === value} onClick={() => setAccessFilter(value)}>
    {label}<small>{count}</small>
  </button>;
  const searchPaths = results && "paths" in results ? results.paths.filter((path) => showCache || !isCachePath(path)) : [];

  return <div className="files-panel">
    <label className="files-filter">
      <MagnifyingGlass size={13} aria-hidden="true" />
      <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={tr("web.files.filter")} aria-label={tr("web.files.filter")} disabled={!canReadFiles} />
    </label>
    {historyOnly && <p className="hint">{tr(canReadFiles ? "web.inspector_history_files_current" : "web.inspector_history_files")}</p>}
    {query.trim() ? <section className="files-section" aria-label={tr("web.files.results")}>
      {!results || results.query !== query.trim() ? <p className="hint" role="status">{tr("web.inspector_loading")}</p>
        : "error" in results ? <p className="form-error" role="alert">{results.error}</p>
        : searchPaths.length ? <div className="files-list">{searchPaths.map((path) => <div key={path} className="files-result">
          {fileRow(path, undefined, { highlight: query })}
          <button type="button" className="icon-button files-locate" onClick={() => reveal(path, false)} aria-label={tr("web.files.locate")} title={`${tr("web.files.locate")}: ${path}`}><Crosshair size={13} /></button>
        </div>)}</div>
        : <p className="empty-small">{tr("web.files.no_match")}</p>}
    </section> : <>
      {accessedFiles.length > 0 && <details className="files-section files-session" open={sessionOpen} onToggle={(event) => { if (event.currentTarget.open !== sessionOpen) setSessionOpen(event.currentTarget.open); }}>
        <summary className="section-label"><span>{tr("web.files.session")}</span><CaretRight size={10} weight="bold" className="files-caret" aria-hidden="true" /><small>{accessedFiles.length}</small></summary>
        <div className="files-chips" role="group" aria-label={tr("web.files.session")}>
          {filterChip("all", tr("web.files.filter_all"), visibleAccessed.length)}
          {filterChip("write", tr("web.files.filter_changed"), writeCount)}
          {filterChip("read", tr("web.files.filter_read"), readCount)}
        </div>
        <div className="files-list accessed-file-list">{(sessionShowAll ? sessionRows : sessionRows.slice(0, SESSION_VISIBLE_ROWS)).map((file) => {
          // Folders (listed, or known from a loaded tree) open in the tree instead of the preview.
          const isDir = file.access === "list" || dirPaths.has(file.path);
          const metadata = fileMeta(file.path);
          const { label, kind } = accessTag(file);
          const tools = toolSummary(file);
          // The project root reads better as its name than as ".".
          const isRoot = /^\.?\/?$/.test(file.path);
          const detail = file.access === "write" && (metadata.additions !== undefined || metadata.deletions !== undefined)
            ? `+${metadata.additions ?? 0} −${metadata.deletions ?? 0}` : "";
          const size = !detail && typeof metadata.size === "number" && !isDir ? formatSize(metadata.size) : "";
          const row = fileRow(file.path, undefined, {
            label, kind, detail, size, displayName: isRoot ? projectName || undefined : undefined,
            title: tools ? `${file.path}\n${tools}` : file.path,
            disabled: file.access === "list", ariaLabel: `${label} ${file.path}`, className: `accessed-file ${kind}`,
            count: accessCounts[file.path] > 1 ? accessCounts[file.path] : undefined,
            isDir, onActivate: isDir ? () => reveal(file.path, true) : undefined,
          });
          const callId = showCall ? tagCallId(file) : undefined;
          // Folders already open in the tree on click; files get a separate "show in tree".
          if (isDir && !callId) return row;
          return <div key={file.path} className="files-result">
            {row}
            {callId && <button type="button" className="icon-button files-locate" onClick={() => showCall?.(callId)} aria-label={`${tr("web.files.show_call")}: ${file.path}`} title={tr("web.files.show_call")}><ChatCircleDots size={13} /></button>}
            {!isDir && <button type="button" className="icon-button files-locate" onClick={() => reveal(file.path, false)} disabled={!canReadFiles} aria-label={`${tr("web.files.locate")}: ${file.path}`} title={tr("web.files.locate")}><Crosshair size={13} /></button>}
          </div>;
        })}</div>
        {sessionRows.length > SESSION_VISIBLE_ROWS && <button type="button" className="text-button files-more" onClick={() => setSessionShowAll(!sessionShowAll)} aria-expanded={sessionShowAll}>
          {sessionShowAll ? tr("web.files.show_fewer") : tr("web.files.show_all", { count: sessionRows.length })}
        </button>}
      </details>}
      <section className="files-section" aria-label={tr("web.files.project")}>
        <div className="section-label files-project-head"><span title={projectName}>{projectName || tr("web.files.project")}</span>
          <span className="files-head-actions">
            <button type="button" className={`files-cache-toggle${showCache ? " on" : ""}`} onClick={() => setShowCache(!showCache)} aria-pressed={showCache} aria-label={tr(showCache ? "web.files.hide_cache" : "web.files.show_cache")} title={tr(showCache ? "web.files.hide_cache" : "web.files.show_cache")}>{tr(showCache ? "web.files.hide_cache_short" : "web.files.show_cache_short")}</button>
            <button type="button" className="icon-button" onClick={collapseAll} disabled={!canReadFiles || expanded.size === 0} aria-label={tr("web.files.collapse_all")} title={tr("web.files.collapse_all")}><CaretDoubleUp size={13} /></button>
            <button type="button" className="icon-button" onClick={refresh} disabled={!canReadFiles} aria-label={tr("web.files.refresh")} title={tr("web.files.refresh")}><ArrowClockwise size={13} /></button>
          </span>
        </div>
        {canReadFiles ? <div className="files-tree" role="tree" ref={treeRef} onKeyDown={onTreeKey} aria-label={tr("web.files.project")}>{tree("", 0)}</div> : null}
      </section>
    </>}
  </div>;
}
