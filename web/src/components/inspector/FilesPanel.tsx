import { ArrowClockwise, At, CaretDoubleUp, CaretRight, ChatCircleDots, Crosshair, Eye, EyeSlash, File, FileCode, FileImage, FileLock, FileText, Folder, FolderOpen, GitDiff, MagnifyingGlass, X } from "@phosphor-icons/react";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useT } from "../../i18n";
import { CodeView } from "../code/CodeView";
import { languageForPath } from "../code/highlight";
import { SegmentedControl } from "../ui/SegmentedControl";

export type TreeEntry = { name: string; path: string; kind: string; size?: number | null };
export type AccessedFile = { path: string; access?: string; call_id?: string; tool?: string; tools?: Record<string, number>; last_calls?: Record<string, string> };
type FileMeta = { size?: number; additions?: number; deletions?: number };
// A reload keeps the previous entries on screen until the new listing arrives.
type Branch = { status: "loading"; entries?: TreeEntry[] } | { status: "error"; error: string } | { status: "ready"; entries: TreeEntry[] };

const splitPath = (path: string) => {
  const index = path.replace(/\/+$/, "").lastIndexOf("/");
  return index < 0 ? { dir: "", name: path } : { dir: path.slice(0, index + 1), name: path.slice(index + 1) };
};
const sortEntries = (entries: TreeEntry[]) => [...entries].sort((a, b) => (a.kind === "dir" ? 0 : 1) - (b.kind === "dir" ? 0 : 1) || a.name.localeCompare(b.name));
/** Lines past this are cut from the preview to keep the panel responsive. */
const PREVIEW_LINES = 4000;
/** The refresh icon spins at least this long, so a fast reload still reads as one. */
const MIN_SPIN_MS = 500;

