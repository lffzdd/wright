import { CaretDown, Check, User, X } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
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

function formatDuration(duration: number): string {
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

function directoryTree(text: string): string {
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  if (lines.length === 0) return text;
  if (lines.some((line) => line.startsWith("├") || line.startsWith("└") || line.startsWith("//"))) return text;
  return lines.map((line, index) => `${index === lines.length - 1 ? "└──" : "├──"} ${line}`).join("\n");
}

function compactToolBody(name: string, body: string, result?: Record<string, unknown> | null): string {
  const trimmed = body.trim();
  if (/directory/i.test(name)) return directoryTree(trimmed);
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

export function UserMessage({ text, userLabel, timestamp, tocId }: { text: string; userLabel?: string; timestamp?: string; tocId?: string }) {
  const tr = useT();
  const parts = text.split(/(@(?:[\w.-]+\/)*[\w.-]+)/g);
  const initials = userLabel ? userLabel.split(/\s+/).map((part) => part[0]).join("").slice(0, 2) : "";
  return <div className="msg-row user-msg" data-toc-id={tocId}>
    <div className="msg-card">
      <div className="msg-card-head">
        <span><strong>{userLabel || tr("web.you")}</strong> <span className="muted">{tr("web.user_instruction")}</span></span>
        {timestamp && <span className="mono muted">{timestamp}</span>}
      </div>
      <p className="msg-body">{parts.map((part, index) => part.startsWith("@") ? <span className="prompt-reference" key={`${part}-${index}`}>{part}</span> : part)}</p>
    </div>
    <div className="avatar" aria-hidden="true">{initials || <User size={11} weight="bold" />}</div>
  </div>;
}

export function AssistantMessage({ text }: { text: string }) {
  const tr = useT();
  return <div className="msg-row assistant-msg">
    <div className="avatar assistant" aria-hidden="true">W</div>
    <div className="msg-card">
      <div className="msg-card-head">
        <span><strong>Wright</strong> <span className="muted">{tr("web.assistant_reply")}</span></span>
      </div>
      <div className="msg-body"><MarkdownContent content={text} /></div>
    </div>
  </div>;
}

export function SummaryRow({ text, durationMs }: { text: string; durationMs?: number | null }) {
  const [open, setOpen] = useState(false);
  const tr = useT();
  const durationLabel = typeof durationMs === "number" ? formatDuration(durationMs) : "";
  const preview = text.replace(/\s+/g, " ").trim();
  return <div className="tl-indent">
    <section className={`tool-card reasoning-card ${open ? "open" : ""}`}>
      <button
        type="button"
        className="tool-card-head"
        aria-expanded={open}
        aria-label={open ? tr("web.reasoning_collapse") : tr("web.reasoning_expand")}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="tool-title">
          <span className="tool-badge reasoning">REASON</span>
          <span className="tool-name">{tr("web.reasoning")}{durationLabel ? ` · ${durationLabel}` : ""}</span>
          {!open && preview && <span className="reasoning-snippet">{preview}</span>}
        </span>
        <span className="tool-meta">
          <CaretDown size={12} className={open ? "" : "collapsed"} aria-hidden="true" />
        </span>
      </button>
      {open && preview && <pre className="term-body">{text}</pre>}
    </section>
  </div>;
}

export function ReasoningRow({ text, durationMs }: { text: string; durationMs?: number | null }) {
  return <SummaryRow text={text} durationMs={durationMs} />;
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

type SandboxDenial = { path: string; grant_directory: string; readable: string[]; writable: string[] };

function SandboxDenialHint({ denial, sessionId }: { denial: SandboxDenial; sessionId?: string }) {
  const tr = useT();
  const [state, setState] = useState<"idle" | "busy" | "granted" | "failed">("idle");
  const directory = denial.grant_directory;
  const grant = () => {
    if (!sessionId) return;
    setState("busy");
    api.grants(sessionId)
      .then((grants) => api.addGrant(sessionId, { kind: "directory", path: directory, operations: ["file_read"] }, "session", grants.version))
      .then(() => setState("granted"), () => setState("failed"));
  };
  return <div className="sandbox-denial" role="status">
    <p>{tr(denial.path ? "web.sandbox.denied_path" : "web.sandbox.denied", { path: denial.path })}</p>
    <p className="mono">{tr("web.sandbox.scope_read", { paths: denial.readable.join(", ") })}</p>
    {directory && sessionId && state !== "granted" && <button type="button" className="button" disabled={state === "busy"} onClick={grant}>
      {tr(state === "busy" ? "web.submitting" : "web.sandbox.grant_read", { path: directory })}
    </button>}
    {state === "granted" && <p>{tr("web.sandbox.granted", { path: directory })}</p>}
    {state === "failed" && <p className="form-error" role="alert">{tr("web.sandbox.grant_failed")}</p>}
    <p className="hint">{tr("web.sandbox.denied_host")}</p>
  </div>;
}

export function ToolCard({
  name, phase, args, result, data, duration, output, kind, sessionId,
}: {
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
  const path = arg(args, ["path", "file", "file_path", "target"]);
  const code = family === "shell" ? exitCode(result, data) : null;
  const raw = output || (result ? (typeof result.output === "string" ? result.output : JSON.stringify(result, null, 2)) : "");
  const metricSource = data && typeof data === "object" ? data as Record<string, unknown> : result;
  const denied = phase === "failed" && (metricSource?.permission as { decision?: unknown } | undefined)?.decision === "deny";
  // Derived until the person toggles it, so a live card and a reloaded one agree.
  const open = toggled ?? (!denied && (phase === "failed" || phase === "running" || family === "edit"));
  const hasDiff = family === "edit" && typeof metricSource?.diff === "string";
  const body = hasDiff ? metricSource.diff as string : compactToolBody(name, raw, result);
  const badge = family === "shell" ? "SHELL" : family === "edit" ? "FILE EDIT" : family === "read" ? "TOOL" : "TOOL";
  const bodyLines = body.split("\n").filter(Boolean);
  const added = family === "edit" && typeof metricSource?.additions === "number" ? metricSource.additions : family === "edit" ? bodyLines.filter((line) => line.startsWith("+")).length : 0;
  const deleted = family === "edit" && typeof metricSource?.deletions === "number" ? metricSource.deletions : family === "edit" ? bodyLines.filter((line) => line.startsWith("-")).length : 0;
  const directoryCount = family === "read" && /directory/i.test(name)
    ? directoryEntryCount(result, data) || (typeof result?.output === "string" && !result.output.trim().startsWith("{") ? bodyLines.length : 0)
    : 0;
  const testFailed = family === "shell" && code !== null && code !== 0 && looksLikeTestFailure(body);
  const durationLabel = typeof duration === "number" ? formatDuration(duration) : "";
  return <div className="tl-indent">
    <section className="tool-card">
      <button type="button" className="tool-card-head" onClick={() => setToggled(!open)}>
        <span className="tool-title">
          <span className={`tool-badge ${family}`}>{badge}</span>
          {family === "shell" ? <span className="mono command">{command ? `$ ${command}` : name}</span> : <>
            {family !== "edit" && <span className="tool-name">{name}</span>}
            {path && <span className={`mono path ${family === "edit" ? "path-strong" : ""}`}>{path}</span>}
            {family === "edit" && phase !== "failed" && (phase === "succeeded" || added + deleted > 0) && <><span className="edit-add">+{added}</span><span className="edit-del">−{deleted}</span></>}
          </>}
        </span>
        <span className="tool-meta">
          {phase === "failed" && code === null && <span className={denied ? "denied-pill" : "fail-pill"}>{tr(denied ? "web.tool_denied" : "web.status.failed")}</span>}
          {family === "read" && phase === "succeeded" && <span className="read-success"><Check size={12} weight="bold" />{directoryCount > 0 ? tr("web.n_files", { count: directoryCount }) : phase}{directoryCount > 0 && durationLabel ? ` (${durationLabel})` : ""}</span>}
          {family === "edit" && phase === "succeeded" && <span className="muted edit-summary">{t("web.applied")}</span>}
          {code !== null && <span className={code === 0 ? "ok-pill" : "fail-pill"}>Exit {code}{testFailed ? ` (${tr("web.bug_confirmed")})` : ""}</span>}
          {durationLabel && directoryCount === 0 && <span className="mono muted">{durationLabel}</span>}
          <CaretDown size={12} className={open ? "" : "collapsed"} />
        </span>
      </button>
      {open && body && (family === "edit" ? <DiffBody text={body} /> : testFailed ? <TestFailureOutput text={body} /> : <pre className={`term-body ${code !== null && code !== 0 ? "failed" : ""}`}>{body}</pre>)}
      {open && Boolean(metricSource?.sandbox_denial) && <SandboxDenialHint denial={metricSource!.sandbox_denial as SandboxDenial} sessionId={sessionId} />}
      {open && hasDiff && !body && <p className="empty-small">{tr("web.no_diff")}</p>}
      {open && hasDiff && metricSource?.diff_truncated === true && <p className="hint" role="status">{tr("web.diff_truncated")}</p>}
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

export function toolCardFromTimeline(item: TimelineItem, sessionId?: string) {
  const result = item.result;
  const envelope = result && ("ok" in result || "data" in result || "err" in result);
  const tool: ToolState = {
    call_id: item.call_id || item.id, name: item.name || item.kind,
    arguments: item.arguments, output: item.output,
    data: envelope ? result.data : result,
    err: envelope ? textOf(result.err) : undefined,
  };
  const data = tool.data && typeof tool.data === "object" ? tool.data as Record<string, unknown> : null;
  return <ToolCard key={item.id} name={tool.name} phase={item.phase} args={tool.arguments} result={data} duration={item.duration_ms} output={toolOutput(tool)} kind={item.kind} sessionId={sessionId} />;
}

export function toolCardFromState(tool: ToolState, sessionId?: string) {
  return <ToolCard key={tool.call_id} name={tool.name} phase={tool.phase} args={tool.arguments} data={tool.data} output={toolOutput(tool)} sessionId={sessionId} />;
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
