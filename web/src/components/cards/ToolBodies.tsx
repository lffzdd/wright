import { ArrowSquareOut, CaretDown, File, FolderSimple } from "@phosphor-icons/react";
import { useState, type CSSProperties, type ReactNode } from "react";
import { api } from "../../api";
import { parseDiff } from "../code/diff";
import { t, useT } from "../../i18n";
import { CodeView } from "../code/CodeView";
import { languageForPath, useHighlight } from "../code/highlight";
import { useOpenFile } from "../code/openFile";
import { Expandable, StructuredOutput, parseJsonOutput } from "./StructuredOutput";
import { isDirectoryTool, toolCategory } from "./toolKinds";
import { formatSize, textOf } from "./values";

/** Diff rows an edit card shows before "Show all"; the rest stays one click away. */
export const DIFF_PREVIEW_ROWS = 16;

export function DiffBody({ text, limit, path }: { text: string; limit?: number; path?: string }) {
  const tr = useT();
  const [all, setAll] = useState(false);
  const parsed = parseDiff(text).lines;
  // Highlight the code lines as one block so multi-line constructs colour right.
  const codeRows = parsed.filter((line) => line.type !== "hunk" && line.type !== "meta");
  const tokens = useHighlight(codeRows.map((line) => line.content).join("\n"), languageForPath(path));
  const tokenAt = new Map(codeRows.map((line, index) => [line, tokens?.[index]]));
  const lines = text.split(/\r?\n/).filter((line) => line.length > 0);
  const structured = /^@@ /m.test(text) || (lines.length > 0 && lines.every((line) => line.startsWith("+") || line.startsWith("-") || line.startsWith(" ")));
  if (!structured) return <pre className="term-body">{text}</pre>;
  const rows = parsed;
  // Only cap when it hides a meaningful amount; a couple of extra rows just show.
  const capped = !all && typeof limit === "number" && rows.length > limit + 4;
  return <div className="diff-body">
    {(capped ? rows.slice(0, limit) : rows).filter((line) => !(line.type === "meta" && /^(---|\+\+\+) /.test(line.content))).map((line, index) => line.type === "hunk" || line.type === "meta"
      ? <div className="diff-meta" key={index}>{line.content}</div>
      : <div className={line.type === "add" ? "diff-add" : line.type === "del" ? "diff-del" : "diff-ctx"} key={index}>
        <span className="diff-line diff-line-old">{line.oldNum ?? ""}</span>
        <span className="diff-line diff-line-new">{line.newNum ?? ""}</span>
        <span className="diff-mark">{line.type === "add" ? "+" : line.type === "del" ? "-" : " "}</span>
        <span className="diff-text">{tokenAt.get(line)?.length
          ? tokenAt.get(line)!.map((token, at) => <span key={at} className="cv-tok" style={token.style as CSSProperties}>{token.content}</span>)
          : line.content || " "}</span>
      </div>)}
    {capped && <button type="button" className="diff-more" onClick={() => setAll(true)}>
      <CaretDown size={11} aria-hidden="true" />{tr("web.diff_show_all", { count: rows.length - limit! })}
    </button>}
  </div>;
}

export function looksLikeTestFailure(output: string): boolean {
  return /AssertionError|Tests:\s*\d+\s*failed/i.test(output);
}

export function TestFailureOutput({ text }: { text: string }) {
  const lines = text.split(/\r?\n/);
  return <pre className="term-body failed test-failure-output" aria-label={text}>{lines.map((line, index) => {
    const testLocation = /(?:^|\s)[^\s>]+\.(?:test|spec)\.[cm]?[jt]sx?(?:\s*>|:)/i.test(line);
    const assertion = /AssertionError|expected.+received|received.+expected/i.test(line);
    const summary = /Tests:\s*\d+\s+failed,\s*\d+\s+passed/i.test(line);
    const parts = summary ? line.split(/(\d+\s+failed|\d+\s+passed)/gi) : [line];
    return <span className={`term-line${testLocation ? " test-location" : ""}${assertion ? " assertion" : ""}${summary ? " test-summary" : ""}`} key={index}>{parts.map((part, partIndex) => {
      const failed = /^\d+\s+failed$/i.test(part);
      const passed = /^\d+\s+passed$/i.test(part);
      return failed || passed ? <span className={failed ? "test-count-failed" : "test-count-passed"} key={partIndex}>{part}</span> : part;
    })}</span>;
  })}</pre>;
}

