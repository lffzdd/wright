import { ArrowClockwise, ArrowLeft, CaretDoubleUp, CaretRight, CopySimple, Crosshair, Eye, EyeSlash, File, Folder, FolderOpen, GitDiff, MagnifyingGlass, TextAlignLeft, X } from "@phosphor-icons/react";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useT } from "../../i18n";
import { CodeView } from "../code/CodeView";
import { languageForPath, languageLabel } from "../code/highlight";

export type TreeEntry = { name: string; path: string; kind: string; size?: number | null };
export type AccessedFile = { path: string; access?: string; call_id?: string; tool?: string };
type FileMeta = { size?: number; additions?: number; deletions?: number };
type Branch = { status: "loading" } | { status: "error"; error: string } | { status: "ready"; entries: TreeEntry[] };
type AccessFilter = "all" | "write" | "read";
type RowExtra = {
  label?: string; kind?: string; detail?: string; disabled?: boolean; ariaLabel?: string; className?: string;
  count?: number; highlight?: string; isDir?: boolean; onActivate?: () => void;
};

const splitPath = (path: string) => {
  const index = path.replace(/\/+$/, "").lastIndexOf("/");
  return index < 0 ? { dir: "", name: path } : { dir: path.slice(0, index + 1), name: path.slice(index + 1) };
};
const sortEntries = (entries: TreeEntry[]) => [...entries].sort((a, b) => (a.kind === "dir" ? 0 : 1) - (b.kind === "dir" ? 0 : 1) || a.name.localeCompare(b.name));
const formatSize = (size: number) => size < 1024 ? `${size} B` : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
/** Lines past this are cut from the preview to keep the panel responsive. */
const PREVIEW_LINES = 4000;

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
const ExtBadge = ({ name }: { name: string }) => {
  const ext = extOf(name);
  return <span className={`files-ext ext-${EXT_GROUPS[ext] ?? "other"}`} aria-hidden="true">{ext.slice(0, 4) || "·"}</span>;
};
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