/** Cache and tool folders that are hidden unless the user asks to see them. */
const CACHE_NAMES = new Set([".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".parcel-cache", ".turbo", ".next", ".specstory", ".DS_Store"]);
const isCachePath = (path: string) => path.split("/").some((part) => CACHE_NAMES.has(part));
/** Directories from the root down to `path`; `includeSelf` adds the path itself. */
const chainOf = (path: string, includeSelf: boolean) => {
  const parts = path.split("/").filter(Boolean);
  const count = includeSelf ? parts.length : parts.length - 1;
  return Array.from({ length: Math.max(0, count) }, (_, index) => parts.slice(0, index + 1).join("/"));
};

/** Last suffix, or the name of a dotfile (`.gitignore` → gitignore). `.env*` stays env. */
const extOf = (name: string) => {
  const lower = name.toLowerCase();
  if (/^\.env($|\.)/.test(lower)) return "env";
  if (name.startsWith(".")) {
    const rest = name.slice(1);
    return rest.includes(".") ? rest.split(".").pop()!.toLowerCase() : rest;
  }
  return name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
};
const EXT_GROUPS: Record<string, string> = {
  ts: "code", tsx: "code", js: "code", jsx: "code", py: "code", go: "code", rs: "code", java: "code", kt: "code", swift: "code",
  c: "code", cc: "code", cpp: "code", h: "code", rb: "code", sh: "code",
  css: "style", scss: "style", less: "style", html: "style",
  md: "doc", mdx: "doc", txt: "doc", rst: "doc", pdf: "doc",
  json: "config", yaml: "config", yml: "config", toml: "config", ini: "config", lock: "config", env: "config", cfg: "config", xml: "config",
  gitignore: "config", gitattributes: "config", gitmodules: "config", editorconfig: "config", dockerignore: "config",
  npmrc: "config", nvmrc: "config", prettierrc: "config", eslintrc: "config", babelrc: "config", envrc: "config",
  cursorindexingignore: "config",
  png: "image", jpg: "image", jpeg: "image", gif: "image", svg: "image", webp: "image", ico: "image",
};
const FileIcon = ({ name }: { name: string }) => {
  const ext = extOf(name);
  const group = EXT_GROUPS[ext] ?? "other";
  const Icon = ext === "lock" ? FileLock : group === "doc" ? FileText : group === "image" ? FileImage : group === "other" ? File : FileCode;
  return <Icon size={14} className={`ico files-icon ext-${group}`} aria-hidden="true" />;
};
/** Placeholder rows for a folder that is loading; they fade in late so quick loads do not flash. */
const TreeSkeleton = ({ depth, label }: { depth: number; label: string }) => <div className="files-skeleton" role="status" style={{ paddingLeft: 8 + depth * 14 + 12 }}>
  <span className="sr-only">{label}</span>
  {[62, 44, 54].map((width) => <span key={width} className="files-skeleton-bar" aria-hidden="true" style={{ width: `${width}%` }} />)}
</div>;
const FolderIcon = ({ open }: { open?: boolean }) => open ? <FolderOpen size={14} className="ico files-icon dir" aria-hidden="true" /> : <Folder size={14} className="ico files-icon dir" aria-hidden="true" />;
/**
 * What the session did to a path, from the strongest tool used on it: any edit
 * or write is MOD, then READ, GREP, LIST. Older payloads without per-tool
 * counts fall back to the last tool and the access kind.
 */
const ACCESS_TAGS: Array<{ kind: string; tools: string[] }> = [
  // Current names first; the legacy names still appear in older sessions' data.
  { kind: "modified", tools: ["edit", "write", "edit_file", "write_file"] },
  { kind: "read", tools: ["read", "read_file"] },
  { kind: "search", tools: ["grep"] },
  { kind: "list", tools: ["ls", "glob", "list_directory"] },
];
const accessKind = (file: AccessedFile) => {
  const used = file.tools && Object.keys(file.tools).length ? Object.keys(file.tools) : file.tool ? [file.tool] : [];
  if (file.access === "write") return ACCESS_TAGS[0];
  return ACCESS_TAGS.find((tag) => tag.tools.some((tool) => used.includes(tool)))
    ?? (file.access === "list" ? ACCESS_TAGS[3] : ACCESS_TAGS[1]);
};
/** The call behind a file's use: the latest call of its strongest tool (an edit, not a later read). */
export const usageCallId = (file: AccessedFile) => {
  const tag = accessKind(file);
  const latest = Object.entries(file.last_calls ?? {}).filter(([tool]) => tag.tools.includes(tool)).pop();
  return latest?.[1] ?? file.call_id;
};
/** "read ×2 · edit" — each tool once, with a count when it ran more than once. */
const toolSummary = (file: AccessedFile) => {
  const tools = Object.entries(file.tools ?? {});
  if (!tools.length) return file.tool ?? "";
  return tools.map(([tool, count]) => count > 1 ? `${tool} ×${count}` : tool).join(" · ");
};
/** Whether the session did real work on a path; a bare `ls` of a folder is not worth a row. */
export const isWorkUse = (file: AccessedFile) => file.access !== "list" && accessKind(file).kind !== "list";
const statusLetter = (status: string) => status.trim().replace("??", "?").charAt(0).toUpperCase() || "?";
const treeKind = (status: string) => {
  const letter = statusLetter(status);
  return letter === "A" || letter === "?" || letter === "U" ? "new" : letter === "D" ? "del" : "mod";
};
const treeLetter = (status: string) => status.trim()[0] === "?" ? "A" : statusLetter(status);
/** Absolute path for tooltips, so a hover adds the location instead of repeating the name. */
const absolutePath = (path: string, root?: string) => {
  const base = (root ?? "").replace(/\\/g, "/").replace(/\/+$/, "");
  const normalized = path.replace(/\\/g, "/");
  if (!base || normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized)) return path;
  return /^\.?\/?$/.test(normalized) ? base : `${base}/${normalized.replace(/^\.\//, "")}`;
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

export function FilesPanel({ sessionKey, canReadFiles, historyOnly, accessedFiles, accessCounts, readTree, readFile, searchFiles, filePath, focusLine, openFile, changes, openChanges, projectName, projectRoot, showCall, onReference }: {
  sessionKey: string;
  canReadFiles: boolean;
  historyOnly: boolean;
  accessedFiles: AccessedFile[];
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
  /** Absolute project directory; tooltips show full paths under it. */
  projectRoot?: string;
  /** Scrolls the conversation to a tool call. */
  showCall?: (callId: string) => void;
  /** Adds the open file as a composer @-reference. */
  onReference?: (path: string) => void;
}) {
  const tr = useT();
  const [branches, setBranches] = useState<Record<string, Branch>>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [query, setQuery] = useState("");
  // The filter lives behind a button in the project header and stays open while it holds text.
  const [searchOpen, setSearchOpen] = useState(false);
  const searchButtonRef = useRef<HTMLButtonElement>(null);
  const [results, setResults] = useState<{ query: string; paths: string[] } | { query: string; error: string } | null>(null);
  const [preview, setPreview] = useState<{ path: string; text?: string; binary?: boolean; error?: string } | null>(null);
  const previewRef = useRef<HTMLElement>(null);
  const treeRef = useRef<HTMLDivElement>(null);
  const [showCache, setShowCacheState] = useState(() => readFlag("wright.files.showCache"));
  const setShowCache = (next: boolean) => { setShowCacheState(next); writeFlag("wright.files.showCache", next); };
  const [revealPath, setRevealPath] = useState<string | null>(null);
  const generation = useRef(0);
  const [spinning, setSpinning] = useState(false);
  // The header switches the body between the project tree and the files this session used.
  const [view, setView] = useState<"tree" | "session">("tree");
  const spinStarted = useRef(0);

  const changeStatus = useMemo(() => new Map(changes.map((item) => [item.path, item.status])), [changes]);

  const updateExpanded = useCallback((next: Set<string>) => {
    setExpanded(next);
    expandedBySession.set(sessionKey, [...next]);
  }, [sessionKey]);

  const load = useCallback((path: string) => {
    const owner = generation.current;
    setBranches((current) => {
      const previous = current[path];
      return { ...current, [path]: { status: "loading", entries: previous && "entries" in previous ? previous.entries : undefined } };
    });
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
    setBranches({}); setExpanded(new Set(saved)); setQuery(""); setSearchOpen(false); setResults(null); setRevealPath(null); setView("tree");
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

  const treeLoading = Object.values(branches).some((branch) => branch.status === "loading");
  useEffect(() => {
    if (!spinning || treeLoading) return;
    const timer = window.setTimeout(() => setSpinning(false), Math.max(0, MIN_SPIN_MS - (Date.now() - spinStarted.current)));
    return () => window.clearTimeout(timer);
  }, [spinning, treeLoading]);

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
    spinStarted.current = Date.now();
    setSpinning(true);
    for (const path of new Set(["", ...expanded, ...Object.keys(branches)])) load(path);
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
    setQuery(""); setResults(null); setSearchOpen(false); setView("tree");
    setRevealPath(path);
    openFile("");
  };

  const closeSearch = () => {
    setQuery(""); setSearchOpen(false);
    window.requestAnimationFrame(() => searchButtonRef.current?.focus());
  };
  // "/" or Cmd/Ctrl+F anywhere in the panel (outside a text field) opens the filter.
  const onPanelKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!canReadFiles || (event.target as HTMLElement).closest("input, textarea")) return;
    const find = (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "f";
    if (!find && !(event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey)) return;
    event.preventDefault();
    setSearchOpen(true);
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
    const lines = preview?.text?.replace(/\n$/, "").split("\n") ?? [];
    const shown = lines.length > PREVIEW_LINES ? lines.slice(0, PREVIEW_LINES) : lines;
    const lang = languageForPath(filePath);
    const status = changeStatus.get(filePath);
    const letter = status ? treeLetter(status) : "";
    const usage = accessedFiles.find((file) => file.path === filePath && file.access !== "list");
    const usageCall = usage && showCall ? usageCallId(usage) : undefined;
    return <section ref={previewRef} tabIndex={-1} className="inspector-file-preview files-preview file-view" aria-label={tr("web.content_preview")}>
      <div className="pane-head files-preview-head">
        <button type="button" className="icon-btn sm" onClick={() => openFile("")} aria-label={tr("web.back_to_files")} title={tr("web.back_to_files")}><CaretRight className="chev-back" size={14} aria-hidden="true" /></button>
        <span className="crumb-path grow" title={filePath}>{filePath}</span>
        {letter && <span className={`badge ${letter}`} title={status}>{letter}</span>}
        {usageCall && <button type="button" className="icon-btn sm" onClick={() => showCall?.(usageCall)} aria-label={tr("web.files.show_call")} title={`${tr("web.files.show_call")} · ${toolSummary(usage!)}`}><ChatCircleDots size={14} /></button>}
        {changeStatus.has(filePath) && <button type="button" className="icon-btn sm" onClick={() => openChanges(filePath)} aria-label={tr("web.files.view_diff")} title={tr("web.files.view_diff")}><GitDiff size={14} /></button>}
        {onReference && <button type="button" className="btn btn-ghost btn-sm mention-file" onClick={() => onReference(filePath)}><At size={14} aria-hidden="true" />{tr("web.cite")}</button>}
      </div>
      {preview?.error && <p className="form-error" role="alert">{preview.error}</p>}
      {!preview?.error && preview?.text === undefined && <p className="hint" role="status">{tr("web.inspector_loading")}</p>}
      {preview?.binary && <p className="hint">{tr("web.binary_file")}</p>}
      {preview?.text !== undefined && !preview.binary && (preview.text
        ? <div className="file-view-code"><CodeView variant="panel" wrap={false} lang={lang} label={filePath} focusLine={focusLine}
          lines={shown.map((text, index) => ({ num: index + 1, text }))} /></div>
        : <p className="empty-small">{tr("web.empty_file")}</p>)}
      {lines.length > PREVIEW_LINES && <p className="hint">{tr("web.files.truncated", { shown: PREVIEW_LINES, total: lines.length })}</p>}
    </section>;
  }

  // Folders known from any loaded listing, so a grep over `src` reads as a folder, not a file.
  const dirPaths = new Set(Object.values(branches).flatMap((branch) => "entries" in branch ? (branch.entries ?? []).filter((entry) => entry.kind === "dir").map((entry) => entry.path) : []));
  // Latest first, as the conversation reads bottom-up from the current turn.
  const sessionFiles = [...accessedFiles].reverse().filter((file) => isWorkUse(file) && (showCache || !isCachePath(file.path)));
  const sessionView = view === "session" && sessionFiles.length > 0;
  const sessionList = <section className="files-session" aria-label={tr("web.files.session")}>
    <div className="list-rows">
      {sessionFiles.map((file) => {
        const isRoot = /^\.?\/?$/.test(file.path);
        const isDir = isRoot || dirPaths.has(file.path);
        const { dir, name } = splitPath(file.path);
        const written = accessKind(file).kind === "modified";
        const callId = showCall ? usageCallId(file) : undefined;
        const count = accessCounts[file.path] ?? 1;
        return <div className="lr" key={file.path}>
          {written ? <span className="badge M" title={tr("web.files.filter_changed")}>M</span> : isDir ? <FolderIcon /> : <FileIcon name={name} />}
          {/* A folder opens in the tree; a file opens its preview. */}
          <button type="button" className="grow mono files-session-path" disabled={!canReadFiles} onClick={() => isDir ? reveal(isRoot ? "" : file.path, true) : openFile(file.path)} title={absolutePath(file.path, projectRoot)}>
            <bdi>{isRoot ? <b>{projectName || file.path}</b> : <><span>{dir}</span><b>{name}{isDir ? "/" : ""}</b></>}</bdi>
          </button>
          <span className="muted" title={tr("web.files.access_count", { count })}>{toolSummary(file)}</span>
          {callId && <button type="button" className="icon-btn sm" onClick={() => showCall?.(callId)} aria-label={`${tr("web.files.show_call")}: ${file.path}`} title={tr("web.files.show_call")}><ChatCircleDots size={14} /></button>}
          <button type="button" className="icon-btn sm" onClick={() => reveal(isRoot ? "" : file.path, isDir)} disabled={!canReadFiles} aria-label={`${tr("web.files.locate")}: ${file.path}`} title={tr("web.files.locate")}><Crosshair size={14} /></button>
        </div>;
      })}
    </div>
  </section>;

  const usedPaths = new Set(accessedFiles.filter(isWorkUse).map((file) => file.path));
  const changedDirs = (() => {
    const dirs = new Set<string>();
    for (const path of changeStatus.keys()) for (const dir of chainOf(path, false)) dirs.add(dir);
    return dirs;
  })();
  const fileCount = Object.values(branches).reduce((sum, branch) => {
    const entries = "entries" in branch ? branch.entries ?? [] : [];
    return sum + entries.filter((entry) => entry.kind !== "dir" && (showCache || !CACHE_NAMES.has(entry.name))).length;
  }, 0);

  const tree = (path: string, depth: number): ReactNode => {
    const branch = branches[path];
    if (!branch || (branch.status === "loading" && !branch.entries)) return <TreeSkeleton depth={depth} label={tr("web.inspector_loading")} />;
    if (branch.status === "error") return <p className="form-error files-tree-note" role="alert" style={{ marginLeft: 8 + depth * 14 }}>{branch.error}</p>;
    const entries = (branch.entries ?? []).filter((entry) => showCache || !CACHE_NAMES.has(entry.name));
    if (!entries.length) return <p className="empty-small files-tree-note" style={{ paddingLeft: 8 + depth * 14 }}>{tr("web.dir_empty")}</p>;
    return entries.map((entry) => {
      const pad = { paddingLeft: 8 + depth * 14 };
      if (entry.kind === "dir") {
        const open = expanded.has(entry.path);
        const hasMod = changedDirs.has(entry.path);
        return <div key={entry.path} role="none">
          <button type="button" className={`tree-row files-row files-dir${open ? " open" : ""}`} role="treeitem" data-path={entry.path} data-dir="true" aria-level={depth + 1} aria-expanded={open} style={pad} onClick={() => toggle(entry.path)} title={absolutePath(entry.path, projectRoot)}>
            <CaretRight size={12} className="chev ico" aria-hidden="true" />
            <FolderIcon open={open} />
            <span className="name">{entry.name}</span>
            {hasMod && <span className="st" style={{ color: "var(--amber)" }} title={tr("web.files.dir_changed")}>•</span>}
          </button>
          {open && <div role="group" className="files-group">{tree(entry.path, depth + 1)}</div>}
        </div>;
      }
      const status = changeStatus.get(entry.path);
      const letter = status ? treeLetter(status) : "";
      const kind = status ? treeKind(status) : "";
      return <button key={entry.path} type="button" className={`tree-row files-row${kind ? ` ${kind}` : ""}`} role="treeitem" data-path={entry.path} aria-level={depth + 1} style={pad} disabled={!canReadFiles} title={absolutePath(entry.path, projectRoot)} onClick={() => openFile(entry.path)}>
        <span className="tree-slot" aria-hidden="true" />
        <FileIcon name={entry.name} />
        <span className="name">{entry.name}</span>
        {usedPaths.has(entry.path) && <span className="incontext" title={tr("web.files.used")} />}
        {letter && <span className="st">{letter}</span>}
      </button>;
    });
  };

  const searchPaths = results && "paths" in results ? results.paths.filter((path) => showCache || !isCachePath(path)) : [];
  const searching = !!query.trim();
  const searchVisible = searchOpen || !!query;
  const searchResults = <section className="files-section files-results" aria-label={tr("web.files.results")}>
    {!results || results.query !== query.trim() ? <p className="hint files-delayed" role="status">{tr("web.inspector_loading")}</p>
      : "error" in results ? <p className="form-error" role="alert">{results.error}</p>
      : searchPaths.length ? <div className="tree files-list files-results-list" key={results.query}>{searchPaths.map((path) => {
        const { dir, name } = splitPath(path);
        return <div key={path} className="files-result">
          <button type="button" className="tree-row files-row" data-path={path} disabled={!canReadFiles} title={absolutePath(path, projectRoot)} onClick={() => openFile(path)}>
            <span className="tree-slot" aria-hidden="true" />
            <FileIcon name={name} />
            <span className="name">{highlightName(name, query)}</span>
            {dir ? <span className="tree-path">{dir}</span> : null}
          </button>
          <span className="files-row-actions"><button type="button" className="icon-button files-locate" onClick={() => reveal(path, false)} aria-label={tr("web.files.locate")} title={`${tr("web.files.locate")}: ${path}`}><Crosshair size={13} /></button></span>
        </div>;
      })}</div>
      : <p className="empty-small">{tr("web.files.no_match")}</p>}
  </section>;

  return <div className="files-panel" onKeyDown={onPanelKey}>
    {historyOnly && !canReadFiles && <p className="hint">{tr("web.inspector_history_files")}</p>}
    <div className={`pane-head files-project-head${searchVisible ? " searching" : ""}`}>
      {searchVisible ? <label className="files-filter">
        <MagnifyingGlass size={13} aria-hidden="true" />
        <input type="search" autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder={tr("web.files.filter")} aria-label={tr("web.files.filter")}
          onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeSearch(); } }}
          onBlur={() => { if (!query) setSearchOpen(false); }} />
        <button type="button" className="files-filter-clear" onMouseDown={(event) => event.preventDefault()} onClick={closeSearch} aria-label={tr("web.files.clear_filter")} title={tr("web.files.clear_filter")}><X size={12} /></button>
      </label> : <>
        {sessionFiles.length > 0
          ? <SegmentedControl className="files-view-switch" value={sessionView ? "session" : "tree"} ariaLabel={tr("web.files.view")} onChange={setView}
            options={[
              { value: "tree", label: projectName || tr("web.files.project"), title: tr("web.files.project") },
              { value: "session", label: `${tr("web.files.view.session")} ${sessionFiles.length}`, title: tr("web.files.session") },
            ]} />
          : <b className="files-project-name" title={projectName}>{projectName || tr("web.files.project")}</b>}
        {!sessionView && <span className="files-count-label">{tr("web.files_count", { count: fileCount })}</span>}
      </>}
      <span className="grow" />
      <span className="files-head-actions">
        <button type="button" className={`files-cache-toggle${showCache ? " on" : ""}`} onClick={() => setShowCache(!showCache)} aria-pressed={showCache} aria-label={tr(showCache ? "web.files.hide_cache" : "web.files.show_cache")} title={tr(showCache ? "web.files.hide_cache" : "web.files.show_cache")}>{showCache ? <Eye size={13} /> : <EyeSlash size={13} />}</button>
        {!searchVisible && <button type="button" ref={searchButtonRef} className="icon-button" onClick={() => setSearchOpen(true)} disabled={!canReadFiles} aria-label={tr("web.files.filter")} title={`${tr("web.files.filter")} (/)`}><MagnifyingGlass size={13} /></button>}
        <button type="button" className="icon-button" onClick={collapseAll} disabled={!canReadFiles || sessionView || expanded.size === 0} aria-label={tr("web.files.collapse_all")} title={tr("web.files.collapse_all")}><CaretDoubleUp size={13} /></button>
        <button type="button" className="icon-button" onClick={refresh} disabled={!canReadFiles} aria-busy={spinning} aria-label={tr("web.files.refresh")} title={tr("web.files.refresh")}><ArrowClockwise size={13} className={spinning ? "files-spin" : undefined} /></button>
      </span>
    </div>
    {searching ? searchResults : sessionView ? sessionList : <section className="files-section" aria-label={tr("web.files.project")}>
      {canReadFiles ? <div className="tree files-tree" role="tree" ref={treeRef} onKeyDown={onTreeKey} aria-label={tr("web.files.project")}>{tree("", 0)}</div> : null}
    </section>}
  </div>;
}