type DirectoryEntry = { name?: unknown; path?: unknown; type?: unknown; size?: unknown };

export function DirectoryTree({ source, globStyle }: { source: Record<string, unknown>; globStyle?: boolean }) {
  const tr = useT();
  const entries = source.entries as DirectoryEntry[];
  if (!entries.length) return <p className="empty-small">{tr("web.dir_empty")}</p>;
  if (globStyle) {
    return <div className="glob-list">
      {entries.map((entry, index) => {
        const name = String(entry.name ?? entry.path ?? "");
        return <div key={`${name}-${index}`}><File size={13} aria-hidden="true" /><span>{name}</span></div>;
      })}
      {source.truncated === true && <div className="muted">…</div>}
    </div>;
  }
  return <div className="dir-tree">
    {entries.map((entry, index) => {
      const name = String(entry.name ?? entry.path ?? "");
      const folder = entry.type === "directory";
      return <div className="dir-entry" key={`${name}-${index}`}>
        {folder ? <FolderSimple size={13} aria-hidden="true" /> : <File size={13} aria-hidden="true" />}
        <span className="dir-name">{folder ? `${name}/` : name}</span>
        {!folder && typeof entry.size === "number" && <span className="dir-size">{formatSize(entry.size)}</span>}
      </div>;
    })}
    {source.truncated === true && <div className="dir-entry muted">…</div>}
  </div>;
}

/** Number of entries an ls result reports, from its data. */
export function directoryEntryCount(result?: Record<string, unknown> | null, data?: unknown): number {
  const candidates = [data, result?.data, result];
  for (const candidate of candidates) {
    if (candidate && typeof candidate === "object" && Array.isArray((candidate as { entries?: unknown }).entries)) {
      return ((candidate as { entries: unknown[] }).entries).length;
    }
  }
  return 0;
}

export type SearchMatch = { path?: unknown; line?: unknown; text?: unknown };

export function searchMatches(source: Record<string, unknown> | null | undefined): SearchMatch[] | null {
  return source && Array.isArray(source.matches) ? source.matches as SearchMatch[] : null;
}

function highlighter(pattern: string, literal: boolean): RegExp | null {
  if (!pattern) return null;
  try {
    return new RegExp(literal ? pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : pattern, "g");
  } catch {
    return null;
  }
}

function Highlighted({ text, pattern }: { text: string; pattern: RegExp | null }) {
  if (!pattern) return <>{text}</>;
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (!match[0]) break;
    const start = match.index ?? 0;
    if (start > last) parts.push(text.slice(last, start));
    parts.push(<mark key={start}>{match[0]}</mark>);
    last = start + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}

/** Search hits, one line each: file, line number, the line with the pattern marked. A hit opens its file there. */
export function SearchResults({ matches, pattern, literal }: { matches: SearchMatch[]; pattern: string; literal: boolean }) {
  const tr = useT();
  const openFile = useOpenFile();
  if (!matches.length) return <p className="empty-small">{tr("web.steps.no_matches")}</p>;
  const regex = highlighter(pattern, literal);
  return <div className="search-results">
    {matches.map((hit, index) => {
      const path = String(hit.path ?? "");
      const line = typeof hit.line === "number" ? hit.line : undefined;
      const open = openFile ? () => openFile(path, line) : undefined;
      return <div className={`grep-row search-hit${open ? " clickable" : ""}`} key={index} onClick={open} title={open ? tr("web.open_in_files") : undefined}>
        <span className="search-hit-path mono">{path}</span>
        <span className="search-hit-line mono">{line ?? ""}</span>
        <span className="search-hit-text mono"><Highlighted text={textOf(hit.text).trim()} pattern={regex} /></span>
      </div>;
    })}
  </div>;
}

