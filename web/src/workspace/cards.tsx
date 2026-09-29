import { CaretDown, Check, Lightning, ShieldWarning, User, X } from "@phosphor-icons/react";
import { useRef, useState } from "react";
import { MarkdownContent } from "../Markdown";
import { present, t, useT } from "../i18n";
import type { Interaction, TimelineItem, ToolState } from "../types";

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
  const lines = text.split("\n").filter((line) => line.length > 0);
  const structured = lines.length > 0 && lines.every((line) => line.startsWith("+") || line.startsWith("-") || line.startsWith(" "));
  if (!structured) return <pre className="term-body">{text}</pre>;
  return <div className="diff-body">
    {lines.map((line, index) => <div className={line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-del" : "diff-ctx"} key={`${index}-${line.slice(0, 12)}`}>
      <span className="diff-line" aria-hidden="true" />
      <span className="diff-mark">{line[0]}</span>
      <span className="diff-text">{line.slice(1)}</span>
    </div>)}
  </div>;
}

function formatDuration(duration: number): string {
  return duration >= 1000 ? `${(duration / 1000).toFixed(1)}s` : `${duration}ms`;
}

function looksLikeTestFailure(output: string): boolean {
  return /AssertionError|Tests:\s*\d+\s*failed/i.test(output);
}