export function FilesPanel({ sessionKey, canReadFiles, historyOnly, accessedFiles, accessCounts, fileMeta, readTree, readFile, searchFiles, filePath, focusLine, openFile, changes, openChanges, projectName }: {
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
    const status = changeStatus.get(path);
    return <button type="button" key={path} data-path={path} data-dir={extra?.isDir ? "true" : undefined}
      className={`files-row${extra?.className ? ` ${extra.className}` : ""}${status ? " changed" : ""}`}
      disabled={extra?.disabled || !canReadFiles}
      style={depth !== undefined ? { paddingLeft: 8 + depth * 14 + 16 } : undefined}
      role={depth !== undefined ? "treeitem" : undefined} aria-level={depth !== undefined ? depth + 1 : undefined}
      aria-label={extra?.ariaLabel} title={path}
      onClick={() => (extra?.onActivate ? extra.onActivate() : openFile(path))}>
      {extra?.label ? <span className={`accessed-kind ${extra.kind ?? ""}`}>{extra.label}</span> : <ExtBadge name={name} />}
      <span className="files-name"><span>{extra?.highlight ? highlightName(name, extra.highlight) : name}</span>{depth === undefined && dir && <small>{dir}</small>}</span>
      {extra?.count ? <span className="files-count" title={tr("web.files.access_count", { count: extra.count })}>×{extra.count}</span> : null}
      {status && !extra?.label && <span className={`files-status ${statusClass(status)}`} title={tr("web.files.changed")}>{statusLetter(status)}</span>}
      {extra?.detail ? <span className={`files-detail${extra.kind === "modified" ? " modified" : ""}`}>{extra.detail}</span> : null}
    </button>;
  };

  const treeFileRow = (entry: TreeEntry, depth: number) => {
    const status = changeStatus.get(entry.path);
    const metadata = fileMeta(entry.path);
    const detail = status && metadata.additions !== undefined
      ? `+${metadata.additions ?? 0} −${metadata.deletions ?? 0}`
      : typeof entry.size === "number" ? formatSize(entry.size) : undefined;
    return fileRow(entry.path, depth, { detail, className: "files-tree-file" });
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
          {open ? <FolderOpen size={14} className="files-icon dir" /> : <Folder size={14} className="files-icon dir" />}
          <span className="files-name"><span>{entry.name}</span></span>
        </button>
        {open && <div role="group">{tree(entry.path, depth + 1)}</div>}
      </div>;
    });
  };

  const visibleAccessed = accessedFiles.filter((file) => showCache || !isCachePath(file.path));
  const writeCount = visibleAccessed.filter((file) => file.access === "write").length;
  const readCount = visibleAccessed.filter((file) => file.access === "read").length;
  const sessionRows = [...visibleAccessed].reverse().filter((file) => accessFilter === "all" || file.access === accessFilter);
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
      {accessedFiles.length > 0 && <details className="files-section files-session" open>
        <summary className="section-label"><span>{tr("web.files.session")}</span><small>{accessedFiles.length}</small></summary>
        <div className="files-chips" role="group" aria-label={tr("web.files.session")}>
          {filterChip("all", tr("web.files.filter_all"), visibleAccessed.length)}
          {filterChip("write", tr("web.files.filter_changed"), writeCount)}
          {filterChip("read", tr("web.files.filter_read"), readCount)}
        </div>
        <div className="files-list accessed-file-list">{sessionRows.map((file) => {
          const isDir = file.access === "list" || dirPaths.has(file.path);
          const metadata = fileMeta(file.path);
          const label = file.access === "list" ? "LIST" : isDir ? "DIR" : file.access === "write" ? "MOD" : file.access === "read" ? "READ" : (file.access || "TOUCH").toUpperCase();
          const kind = file.access === "write" ? "modified" : file.access === "read" || isDir ? "read" : "touched";
          const detail = file.access === "write" && (metadata.additions !== undefined || metadata.deletions !== undefined)
            ? `+${metadata.additions ?? 0} −${metadata.deletions ?? 0}`
            : typeof metadata.size === "number" && !isDir ? formatSize(metadata.size) : "";
          return fileRow(file.path, undefined, {
            label, kind, detail, disabled: file.access === "list", ariaLabel: `${label} ${file.path}`, className: "accessed-file",
            count: accessCounts[file.path] > 1 ? accessCounts[file.path] : undefined,
            isDir, onActivate: isDir ? () => reveal(file.path, true) : undefined,
          });
        })}</div>
      </details>}
      <section className="files-section" aria-label={tr("web.files.project")}>
        <div className="section-label files-project-head"><span title={projectName}>{projectName || tr("web.files.project")}</span>
          <span className="files-head-actions">
            <button type="button" className={`icon-button${showCache ? " on" : ""}`} onClick={() => setShowCache(!showCache)} aria-pressed={showCache} aria-label={tr(showCache ? "web.files.hide_cache" : "web.files.show_cache")} title={tr(showCache ? "web.files.hide_cache" : "web.files.show_cache")}>{showCache ? <Eye size={13} /> : <EyeSlash size={13} />}</button>
            <button type="button" className="icon-button" onClick={collapseAll} disabled={!canReadFiles || expanded.size === 0} aria-label={tr("web.files.collapse_all")} title={tr("web.files.collapse_all")}><CaretDoubleUp size={13} /></button>
            <button type="button" className="icon-button" onClick={refresh} disabled={!canReadFiles} aria-label={tr("web.files.refresh")} title={tr("web.files.refresh")}><ArrowClockwise size={13} /></button>
          </span>
        </div>
        {canReadFiles ? <div className="files-tree" role="tree" ref={treeRef} onKeyDown={onTreeKey} aria-label={tr("web.files.project")}>{tree("", 0)}</div> : null}
      </section>
    </>}
  </div>;
}