/** File content read with numbered lines ("12|text"), shown with a gutter and syntax colors. */
export function NumberedContent({ text, path }: { text: string; path?: string }) {
  const openFile = useOpenFile();
  const lines = text.replace(/\n$/, "").split("\n").map((line) => {
    const match = /^(\d+)\|(.*)$/.exec(line);
    return match ? { num: Number(match[1]), text: match[2] } : { text: line };
  });
  return <CodeView lines={lines} lang={languageForPath(path)} label={path}
    onLine={openFile && path ? (line) => openFile(path, line) : undefined} />;
}

export function isNumbered(text: string): boolean {
  const lines = text.split("\n").filter(Boolean);
  return lines.length > 0 && lines.every((line) => /^\d+\|/.test(line));
}

/** Renders ls's structured entries as a tree, one entry per line. */
function directoryListing(source: Record<string, unknown> | null | undefined): string | null {
  const entries = source && Array.isArray(source.entries) ? source.entries as DirectoryEntry[] : null;
  if (!entries) return null;
  if (!entries.length) return t("web.dir_empty");
  const lines = entries.map((entry, index) => {
    const name = String(entry.name ?? entry.path ?? "");
    const branch = index === entries.length - 1 && source?.truncated !== true ? "└──" : "├──";
    if (entry.type === "directory") return `${branch} ${name}/`;
    return typeof entry.size === "number" ? `${branch} ${name}  ${formatSize(entry.size)}` : `${branch} ${name}`;
  });
  if (source?.truncated === true) lines.push("└── …");
  return lines.join("\n");
}

function directoryTree(text: string): string {
  if (text.startsWith("{") || text.startsWith("[")) return text;
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  if (lines.length === 0) return text;
  if (lines.some((line) => line.startsWith("├") || line.startsWith("└") || line.startsWith("//"))) return text;
  return lines.map((line, index) => `${index === lines.length - 1 ? "└──" : "├──"} ${line}`).join("\n");
}

/** A one-line text for tool results that have no richer view. */
export function compactToolBody(name: string, body: string, result?: Record<string, unknown> | null): string {
  const trimmed = body.trim();
  if (isDirectoryTool(name)) {
    const parsed = result ?? (() => {
      try { return JSON.parse(trimmed) as Record<string, unknown>; } catch { return null; }
    })();
    return directoryListing(parsed) ?? directoryTree(trimmed);
  }
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return body;
  const source = result && typeof result === "object" ? result : (() => {
    try { return JSON.parse(trimmed) as Record<string, unknown>; } catch { return null; }
  })();
  if (!source) return body;
  if (typeof source.objective === "string") return source.objective || "No active plan";
  if (Array.isArray(source.tasks)) return source.tasks.length ? `${source.tasks.length} tasks` : "No tasks";
  if (Array.isArray(source.schedules)) return source.schedules.length ? `${source.schedules.length} schedules` : "No schedules";
  if (Array.isArray(source.memories) || (toolCategory(name) === "memory" && Array.isArray(source.results))) {
    const count = (source.memories as unknown[] | undefined)?.length ?? (source.results as unknown[] | undefined)?.length ?? 0;
    return count ? `${count} memories` : "No memories";
  }
  if (typeof source.count === "number") return `${source.count} results`;
  return body;
}

type WebHit = { title: string; url: string; snippet: string };

