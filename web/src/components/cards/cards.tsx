import { ArrowsClockwise, BookOpen, Brain, CaretDown, ChatCircleDots, File, FileText, FolderSimple, Globe, Lightbulb, ListChecks, MagnifyingGlass, PencilSimple, TerminalWindow, Wrench, X, type Icon as PhosphorIcon } from "@phosphor-icons/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { MarkdownContent } from "../../Markdown";
import { api } from "../../api";
import { parseDiff } from "../../diff";
import { present, t, useT } from "../../i18n";
import type { Interaction, TimelineItem, ToolState } from "../../types";
import { PermissionApproval } from "../permissions/approval";

function textOf(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function arg(record: Record<string, unknown> | undefined, keys: string[]): string {
  if (!record) return "";
  for (const key of keys) {
    const value = textOf(record[key]);
    if (value) return value;
  }
  return "";
}

function DiffBody({ text }: { text: string }) {
  const lines = text.split(/\r?\n/).filter((line) => line.length > 0);
  const structured = /^@@ /m.test(text) || (lines.length > 0 && lines.every((line) => line.startsWith("+") || line.startsWith("-") || line.startsWith(" ")));
  if (!structured) return <pre className="term-body">{text}</pre>;
  return <div className="diff-body">
    {parseDiff(text).lines.map((line, index) => line.type === "hunk" || line.type === "meta"
      ? <div className="diff-meta" key={index}>{line.content}</div>
      : <div className={line.type === "add" ? "diff-add" : line.type === "del" ? "diff-del" : "diff-ctx"} key={index}>
        <span className="diff-line diff-line-old">{line.oldNum ?? ""}</span>
        <span className="diff-line diff-line-new">{line.newNum ?? ""}</span>
        <span className="diff-mark">{line.type === "add" ? "+" : line.type === "del" ? "-" : " "}</span>
        <span className="diff-text">{line.content || " "}</span>
      </div>)}
  </div>;
}

export function formatDuration(duration: number): string {
  return duration >= 1000 ? `${(duration / 1000).toFixed(1)}s` : `${duration}ms`;
}

function looksLikeTestFailure(output: string): boolean {
  return /AssertionError|Tests:\s*\d+\s*failed/i.test(output);
}

function TestFailureOutput({ text }: { text: string }) {
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

function describeRisk(flag: string): string {
  const key = `web.risk_flag.${flag}`;
  const label = t(key);
  return label === key ? flag.replace(/_/g, " ") : label;
}

type DirectoryEntry = { name?: unknown; path?: unknown; type?: unknown; size?: unknown };

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function DirectoryTree({ source }: { source: Record<string, unknown> }) {
  const tr = useT();
  const entries = source.entries as DirectoryEntry[];
  if (!entries.length) return <p className="empty-small">{tr("web.dir_empty")}</p>;
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

type SearchMatch = { path?: unknown; line?: unknown; text?: unknown };

function searchMatches(source: Record<string, unknown> | null | undefined): SearchMatch[] | null {
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

/** Search hits grouped by file, with the pattern marked in each line. */
function SearchResults({ matches, pattern, literal }: { matches: SearchMatch[]; pattern: string; literal: boolean }) {
  const tr = useT();
  if (!matches.length) return <p className="empty-small">{tr("web.steps.no_matches")}</p>;
  const files = new Map<string, SearchMatch[]>();
  for (const match of matches) {
    const path = String(match.path ?? "");
    files.set(path, [...(files.get(path) ?? []), match]);
  }
  const regex = highlighter(pattern, literal);
  return <div className="search-results">
    {[...files].map(([path, hits]) => <div className="search-file" key={path}>
      <div className="search-path">{path}</div>
      {hits.filter((hit) => typeof hit.line === "number").map((hit, index) => <div className="code-line" key={index}>
        <span className="code-num">{String(hit.line)}</span>
        <span className="code-text"><Highlighted text={textOf(hit.text)} pattern={regex} /></span>
      </div>)}
    </div>)}
  </div>;
}

/** File content read with numbered lines ("12|text"), shown with a gutter. */
function NumberedContent({ text }: { text: string }) {
  const lines = text.replace(/\n$/, "").split("\n");
  return <div className="code-view">
    {lines.map((line, index) => {
      const match = /^(\d+)\|(.*)$/.exec(line);
      return <div className="code-line" key={index}>
        <span className="code-num">{match ? match[1] : ""}</span>
        <span className="code-text">{match ? match[2] || " " : line}</span>
      </div>;
    })}
  </div>;
}

function isNumbered(text: string): boolean {
  const lines = text.split("\n").filter(Boolean);
  return lines.length > 0 && lines.every((line) => /^\d+\|/.test(line));
}

/** Renders list_directory's structured entries as a tree, one entry per line. */
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

function compactToolBody(name: string, body: string, result?: Record<string, unknown> | null): string {
  const trimmed = body.trim();
  if (/directory/i.test(name)) {
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
  if (Array.isArray(source.memories) || Array.isArray(source.results)) {
    const count = (source.memories as unknown[] | undefined)?.length ?? (source.results as unknown[] | undefined)?.length ?? 0;
    return count ? `${count} memories` : "No memories";
  }
  if (typeof source.count === "number") return `${source.count} results`;
  return body;
}

function exitCode(result: Record<string, unknown> | null | undefined, data: unknown): number | null {
  const source = result ?? (data && typeof data === "object" ? data as Record<string, unknown> : null);
  if (!source) return null;
  const value = source.returncode ?? source.exit_code ?? source.exitCode;
  return typeof value === "number" ? value : null;
}

export function toolFamily(name: string, kind?: string): "shell" | "edit" | "read" | "tool" {
  const normalized = name.toLowerCase();
  if (kind === "shell" || normalized === "execute_command" || normalized === "shell") return "shell";
  if (kind === "edit" || ["write_file", "edit_file", "search_replace", "apply_patch", "strreplace"].includes(normalized)) return "edit";
  if (kind === "tool" && (normalized.startsWith("read") || normalized.includes("directory") || normalized === "grep")) return "read";
  if (normalized.startsWith("read") || normalized.includes("directory")) return "read";
  return kind === "edit" ? "edit" : kind === "shell" ? "shell" : "tool";
}

export type StepCategory = "directories" | "reads" | "searches" | "web" | "commands" | "edits" | "memory" | "plans" | "tools";

/** What kind of work a tool call does, for its colour and the group summary. */
export function toolCategory(name: string, kind?: string): StepCategory {
  const family = toolFamily(name, kind);
  if (family === "shell") return "commands";
  if (family === "edit") return "edits";
  const lowered = name.toLowerCase();
  if (lowered.startsWith("web_") || lowered.startsWith("http")) return "web";
  // Memory and knowledge lookups are recall, not code search, even when named "search_*".
  if (/memory|episode|knowledge/.test(lowered)) return "memory";
  if (lowered.includes("plan")) return "plans";
  if (lowered.includes("directory")) return "directories";
  if (/grep|glob|search/.test(lowered)) return "searches";
  if (lowered.startsWith("read")) return "reads";
  return "tools";
}

function toolIcon(name: string, category: StepCategory): PhosphorIcon {
  const lowered = name.toLowerCase();
  switch (category) {
    case "commands": return TerminalWindow;
    case "edits": return PencilSimple;
    case "web": return Globe;
    case "memory": return lowered.includes("knowledge") ? BookOpen : Brain;
    case "plans": return ListChecks;
    case "directories": return FolderSimple;
    case "searches": return MagnifyingGlass;
    case "reads": return FileText;
    default:
      if (lowered === "ask_user") return ChatCircleDots;
      if (lowered.includes("loop")) return ArrowsClockwise;
      return Wrench;
  }
}

export function UserMessage({ text, userLabel, timestamp, tocId, enter }: { text: string; userLabel?: string; timestamp?: string; tocId?: string; enter?: boolean }) {
  const tr = useT();
  const parts = text.split(/(@(?:[\w.-]+\/)*[\w.-]+)/g);
  return <div className={`msg-row user-msg${enter ? " enter" : ""}`} data-toc-id={tocId}>
    <div className="msg-card">
      <div className="msg-card-head">
        <span><strong>{userLabel || tr("web.you")}</strong> <span className="muted">{tr("web.user_instruction")}</span></span>
        {timestamp && <span className="mono muted">{timestamp}</span>}
      </div>
      <p className="msg-body">{parts.map((part, index) => part.startsWith("@") ? <span className="prompt-reference" key={`${part}-${index}`}>{part}</span> : part)}</p>
    </div>
  </div>;
}

export function AssistantMessage({ text, enter }: { text: string; enter?: boolean }) {
  return <div className={`msg-row assistant-msg${enter ? " enter" : ""}`}>
    <div className="msg-card">
      <div className="msg-body"><MarkdownContent content={text} /></div>
    </div>
  </div>;
}

export function SummaryRow({ text, durationMs, enter }: { text: string; durationMs?: number | null; enter?: boolean }) {
  const [open, setOpen] = useState(false);
  const tr = useT();
  const durationLabel = typeof durationMs === "number" ? formatDuration(durationMs) : "";
  const preview = text.replace(/\s+/g, " ").trim();
  return <div className={`tl-indent${enter ? " enter" : ""}`}>
    <section className={`tool-card reasoning-card ${open ? "open" : ""}`}>
      <button
        type="button"
        className="tool-card-head"
        aria-expanded={open}
        aria-label={open ? tr("web.reasoning_collapse") : tr("web.reasoning_expand")}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="tool-title">
          <span className="step-icon reasoning" aria-hidden="true"><Lightbulb size={12} /></span>
          <span className="reasoning-snippet">{open ? tr("web.steps.reasoning") : preview}</span>
        </span>
        <span className="tool-meta">
          {durationLabel && <span className="step-meta">{durationLabel}</span>}
          <CaretDown size={12} className={open ? "" : "collapsed"} aria-hidden="true" />
        </span>
      </button>
      {open && preview && <div className="tool-card-body"><div className="reasoning-body">{text}</div></div>}
    </section>
  </div>;
}

export function ReasoningRow({ text, durationMs, enter }: { text: string; durationMs?: number | null; enter?: boolean }) {
  return <SummaryRow text={text} durationMs={durationMs} enter={enter} />;
}

/** Number of entries a list_directory result reports, from its data. */
function directoryEntryCount(result?: Record<string, unknown> | null, data?: unknown): number {
  const candidates = [data, result?.data, result];
  for (const candidate of candidates) {
    if (candidate && typeof candidate === "object" && Array.isArray((candidate as { entries?: unknown }).entries)) {
      return ((candidate as { entries: unknown[] }).entries).length;
    }
  }
  return 0;
}

type SandboxDenial = { path: string; grant_directory: string; grant_write_directory?: string; readable: string[]; writable: string[] };

function SandboxDenialHint({ denial, sessionId }: { denial: SandboxDenial; sessionId?: string }) {
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

export function ToolCard({
  name, phase, args, result, data, duration, output, kind, sessionId, enter,
}: {
  enter?: boolean;
  sessionId?: string;
  name: string;
  phase?: string;
  args?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  data?: unknown;
  duration?: number | null;
  output?: string;
  kind?: string;
}) {
  const tr = useT();
  const family = toolFamily(name, kind);
  const [toggled, setToggled] = useState<boolean | null>(null);
  const command = arg(args, ["command", "cmd"]);
  const path = arg(args, ["path", "file", "file_path", "target", "directory"]);
  const pattern = family === "shell" || family === "edit" ? "" : arg(args, ["pattern", "query", "regex"]);
  const code = family === "shell" ? exitCode(result, data) : null;
  const raw = output || (result ? (typeof result.output === "string" ? result.output : JSON.stringify(result, null, 2)) : "");
  const metricSource = data && typeof data === "object" ? data as Record<string, unknown> : result;
  const denied = phase === "failed" && (metricSource?.permission as { decision?: unknown } | undefined)?.decision === "deny";
  // Derived until the person toggles it, so a live card and a reloaded one agree.
  const open = toggled ?? (!denied && (phase === "failed" || phase === "running" || family === "edit"));
  const hasDiff = family === "edit" && typeof metricSource?.diff === "string";
  const body = hasDiff ? metricSource.diff as string : compactToolBody(name, raw, result ?? (data && typeof data === "object" ? data as Record<string, unknown> : null));
  const lowered = name.toLowerCase();
  const category = toolCategory(name, kind);
  const isDirectory = category === "directories";
  const isSearch = category === "searches";
  const matches = isSearch ? searchMatches(metricSource) : null;
  const directory = isDirectory && Array.isArray(metricSource?.entries) ? metricSource : null;
  const content = !isDirectory && !isSearch && family !== "shell" && family !== "edit" && typeof metricSource?.content === "string" ? metricSource.content : null;
  const failed = phase === "failed" || (code !== null && code !== 0);
  const Icon = failed && !denied ? X : toolIcon(name, category);
  const bodyLines = body.split("\n").filter(Boolean);
  const added = family === "edit" && typeof metricSource?.additions === "number" ? metricSource.additions : family === "edit" ? bodyLines.filter((line) => line.startsWith("+")).length : 0;
  const deleted = family === "edit" && typeof metricSource?.deletions === "number" ? metricSource.deletions : family === "edit" ? bodyLines.filter((line) => line.startsWith("-")).length : 0;
  const directoryCount = family === "read" && /directory/i.test(name)
    ? directoryEntryCount(result, data) || (typeof result?.output === "string" && !result.output.trim().startsWith("{") ? bodyLines.length : 0)
    : 0;
  const testFailed = family === "shell" && code !== null && code !== 0 && looksLikeTestFailure(body);
  const durationLabel = typeof duration === "number" ? formatDuration(duration) : "";
  return <div className={`tl-indent${enter ? " enter" : ""}`}>
    <section className={`tool-card ${open ? "open" : ""}`}>
      <button type="button" className="tool-card-head" aria-expanded={open} onClick={() => setToggled(!open)}>
        <span className="tool-title">
          <span className={`step-icon ${category}${failed && !denied ? " failed" : ""}`} aria-hidden="true"><Icon size={13} weight={failed && !denied ? "bold" : "regular"} /></span>
          {family === "shell" ? <span className="mono command">{command || name}</span> : <>
            {family !== "edit" && <span className="tool-name">{name}</span>}
            {pattern && <span className="mono tool-pattern" title={pattern}>{pattern}</span>}
            {path && <span className={`mono path ${family === "edit" ? "path-strong" : ""}`}>{path}</span>}
            {family === "edit" && phase !== "failed" && (phase === "succeeded" || added + deleted > 0) && <><span className="edit-add">+{added}</span><span className="edit-del">−{deleted}</span></>}
          </>}
        </span>
        <span className="tool-meta">
          {phase === "failed" && code === null && <span className={denied ? "denied-pill" : "fail-pill"}>{tr(denied ? "web.tool_denied" : "web.status.failed")}</span>}
          {directoryCount > 0 && phase === "succeeded" && <span className="step-meta">{tr("web.n_files", { count: directoryCount })}{durationLabel ? ` (${durationLabel})` : ""}</span>}
          {matches && phase === "succeeded" && <span className="step-meta">{tr("web.steps.match_count", { count: matches.length, files: new Set(matches.map((match) => String(match.path ?? ""))).size })}</span>}
          {family === "edit" && phase === "succeeded" && <span className="muted edit-summary">{t("web.applied")}</span>}
          {code !== null && code !== 0 && <span className="fail-pill">Exit {code}{testFailed ? ` (${tr("web.bug_confirmed")})` : ""}</span>}
          {durationLabel && directoryCount === 0 && <span className="step-meta">{durationLabel}</span>}
          <CaretDown size={12} className={open ? "" : "collapsed"} />
        </span>
      </button>
      {open && <div className={`tool-card-body${failed && !denied ? " failed" : ""}`}>
        {family === "shell" && command && <div className="term-cmd">$ {command}</div>}
        {directory ? <DirectoryTree source={directory} />
          : matches && phase === "succeeded" ? <SearchResults matches={matches} pattern={pattern} literal={metricSource?.fixed_string === true || args?.fixed_string === true} />
          : content !== null && isNumbered(content) ? <NumberedContent text={content} />
          : body && (family === "edit" ? <DiffBody text={body} /> : testFailed ? <TestFailureOutput text={body} /> : isNumbered(body) ? <NumberedContent text={body} /> : <pre className={`term-body ${code !== null && code !== 0 ? "failed" : ""}`}>{body}</pre>)}
        {Boolean(metricSource?.sandbox_denial) && <SandboxDenialHint denial={metricSource!.sandbox_denial as SandboxDenial} sessionId={sessionId} />}
        {hasDiff && !body && <p className="empty-small">{tr("web.no_diff")}</p>}
        {hasDiff && metricSource?.diff_truncated === true && <p className="hint" role="status">{tr("web.diff_truncated")}</p>}
      </div>}
    </section>
  </div>;
}

function toolOutput(tool: ToolState): string {
  const data = tool.data;
  const record = data && typeof data === "object" ? data as Record<string, unknown> : null;
  const structuredOutput = record ? [record.stdout, record.stderr].filter((value) => typeof value === "string" && value).join("\n") || textOf(record.output) : "";
  // Without command output the error already says what happened; its envelope is diagnostics.
  if (tool.err && !structuredOutput) return tool.err;
  const body = structuredOutput || tool.output || (typeof data === "string" ? data : data == null ? "" : JSON.stringify(data, null, 2));
  return [tool.err, body].filter(Boolean).join("\n");
}

export function toolCardFromTimeline(item: TimelineItem, sessionId?: string, enter?: boolean) {
  const result = item.result;
  const envelope = result && ("ok" in result || "data" in result || "err" in result);
  const tool: ToolState = {
    call_id: item.call_id || item.id, name: item.name || item.kind,
    arguments: item.arguments, output: item.output,
    data: envelope ? result.data : result,
    err: envelope ? textOf(result.err) : undefined,
  };
  const data = tool.data && typeof tool.data === "object" ? tool.data as Record<string, unknown> : null;
  return <ToolCard key={item.id} name={tool.name} phase={item.phase} args={tool.arguments} result={data} duration={item.duration_ms} output={toolOutput(tool)} kind={item.kind} sessionId={sessionId} enter={enter} />;
}

export function toolCardFromState(tool: ToolState, sessionId?: string, enter?: boolean) {
  return <ToolCard key={tool.call_id} name={tool.name} phase={tool.phase} args={tool.arguments} data={tool.data} output={toolOutput(tool)} sessionId={sessionId} enter={enter} />;
}

export function InteractionCard({ sessionId, interaction, respond, root }: {
  sessionId: string;
  interaction: Interaction;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
  root?: string;
}) {
  if (interaction.kind === "permission") {
    return <PermissionApproval sessionId={sessionId} interaction={interaction} respond={respond} root={root} />;
  }
  return <QuestionCard interaction={interaction} respond={respond} />;
}

function QuestionCard({ interaction, respond }: {
  interaction: Interaction;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
}) {
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState("");
  const tr = useT();
  return <section className="interaction-card ask-card" role="alert">
      <div className="perm-head"><strong>{tr("web.needs_input")}</strong></div>
      <form className="perm-body ask-form" onSubmit={(event) => {
        event.preventDefault();
        if (!answer.trim() || submitting) return;
        setSubmitting("answer");
        Promise.resolve(respond(interaction.request_id, answer.trim())).then((accepted) => { if (accepted === false) setSubmitting(""); });
      }}>
        <p>{interaction.question}</p>
        {interaction.context && <p className="hint">{interaction.context}</p>}
        {interaction.options?.map((option) => <button type="button" className="option-button" key={option} disabled={Boolean(submitting)} onClick={() => {
          if (submitting) return;
          setSubmitting(option);
          Promise.resolve(respond(interaction.request_id, option)).then((accepted) => { if (accepted === false) setSubmitting(""); });
        }}>{option}</button>)}
        <input aria-label={tr("web.answer")} value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder={tr("web.answer_placeholder")} />
        <button className="button primary" aria-label={tr("web.submit_answer")} disabled={Boolean(submitting)}>{tr("web.submit_answer")}</button>
      </form>
    </section>;
}
