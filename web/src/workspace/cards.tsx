import { CaretDown, Check, User, X } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { MarkdownContent } from "../Markdown";
import { parseDiff } from "../diff";
import { present, t, useT } from "../i18n";
import type { Interaction, TimelineItem, ToolState } from "../types";
import { useShellReadiness } from "./permissions";

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

export function UserMessage({ text, userLabel, timestamp }: { text: string; userLabel?: string; timestamp?: string }) {
  const tr = useT();
  const parts = text.split(/(@(?:[\w.-]+\/)*[\w.-]+)/g);
  const initials = userLabel ? userLabel.split(/\s+/).map((part) => part[0]).join("").slice(0, 2) : "";
  return <div className="msg-row">
    <div className="avatar" aria-hidden="true">{initials || <User size={11} weight="bold" />}</div>
    <div className="msg-card">
      <div className="msg-card-head">
        <span><strong>{userLabel || tr("web.you")}</strong> <span className="muted">{tr("web.user_instruction")}</span></span>
        {timestamp && <span className="mono muted">{timestamp}</span>}
      </div>
      <p className="msg-body">{parts.map((part, index) => part.startsWith("@") ? <span className="prompt-reference" key={`${part}-${index}`}>{part}</span> : part)}</p>
    </div>
  </div>;
}

export function AssistantMessage({ text }: { text: string }) {
  const tr = useT();
  return <div className="msg-row">
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
  return <div className={`tl-indent reasoning-block ${open ? "open" : ""}`}>
    <div className="summary-row">
      <div className="summary-copy">
        <svg className="summary-mark" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M13 10V3L4 14h7v7l9-11h-7z" />
        </svg>
        <span className="reasoning-label">{tr("web.reasoning")}{typeof durationMs === "number" ? ` (${formatDuration(durationMs)})` : ""}:</span>
        {!open && <strong>{text}</strong>}
      </div>
      <button type="button" className="text-button summary-toggle" onClick={() => setOpen((value) => !value)}>{open ? tr("web.hide") : tr("web.steps")}<CaretDown size={12} className={open ? "" : "collapsed"} /></button>
    </div>
    {open && <p className="summary-body">{text}</p>}
  </div>;
}

export function ReasoningRow({ text, durationMs }: { text: string; durationMs?: number | null }) {
  return <SummaryRow text={text} durationMs={durationMs} />;
}