function httpUrl(value: string): string {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

/** web_search (and similar) hits: title, URL, short snippet. Memory search results have no URL. */
export function webHits(source: Record<string, unknown> | null | undefined): WebHit[] | null {
  if (!source || !Array.isArray(source.results)) return null;
  const items = source.results as unknown[];
  if (!items.every((item) => item && typeof item === "object" && ("url" in item || "snippet" in item))) return null;
  return items.map((item) => {
    const record = item as Record<string, unknown>;
    return { title: textOf(record.title), url: textOf(record.url), snippet: textOf(record.snippet) };
  });
}

export function WebResults({ hits }: { hits: WebHit[] }) {
  const tr = useT();
  if (!hits.length) return <p className="empty-small">{tr("web.steps.no_results")}</p>;
  return <div className="web-results">
    {hits.map((hit, index) => {
      const href = httpUrl(hit.url);
      const title = hit.title || hit.url;
      return <article className="web-hit" key={`${hit.url}-${index}`}>
        {href
          ? <a className="web-hit-title" href={href} target="_blank" rel="noopener noreferrer"><span>{title}</span><ArrowSquareOut size={12} aria-hidden="true" /></a>
          : <span className="web-hit-title">{title}</span>}
        {hit.url && hit.url !== title && <div className="web-hit-url mono">{hit.url}</div>}
        {hit.snippet && <p className="web-hit-snippet">{hit.snippet}</p>}
      </article>;
    })}
  </div>;
}

/**
 * Tool output with no dedicated view: JSON is laid out for reading, other text
 * is shown as is, both capped to a few lines until expanded. Running output is
 * left uncapped so the newest lines stay in view.
 */
export function PlainOutput({ body, failed, structured, live }: { body: string; failed: boolean; structured: boolean; live: boolean }) {
  const json = structured && !failed ? parseJsonOutput(body) : undefined;
  if (json !== undefined) return <StructuredOutput value={json} />;
  const text = <pre className={`term-body ${failed ? "failed" : ""}`}>{body}</pre>;
  return live ? text : <Expandable>{text}</Expandable>;
}

export type SandboxDenial = { path: string; grant_directory: string; grant_write_directory?: string; readable: string[]; writable: string[] };

export function SandboxDenialHint({ denial, sessionId }: { denial: SandboxDenial; sessionId?: string }) {
  const tr = useT();
  const [state, setState] = useState<"idle" | "busy" | "granted" | "failed">("idle");
  const [wrote, setWrote] = useState(false);
  const readDirectory = denial.grant_directory;
  const writeDirectory = denial.grant_write_directory ?? "";
  const grant = (write: boolean) => {
    if (!sessionId) return;
    const path = write ? writeDirectory : readDirectory;
    setState("busy");
    setWrote(write);
    api.grants(sessionId)
      .then((grants) => api.addGrant(sessionId, { kind: "directory", path, operations: write ? ["file_read", "file_write"] : ["file_read"] }, "session", grants.version))
      .then(() => setState("granted"), () => setState("failed"));
  };
  const offer = sessionId && state !== "granted";
  return <div className="sandbox-denial" role="status">
    <p>{tr(denial.path ? "web.sandbox.denied_path" : "web.sandbox.denied", { path: denial.path })}</p>
    <p className="mono">{tr("web.sandbox.scope_read", { paths: denial.readable.join(", ") })}</p>
    {offer && (readDirectory || writeDirectory) && <div className="sandbox-denial-actions">
      {readDirectory && <button type="button" className="button" disabled={state === "busy"} onClick={() => grant(false)}>
        {tr(state === "busy" && !wrote ? "web.submitting" : "web.sandbox.grant_read", { path: readDirectory })}
      </button>}
      {writeDirectory && <button type="button" className="button" disabled={state === "busy"} onClick={() => grant(true)}>
        {tr(state === "busy" && wrote ? "web.submitting" : "web.sandbox.grant_write", { path: writeDirectory })}
      </button>}
    </div>}
    {state === "granted" && <p>{tr(wrote ? "web.sandbox.granted_write" : "web.sandbox.granted", { path: wrote ? writeDirectory : readDirectory })}</p>}
    {state === "failed" && <p className="form-error" role="alert">{tr("web.sandbox.grant_failed")}</p>}
    <p className="hint">{tr("web.sandbox.denied_host")}</p>
  </div>;
}

export function ReadToolbar({ path, line }: { path: string; line: number }) {
  const tr = useT();
  const openFile = useOpenFile();
  if (!openFile) return null;
  return <div className="tool-toolbar">
    <button type="button" className="text-button" onClick={() => openFile(path, line)}><ArrowSquareOut size={12} />{tr("web.open_in_files")}</button>
  </div>;
}