function describeRisk(flag: string): string {
  const key = `web.risk_flag.${flag}`;
  const label = t(key);
  return label === key ? flag.replace(/_/g, " ") : label;
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

export function SummaryRow({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const tr = useT();
  return <div className="tl-indent">
    <div className={`summary-row ${open ? "open" : ""}`}>
      <div className="summary-head">
        <div className="summary-copy">
          <Lightning className="summary-mark" size={14} weight="fill" aria-hidden="true" />
          <span className="reasoning-label">{tr("web.reasoning")}</span>
          {!open && <strong>{text}</strong>}
        </div>
        <button type="button" className="text-button summary-toggle" onClick={() => setOpen((value) => !value)}>{open ? tr("web.hide") : tr("web.steps")}<CaretDown size={12} className={open ? "" : "collapsed"} /></button>
      </div>
      {open && <p className="summary-body">{text}</p>}
    </div>
  </div>;
}

export function ReasoningRow({ text }: { text: string }) {
  return <SummaryRow text={text} />;
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
  const [open, setOpen] = useState(family === "shell" || family === "edit" || phase === "failed");
  const command = arg(args, ["command", "cmd"]);
  const path = arg(args, ["path", "file", "file_path", "target"]);
  const code = family === "shell" ? exitCode(result, data) : null;
  const body = output || (result ? JSON.stringify(result, null, 2) : "");
  const badge = family === "shell" ? "SHELL" : family === "edit" ? "FILE EDIT" : family === "read" ? "TOOL" : "TOOL";
  const bodyLines = body.split("\n").filter(Boolean);
  const metricSource = result ?? (data && typeof data === "object" ? data as Record<string, unknown> : null);
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
          {family === "edit" && phase === "succeeded" && <span className="muted edit-summary">Applied</span>}
          {code !== null && <span className={code === 0 ? "ok-pill" : "fail-pill"}>Exit {code}{testFailed ? ` (${tr("web.bug_confirmed")})` : ""}</span>}
          {durationLabel && directoryCount === 0 && <span className="mono muted">{durationLabel}</span>}
          <CaretDown size={12} className={open ? "" : "collapsed"} />
        </span>
      </button>
      {open && body && (family === "edit" ? <DiffBody text={body} /> : <pre className={`term-body ${code !== null && code !== 0 ? "failed" : ""}`}>{body}</pre>)}
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

export function InteractionCard({ interaction, respond }: {
  interaction: Interaction;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
}) {
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState("");
  const [error, setError] = useState("");
  const [kept, setKept] = useState("");
  const [more, setMore] = useState(false);
  const sending = useRef(false);
  const tr = useT();
  const isPermission = interaction.kind === "permission";
  const reason = present(interaction.reason_code, interaction.reason_params, interaction.reason ?? "");
  const grant = present(interaction.summary_code, interaction.summary_params, interaction.grant_summary ?? "");
  const command = interaction.command || interaction.preview || interaction.subject || interaction.tool_name || "";
  const risks = interaction.risk_flags ?? [];
  const choices = interaction.choices ?? [];
  const denies = choices.filter((choice) => choice.id === "deny");
  const allows = choices.filter((choice) => choice.id !== "deny");
  const visible = allows.slice(0, 2);
  const extra = allows.slice(2);
  const riskLevel = risks.some((risk) => /recursive|delete|sudo|root/i.test(risk)) ? "Medium" : risks.length ? "Review" : "Low";
  const riskNote = risks.length ? risks.map(describeRisk).join(" · ") : tr("web.risk_unspecified");
  const scopeFallback = visible[0]?.scope || "";
  const scopeDetail = risks.includes("recursive_delete")
    ? t("web.scope_note.recursive_delete")
    : scopeFallback && scopeFallback !== grant ? scopeFallback : "";
  const choose = (choiceId: string) => {
    if (sending.current) return;
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
    <button key={choice.id} type="button" className={`perm-choice ${tone}`} disabled={Boolean(submitting)} onClick={() => choose(choice.id)}>
      {tone === "primary" && <Check size={13} weight="bold" />}{tone === "deny" && <X size={12} weight="bold" />}
      {submitting === choice.id ? tr("web.submitting") : choice.label}
      {tone === "primary" && <kbd>⌥↵</kbd>}{tone === "deny" && <kbd>Esc</kbd>}
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
    <section className="interaction-card" role="alert">
      <div className="perm-head">
        <span className="perm-title"><span className="perm-icon"><ShieldWarning size={12} weight="fill" /></span><strong>{tr("web.permission_required")}</strong><span className="perm-blocked">{tr("web.action_blocked")}</span></span>
        <span className="perm-paused">{tr("web.agent_paused")}<span className="dot pulse" /></span>
      </div>
      <div className="perm-body">
        <div>
          <div className="perm-kicker">{tr("web.agent_wants")}</div>
          <div className="perm-command"><span className="muted">$</span><span>{command}</span><button type="button" className="copy-command" onClick={() => navigator.clipboard?.writeText(command).catch(() => undefined)}>Copy</button></div>
        </div>
        <div className="perm-grid">
          <div>
            <span className="perm-label">Reason</span>
            <span className="sr-only">{reason ? tr("web.reason", { value: reason }) : tr("web.reason_missing")}</span>
            <span>{reason || tr("web.reason_missing")}</span>
          </div>
          <div>
            <span className="perm-label">Scope</span>
            <span className="sr-only">{grant ? tr("web.grant", { value: grant }) : (visible[0]?.scope || tr("web.scope_missing"))}</span>
            <span className="mono scope-value">{grant || visible[0]?.scope || tr("web.scope_missing")}</span>
            {scopeDetail ? <small>{scopeDetail}</small> : null}
          </div>
          <div>
            <span className="perm-label">Risk level</span>
            <span className="risk-level"><i />{riskLevel}</span>
            <small>{riskNote}</small>
          </div>
        </div>
        {error && <p role="status">{error}{kept ? tr("web.kept_choice", { choice: kept }) : ""}</p>}
        <div className="interaction-actions">
          {visible.map((choice, index) => choiceButton(choice, index === 0 ? "primary" : "session"))}
          {denies.map((choice) => choiceButton(choice, "deny"))}
          {extra.length > 0 && <button type="button" className="text-button perm-more" onClick={() => setMore((value) => !value)}>{tr("web.more_grants")}</button>}
        </div>
        {more && extra.length > 0 && <div className="more-grants">
          {extra.map((choice) => <button key={choice.id} type="button" className="perm-choice session" disabled={Boolean(submitting)} onClick={() => choose(choice.id)}>
            <b>{choice.label}</b><small>{choice.scope} · {choice.persistence}</small>
          </button>)}
        </div>}
      </div>
    </section>
  </div>;
}