export function ToolCard({
  name, phase, args, result, data, duration, output, kind,
}: {
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
  const [open, setOpen] = useState(phase === "failed" || phase === "running" || family === "edit");
  const command = arg(args, ["command", "cmd"]);
  const path = arg(args, ["path", "file", "file_path", "target"]);
  const code = family === "shell" ? exitCode(result, data) : null;
  const raw = output || (result ? (typeof result.output === "string" ? result.output : JSON.stringify(result, null, 2)) : "");
  const metricSource = data && typeof data === "object" ? data as Record<string, unknown> : result;
  const hasDiff = family === "edit" && typeof metricSource?.diff === "string";
  const body = hasDiff ? metricSource.diff as string : compactToolBody(name, raw, result);
  const badge = family === "shell" ? "SHELL" : family === "edit" ? "FILE EDIT" : family === "read" ? "TOOL" : "TOOL";
  const bodyLines = body.split("\n").filter(Boolean);
  const added = family === "edit" && typeof metricSource?.additions === "number" ? metricSource.additions : family === "edit" ? bodyLines.filter((line) => line.startsWith("+")).length : 0;
  const deleted = family === "edit" && typeof metricSource?.deletions === "number" ? metricSource.deletions : family === "edit" ? bodyLines.filter((line) => line.startsWith("-")).length : 0;
  const directoryCount = family === "read" && /directory/i.test(name) ? bodyLines.length : 0;
  const testFailed = family === "shell" && code !== null && code !== 0 && looksLikeTestFailure(body);
  const durationLabel = typeof duration === "number" ? formatDuration(duration) : "";
  return <div className="tl-indent">
    <section className="tool-card">
      <button type="button" className="tool-card-head" onClick={() => setOpen((value) => !value)}>
        <span className="tool-title">
          <span className={`tool-badge ${family}`}>{badge}</span>
          {family === "shell" ? <span className="mono command">{command ? `$ ${command}` : name}</span> : <>
            {family !== "edit" && <span className="tool-name">{name}</span>}
            {path && <span className={`mono path ${family === "edit" ? "path-strong" : ""}`}>{path}</span>}
            {family === "edit" && <><span className="edit-add">+{added}</span><span className="edit-del">−{deleted}</span></>}
          </>}
        </span>
        <span className="tool-meta">
          {family === "read" && phase === "succeeded" && <span className="read-success"><Check size={12} weight="bold" />{directoryCount > 0 ? tr("web.n_files", { count: directoryCount }) : phase}{directoryCount > 0 && durationLabel ? ` (${durationLabel})` : ""}</span>}
          {family === "edit" && phase === "succeeded" && <span className="muted edit-summary">{t("web.applied")}</span>}
          {code !== null && <span className={code === 0 ? "ok-pill" : "fail-pill"}>Exit {code}{testFailed ? ` (${tr("web.bug_confirmed")})` : ""}</span>}
          {durationLabel && directoryCount === 0 && <span className="mono muted">{durationLabel}</span>}
          <CaretDown size={12} className={open ? "" : "collapsed"} />
        </span>
      </button>
      {open && body && (family === "edit" ? <DiffBody text={body} /> : testFailed ? <TestFailureOutput text={body} /> : <pre className={`term-body ${code !== null && code !== 0 ? "failed" : ""}`}>{body}</pre>)}
      {open && hasDiff && !body && <p className="empty-small">{tr("web.no_diff")}</p>}
      {open && hasDiff && metricSource?.diff_truncated === true && <p className="hint" role="status">{tr("web.diff_truncated")}</p>}
    </section>
  </div>;
}

function toolOutput(tool: ToolState): string {
  const data = tool.data;
  const record = data && typeof data === "object" ? data as Record<string, unknown> : null;
  const structuredOutput = record ? [record.stdout, record.stderr].filter((value) => typeof value === "string" && value).join("\n") || textOf(record.output) : "";
  const body = structuredOutput || tool.output || (typeof data === "string" ? data : data == null ? "" : JSON.stringify(data, null, 2));
  return [tool.err, body].filter(Boolean).join("\n");
}

export function toolCardFromTimeline(item: TimelineItem) {
  const result = item.result;
  const envelope = result && ("ok" in result || "data" in result || "err" in result);
  const tool: ToolState = {
    call_id: item.call_id || item.id, name: item.name || item.kind,
    arguments: item.arguments, output: item.output,
    data: envelope ? result.data : result,
    err: envelope ? textOf(result.err) : undefined,
  };
  const data = tool.data && typeof tool.data === "object" ? tool.data as Record<string, unknown> : null;
  return <ToolCard key={item.id} name={tool.name} phase={item.phase} args={tool.arguments} result={data} duration={item.duration_ms} output={toolOutput(tool)} kind={item.kind} />;
}

export function toolCardFromState(tool: ToolState) {
  return <ToolCard key={tool.call_id} name={tool.name} phase={tool.phase} args={tool.arguments} data={tool.data} output={toolOutput(tool)} />;
}

export function InteractionCard({ sessionId, interaction, respond }: {
  sessionId: string;
  interaction: Interaction;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
}) {
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState("");
  const [error, setError] = useState("");
  const [kept, setKept] = useState("");
  const [more, setMore] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [rememberId, setRememberId] = useState("");
  const [network, setNetwork] = useState(false);
  const sending = useRef(false);
  const moreRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (more) moreRef.current?.scrollIntoView?.({ block: "nearest" }); }, [more]);
  const tr = useT();
  const isPermission = interaction.kind === "permission";
  const reason = present(interaction.reason_code, interaction.reason_params, interaction.reason ?? "");
  const grant = present(interaction.summary_code, interaction.summary_params, interaction.grant_summary ?? "");
  const isShell = interaction.tool_name === "execute_command";
  const isHttp = Boolean(interaction.http_target);
  const isFile = interaction.operation?.includes("file_") || ["write_file", "edit_file", "read_file"].includes(interaction.tool_name || "");
  const action = isShell ? "shell" : isHttp ? "http" : isFile ? interaction.operation?.includes("file_write") || ["write_file", "edit_file"].includes(interaction.tool_name || "") ? "file_write" : "file_read" : "tool";
  const target = isShell ? interaction.command || "" : isHttp ? `${interaction.http_method} ${interaction.http_target}` : (interaction.targets?.length ? [...new Set(interaction.targets)].join("\n") : interaction.subject || interaction.tool_name || "");
  const shell = useShellReadiness(sessionId, isPermission && isShell);
  const shellBlocked = isShell && shell.status?.available !== true;
  const risks = interaction.risk_flags ?? [];
  const choices = interaction.choices ?? [];
  const denies = choices.filter((choice) => choice.id === "deny");
  const allows = choices.filter((choice) => choice.id !== "deny");
  const visible = allows.filter((choice) => choice.id === "allow_once");
  const extra = allows.filter((choice) => choice.id !== "allow_once" && choice.id !== "allow_once_network");
  const remembered = extra.find((choice) => choice.id === rememberId) || extra.find((choice) => choice.lifetime === "project") || extra[0];
  const offered = extra.filter((choice) => advanced || choice.lifetime !== "user");
  const scopeKey = (choice: typeof remembered) => choice ? `${choice.resource_kind}:${choice.scope}` : "";
  const scopes = offered.filter((choice, index, items) => items.findIndex((item) => scopeKey(item) === scopeKey(choice)) === index);
  const capabilities = offered.filter((choice) => scopeKey(choice) === scopeKey(remembered)).filter((choice, index, items) => items.findIndex((item) => item.operations?.join() === choice.operations?.join()) === index);
  const lifetimes = offered.filter((choice) => scopeKey(choice) === scopeKey(remembered) && choice.operations?.join() === remembered?.operations?.join());
  const networkChoice = allows.find((choice) => choice.id === "allow_once_network");
  const hasGlobalChoices = extra.some((choice) => choice.lifetime === "user");
  const setOption = (field: "scope" | "operations" | "lifetime", value: string) => {
    const options = offered.filter((choice) => field === "scope" ? scopeKey(choice) === value : field === "operations" ? scopeKey(choice) === scopeKey(remembered) && choice.operations?.join() === value : scopeKey(choice) === scopeKey(remembered) && choice.operations?.join() === remembered?.operations?.join() && choice.lifetime === value);
    const candidate = options.find((choice) => choice.lifetime === remembered?.lifetime) || options.find((choice) => choice.lifetime === "project") || options[0];
    if (candidate) setRememberId(candidate.id);
  };
  const riskLevel = tr(`web.risk_level.${interaction.risk_level || "review"}`);
  const riskNote = risks.length ? risks.map(describeRisk).join(" · ") : tr("web.risk_unspecified");
  const scopeFallback = visible[0] ? tr("permission.scope.once") : "";
  const scopeDetail = risks.includes("recursive_delete")
    ? t("web.scope_note.recursive_delete")
    : scopeFallback && scopeFallback !== grant ? scopeFallback : "";
  const choose = (choiceId: string) => {
    if (sending.current || (shellBlocked && choiceId !== "deny")) return;
    sending.current = true;
    setSubmitting(choiceId);
    setError("");
    setKept(choiceId);
    Promise.resolve(respond(interaction.request_id, choiceId)).then((accepted) => {
      if (accepted === false) {
        sending.current = false;
        setSubmitting("");
        setError(t("web.decision_failed"));
      }
    }).catch(() => {
      sending.current = false;
      setSubmitting("");
      setError(t("web.decision_failed"));
    });
  };
  const choiceButton = (choice: { id: string; label: string }, tone: "primary" | "session" | "deny") => (
    <button key={choice.id} type="button" className={`perm-choice ${tone}`} disabled={Boolean(submitting) || (shellBlocked && tone !== "deny")} onClick={() => choose(choice.id === "allow_once" && network && networkChoice ? networkChoice.id : choice.id)}>
      {tone === "primary" && <Check size={13} weight="bold" />}{tone === "deny" && <X size={12} weight="bold" />}
      {submitting === choice.id || (choice.id === "allow_once" && submitting === networkChoice?.id) ? tr("web.submitting") : choice.id === "allow_once" ? tr(network ? "web.allow_once_network" : "web.allow_once") : choice.id === "deny" ? tr("permission.choice.deny") : choice.label}
      {tone === "primary" && <kbd>{/Mac/i.test(navigator.platform) ? "⌥↵" : "Alt+Enter"}</kbd>}{tone === "deny" && <kbd>Esc</kbd>}
    </button>
  );
  if (!isPermission) {
    return <section className="interaction-card ask-card" role="alert">
      <div className="perm-head"><strong>{tr("web.needs_input")}</strong></div>
      <form className="perm-body ask-form" onSubmit={(event) => {
        event.preventDefault();
        if (!answer.trim() || submitting) return;
        setSubmitting("answer");
        Promise.resolve(respond(interaction.request_id, answer.trim())).then((accepted) => { if (accepted === false) setSubmitting(""); });
      }}>
        <p>{interaction.question}</p>
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
  return <div className="tl-indent">
    <section className="interaction-card" role="alert" tabIndex={0} onKeyDown={(event) => {
      if (event.target instanceof HTMLElement && event.target.closest("input,select,textarea")) return;
      if (event.key === "Escape" && denies.length) { event.preventDefault(); choose(denies[0].id); }
      if (event.altKey && event.key === "Enter" && visible.length) { event.preventDefault(); choose(network && networkChoice ? networkChoice.id : visible[0].id); }
    }}>
      <div className="perm-head">
        <span className="perm-title"><span className="perm-icon"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg></span><strong>{tr("web.permission_required")}</strong><span className="perm-blocked">{tr("web.action_blocked")}</span></span>
        <span className="perm-paused">{tr("web.agent_paused")}<span className="dot pulse" /></span>
      </div>
      <div className="perm-body">
        <div>
          <div className="perm-kicker">{tr(`web.permission_action.${action}`)}</div>
          <div className="perm-command">{isShell && <span className="muted">$</span>}<span className="perm-target">{target}</span><button type="button" className="copy-command" aria-label={tr("web.copy_target")} onClick={() => navigator.clipboard?.writeText(target).catch(() => undefined)}>{t("web.copy")}</button></div>
          {interaction.preview && <details className="permission-preview" open><summary>{tr("web.content_preview")}</summary><pre>{interaction.preview}</pre>{interaction.preview_truncated && <p role="status">{tr("web.preview_truncated")}</p>}</details>}
        </div>
        <div className="perm-grid">
          <div>
            <span className="perm-label">{tr("web.reason_label")}</span>
            <span className="sr-only">{reason ? tr("web.reason", { value: reason }) : tr("web.reason_missing")}</span>
            <span>{reason || tr("web.reason_missing")}</span>
          </div>
          <div>
            <span className="perm-label">{tr("web.scope_label")}</span>
            <span className="sr-only">{grant ? tr("web.grant", { value: grant }) : (visible[0]?.scope || tr("web.scope_missing"))}</span>
            <span className="mono scope-value">{grant || visible[0]?.scope || tr("web.scope_missing")}</span>
            {scopeDetail ? <small>{scopeDetail}</small> : null}
          </div>
          <div>
            <span className="perm-label">{tr("web.risk_label")}</span>
            <span className="risk-level"><i />{riskLevel}</span>
            <small>{riskNote}</small>
          </div>
        </div>
        {interaction.shell_note && <p>{present("permission.shell_note", { cwd: interaction.cwd || "" }, interaction.shell_note)}</p>}
        {isShell && <div className="shell-readiness" role="status">
          <strong>{shell.status ? tr(`web.sandbox.${shell.status.state}`) : tr(shell.checked ? "web.sandbox.unavailable" : "web.sandbox.checking")}</strong>
          {shell.status?.detail && <small>{shell.status.detail}</small>}
          {shell.error && <p className="form-error" role="alert">{shell.error}</p>}
          {shellBlocked && <p>{tr("web.sandbox.approval_hint")}</p>}
          {shellBlocked && shell.status?.platform === "win32" && shell.status.state !== "initializing" && <button type="button" className="button" disabled={Boolean(shell.busy) || Boolean(submitting)} onClick={() => void shell.initialize()}>{tr("web.sandbox.initialize")}</button>}
          {shell.error && <button type="button" className="text-button" disabled={Boolean(shell.busy)} onClick={shell.refresh}>{tr("web.sandbox.refresh")}</button>}
        </div>}
        {networkChoice && <label className="network-capability"><input type="checkbox" checked={network} onChange={(event) => setNetwork(event.target.checked)} disabled={Boolean(submitting) || shellBlocked} />{tr("web.command_network")}</label>}
        {network && extra.length > 0 && <p className="hint" role="status">{tr("web.network_once_only")}</p>}
        {error && <p role="alert">{error}{kept ? tr("web.kept_choice", { choice: choices.find((choice) => choice.id === kept)?.lifetime ? tr(`web.lifetime.${choices.find((choice) => choice.id === kept)?.lifetime}`) : tr(kept === "deny" ? "permission.choice.deny" : "web.allow_once") }) : ""}</p>}
        <div className="interaction-actions">
          {visible.map((choice, index) => choiceButton(choice, index === 0 ? "primary" : "session"))}
          {denies.map((choice) => choiceButton(choice, "deny"))}
          {extra.length > 0 && <button type="button" className="text-button perm-more" onClick={() => setMore((value) => !value)}>{tr("web.more_grants")}</button>}
        </div>
        <small className="audit-note">{tr("web.audit_recorded")}</small>
        {more && remembered && <div ref={moreRef} className="more-grants grant-form">
          <label>{tr("web.scope_label")}<select value={scopeKey(remembered)} onChange={(event) => setOption("scope", event.target.value)}>{scopes.map((choice) => <option key={scopeKey(choice)} value={scopeKey(choice)}>{tr(`web.grant_kind.${choice.resource_kind}`)} · {choice.scope}</option>)}</select></label>
          <label>{tr("web.operations")}<select value={remembered.operations?.join() || ""} onChange={(event) => setOption("operations", event.target.value)}>{capabilities.map((choice) => <option key={choice.id} value={choice.operations?.join() || ""}>{choice.operations?.map((operation) => tr(`web.operation.${operation}`)).join(", ") || tr(`web.grant_kind.${choice.resource_kind}`)}</option>)}</select></label>
          <label>{tr("web.lifetime")}<select value={remembered.lifetime || "session"} onChange={(event) => setOption("lifetime", event.target.value)}>{lifetimes.map((choice) => <option key={choice.id} value={choice.lifetime}>{tr(`web.lifetime.${choice.lifetime}`)}</option>)}</select></label>
          {hasGlobalChoices && <button type="button" className="text-button" onClick={() => { if (advanced && remembered.lifetime === "user") setRememberId(extra.find((choice) => scopeKey(choice) === scopeKey(remembered) && choice.lifetime === "project")?.id || extra[0].id); setAdvanced(!advanced); }}>{tr("web.advanced")}</button>}
          <p className="grant-summary" aria-live="polite">{remembered.scope} · {remembered.operations?.map((operation) => tr(`web.operation.${operation}`)).join(", ")} · {tr(`web.lifetime.${remembered.lifetime}`)}</p>
          {remembered.lifetime === "user" && <small>{tr("web.cross_project")}</small>}
          <button type="button" className="button primary" disabled={Boolean(submitting) || shellBlocked || network} onClick={() => choose(remembered.id)}>{tr("web.remember_allow")}</button>
        </div>}
      </div>
    </section>
  </div>;
}
