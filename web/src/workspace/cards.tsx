import { useRef, useState } from "react";
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
      <span className="diff-mark">{line[0]}</span>
      <span>{line.slice(1)}</span>
    </div>)}
  </div>;
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

export function UserMessage({ text }: { text: string }) {
  const tr = useT();
  return <div className="msg-row">
    <div className="avatar" aria-hidden="true">{tr("web.you").slice(0, 1)}</div>
    <div className="msg-card">
      <div className="msg-card-head">
        <span><strong>{tr("web.you")}</strong> <span className="muted">{tr("web.user_instruction")}</span></span>
      </div>
      <p className="msg-body">{text}</p>
    </div>
  </div>;
}

export function SummaryRow({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return <div className="tl-indent">
    <div className={`summary-row ${open ? "open" : ""}`}>
      <div className="summary-copy">
        <span className="summary-mark" aria-hidden="true">↯</span>
        <strong>{text}</strong>
      </div>
      <button type="button" className="text-button" onClick={() => setOpen((value) => !value)}>{open ? "hide" : "steps"}</button>
    </div>
  </div>;
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
  const family = toolFamily(name, kind);
  const [open, setOpen] = useState(family === "shell" || family === "edit" || phase === "failed");
  const command = arg(args, ["command", "cmd"]);
  const path = arg(args, ["path", "file", "file_path", "target"]);
  const code = family === "shell" ? exitCode(result, data) : null;
  const body = output || (result ? JSON.stringify(result, null, 2) : "");
  const badge = family === "shell" ? "SHELL" : family === "edit" ? "FILE EDIT" : family === "read" ? "TOOL" : "TOOL";
  return <div className="tl-indent">
    <section className="tool-card">
      <button type="button" className="tool-card-head" onClick={() => setOpen((value) => !value)}>
        <span className="tool-title">
          <span className={`tool-badge ${family}`}>{badge}</span>
          {family === "shell" ? <span className="mono command">{command ? `$ ${command}` : name}</span> : <>
            <span className="tool-name">{name}</span>
            {path && <span className="mono path">{path}</span>}
          </>}
        </span>
        <span className="tool-meta">
          {code !== null && <span className={code === 0 ? "ok-pill" : "fail-pill"}>Exit {code}</span>}
          {typeof duration === "number" && <span className="mono muted">{duration >= 1000 ? `${(duration / 1000).toFixed(1)}s` : `${duration}ms`}</span>}
        </span>
      </button>
      {open && body && (family === "edit" ? <DiffBody text={body} /> : <pre className={`term-body ${code !== null && code !== 0 ? "failed" : ""}`}>{body}</pre>)}
    </section>
  </div>;
}

export function toolCardFromTimeline(item: TimelineItem) {
  const result = item.result ?? null;
  const output = result ? (typeof result.output === "string" ? result.output : typeof result.stdout === "string" ? result.stdout : JSON.stringify(result, null, 2)) : "";
  return <ToolCard key={item.id} name={item.name || item.kind} phase={item.phase} args={item.arguments} result={result} duration={item.duration_ms} output={output} kind={item.kind} />;
}

export function toolCardFromState(tool: ToolState) {
  const data = tool.data && typeof tool.data === "object" ? tool.data as Record<string, unknown> : null;
  const output = typeof tool.data === "string" ? tool.data : tool.output || tool.err || (data ? JSON.stringify(data, null, 2) : "");
  return <ToolCard key={tool.call_id} name={tool.name} phase={tool.phase} args={tool.arguments} data={tool.data} output={output} />;
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
      {submitting === choice.id ? tr("web.submitting") : choice.label}
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
        <span className="perm-title"><span className="perm-icon">!</span><strong>{tr("web.permission_required")}</strong><span className="perm-blocked">{tr("web.action_blocked")}</span></span>
        <span className="perm-paused"><span className="dot pulse" />{interaction.tool_name || tr("web.agent_paused")}</span>
      </div>
      <div className="perm-body">
        <div>
          <div className="perm-kicker">{tr("web.agent_wants")}</div>
          <div className="perm-command"><span className="muted">$</span><span>{command}</span></div>
        </div>
        <div className="perm-grid">
          <div>
            <span className="perm-label">Reason</span>
            <span>{reason ? tr("web.reason", { value: reason }) : tr("web.reason_missing")}</span>
          </div>
          <div>
            <span className="perm-label">Scope</span>
            <span className="mono">{grant ? tr("web.grant", { value: grant }) : (visible[0]?.scope || tr("web.scope_missing"))}</span>
          </div>
          <div>
            <span className="perm-label">Risk</span>
            <span>{risks.length ? risks.join(", ") : tr("web.risk_unspecified")}</span>
          </div>
        </div>
        {error && <p role="status">{error}{kept ? tr("web.kept_choice", { choice: kept }) : ""}</p>}
        <div className="interaction-actions">
          {visible.map((choice, index) => choiceButton(choice, index === 0 ? "primary" : "session"))}
          {denies.map((choice) => choiceButton(choice, "deny"))}
          {extra.length > 0 && <button type="button" className="perm-choice session" onClick={() => setMore((value) => !value)}>{tr("web.more_grants")}</button>}
        </div>
        <details className="grant-detail">
          <summary>{[...visible, ...denies].map((choice) => `${choice.label}: ${choice.scope} · ${choice.persistence}`).join("  ·  ")}</summary>
          <ul>
            {choices.map((choice) => <li key={choice.id}><strong>{choice.label}</strong><span>{choice.scope}</span><span>{choice.persistence}</span></li>)}
          </ul>
        </details>
        {more && <div className="more-grants">
          {extra.map((choice) => <button key={choice.id} type="button" className="perm-choice session" disabled={Boolean(submitting)} onClick={() => choose(choice.id)}>
            <b>{choice.label}</b><small>{choice.scope} · {choice.persistence}</small>
          </button>)}
        </div>}
      </div>
    </section>
  </div>;
}
