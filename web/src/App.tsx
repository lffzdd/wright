import {
  Archive,
  ArrowClockwise,
  Brain,
  CaretDown,
  Check,
  CircleNotch,
  Code,
  FileCode,
  GitBranch,
  HardDrives,
  Paperclip,
  PaperPlaneRight,
  Plus,
  SidebarSimple,
  Sparkle,
  Square,
  TerminalWindow,
  Warning,
  X,
} from "@phosphor-icons/react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DiffViewer } from "./DiffViewer";
import { MarkdownContent } from "./Markdown";
import { api, bootstrap } from "./api";
import { getLocale, present, setLocale, t, useT } from "./i18n";
import { commandAfter, parseEvent } from "./protocol";
import { applyEvent } from "./reducer";
import type { Attachment, Interaction, SessionSummary, Snapshot, ViewState } from "./types";

type InspectorTab = "changes" | "plan" | "details";

const initialView = (snapshot: Snapshot): ViewState => ({
  ...snapshot,
  agents: snapshot.agents ?? [],
  notices: snapshot.notices ?? [],
  queued_commands: snapshot.queued_commands ?? [],
  queue_depth: (snapshot.queued_commands ?? []).length,
  seen: [],
  connection: "connecting",
  resync: false,
});

export type Draft = {
  prompt: string;
  attachments: Attachment[];
  commandId: string | null;
  phase: "idle" | "awaiting" | "accepted" | "rejected" | "unknown";
  reason: string;
};

const emptyDraft = (): Draft => ({ prompt: "", attachments: [], commandId: null, phase: "idle", reason: "" });

function formatCount(value: number | null): string {
  return value == null ? "unknown" : value.toLocaleString();
}

export function visibleModels(models: string[], currentModel: string, extras: string[] = []): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const model of [...models, ...extras, currentModel]) {
    const cleaned = model.trim();
    if (!cleaned || seen.has(cleaned)) continue;
    seen.add(cleaned);
    result.push(cleaned);
  }
  return result;
}

function StatusDot({ status }: { status: string }) {
  return <span className={`status-dot status-${status}`} aria-hidden="true" />;
}

function ModelSelector({
  currentModel,
  models,
  running,
  onSelect,
}: {
  currentModel: string;
  models: string[];
  running: boolean;
  onSelect: (model: string) => void;
}) {
  const tr = useT();
  const [open, setOpen] = useState(false);
  const [custom, setCustom] = useState("");
  const [extras, setExtras] = useState<string[]>([]);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const options = visibleModels(models, currentModel, extras);

  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", handleClick);
    return () => window.removeEventListener("mousedown", handleClick);
  }, [open]);

  return (
    <div className="model-selector-wrap" ref={dropdownRef}>
      <button
        type="button"
        className="model-selector-btn"
        disabled={running}
        onClick={() => setOpen(!open)}
        title={running ? tr("web.model_running") : tr("web.model_change")}
      >
        <span>{currentModel || "configured model"}</span>
        <CaretDown size={11} className={open ? "rotated" : ""} />
      </button>
      {open && (
        <div className="model-dropdown">
          <div className="model-dropdown-title">SELECT MODEL</div>
          <div className="model-dropdown-list">
            {options.map((m) => (
              <button
                key={m}
                type="button"
                className={`model-option ${m === currentModel ? "selected" : ""}`}
                onClick={() => {
                  onSelect(m);
                  setOpen(false);
                }}
              >
                <span>{m}</span>
                {m === currentModel && <Check size={13} />}
              </button>
            ))}
          </div>
          <form
            className="model-custom-form"
            onSubmit={(e) => {
              e.preventDefault();
              const next = custom.trim();
              if (next) {
                setExtras((current) => current.includes(next) ? current : [...current, next]);
                onSelect(next);
                setCustom("");
                setOpen(false);
              }
            }}
          >
            <input
              type="text"
              placeholder={tr("web.model_custom")}
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
            />
            <button type="submit" className="button subtle" disabled={!custom.trim()}>
              Set
            </button>
          </form>
        </div>
      )}
    </div>
  );
}

function SessionRail({
  sessions,
  selected,
  onSelect,
  onCreate,
  open,
  onClose,
}: {
  sessions: SessionSummary[];
  selected: string | null;
  onSelect: (session: SessionSummary) => void;
  onCreate: () => void;
  open: boolean;
  onClose: () => void;
}) {
  const tr = useT();
  const active = sessions.filter((item) => item.active);
  const history = sessions.filter((item) => !item.active);
  return (
    <aside className={`session-rail ${open ? "responsive-open" : ""}`}>
      <div className="rail-heading">
        <span>{tr("web.sessions")}</span>
        <div className="rail-buttons">
          <button className="icon-button responsive-only" onClick={onClose} title={tr("web.close_sessions_title")} aria-label={tr("web.close_sessions")}><X size={16} /></button>
          <button className="icon-button" onClick={onCreate} title={tr("web.new_session")} aria-label={tr("web.new_session")}><Plus size={16} /></button>
        </div>
      </div>
      <div className="session-list">
        {active.map((session) => (
          <button key={session.session_id} className={`session-item ${selected === session.session_id ? "selected" : ""}`} onClick={() => onSelect(session)}>
            <StatusDot status={session.execution || session.status} />
            <span className="session-copy">
              <strong>{session.user_goal && session.user_goal !== "(interactive session)" ? session.user_goal : tr("web.session_label", { id: session.session_id.slice(0, 6) })}</strong>
              <small>{session.execution === "queued" ? (session.queue_reason || tr("web.queued_directory")) : session.execution === "waiting_for_input" ? tr("web.waiting") : `${session.environment} · ${session.model ?? tr("web.configured_model")}`}</small>
            </span>
            {Boolean(session.pending_interactions) && <Warning size={15} weight="fill" className="warning-icon" />}
          </button>
        ))}
        {!active.length && <p className="empty-small">{tr("web.no_active")}</p>}
      </div>
      <div className="rail-heading history-heading"><span>HISTORY</span></div>
      <div className="session-list history-list">
        {history.map((session) => (
          <button
            key={session.session_id}
            className="session-item"
            disabled={session.recoverable === false}
            title={session.recoverable === false ? tr("web.archived") : tr("web.resume")}
            onClick={() => onSelect(session)}
          >
            <StatusDot status="closed" />
            <span className="session-copy">
              <strong>{session.user_goal || `Session ${session.session_id.slice(0, 6)}`}</strong>
              <small>{session.saved_at || "checkpoint saved"}</small>
            </span>
          </button>
        ))}
      </div>
      <div className="rail-foot"><HardDrives size={14} /><span>Local runtime only</span></div>
    </aside>
  );
}

function ToolCard({ sessionId, tool }: {
  sessionId: string;
  tool: NonNullable<ViewState["active_turn"]>["tools"][number];
}) {
  const [open, setOpen] = useState(false);
  const done = tool.ok !== undefined;
  const phase = done ? (tool.ok ? "succeeded" : "failed") : (tool.phase ?? "planned");
  const phaseLabel = phase.replace("_", " ");
  return (
    <section className="tool-card">
      <button className="tool-summary" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`tool-icon ${done ? (tool.ok ? "success" : "danger") : phase}`}>
          {done ? (tool.ok ? <Check size={14} /> : <X size={14} />) : phase === "running" ? <CircleNotch className="spin" size={14} /> : <Warning size={14} />}
        </span>
        <Code size={16} />
        <strong>{tool.name}</strong>
        <span>{phaseLabel}</span>
        <CaretDown size={14} className={open ? "rotated" : ""} />
      </button>
      {open && <div className="tool-detail">
        <label>Arguments</label>
        <pre>{JSON.stringify(tool.arguments ?? {}, null, 2)}</pre>
        {tool.output && <><label>Output</label><pre>{tool.output}</pre></>}
        {done && <><label>Result</label><pre>{tool.ok ? JSON.stringify(tool.data ?? {}, null, 2) : tool.err}</pre></>}
        {!!tool.artifacts?.length && <><label>Artifacts</label><div className="tool-artifacts">
          {tool.artifacts.map((artifact) => <a
            key={artifact.id}
            href={api.artifactUrl(sessionId, artifact.id)}
            target="_blank"
            rel="noreferrer"
            title={`${artifact.media_type} · ${artifact.size.toLocaleString()} bytes`}
          >{artifact.name}</a>)}
        </div></>}
      </div>}
    </section>
  );
}

export function InteractionCard({ interaction, respond }: {
  interaction: Interaction;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
}) {
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState("");
  const [error, setError] = useState("");
  const [kept, setKept] = useState("");
  const sending = useRef(false);
  const tr = useT();
  const isPermission = interaction.kind === "permission";
  const reason = present(interaction.reason_code, interaction.reason_params, interaction.reason ?? "");
  const grant = present(interaction.summary_code, interaction.summary_params, interaction.grant_summary ?? "");
  const choiceText = (choice: { id: string; label: string; scope: string; persistence: string }) => {
    const label = present(`permission.choice.${choice.id}`, undefined, choice.label);
    const scopeKey = choice.id === "allow_once" ? "permission.scope.once" : choice.id === "deny" ? "permission.scope.none" : "";
    const persistenceKey = {
      allow_once: "permission.persistence.none",
      deny: "permission.persistence.none",
      allow_session_directory: "permission.persistence.session_directory",
      allow_persistent_directory: "permission.persistence.persistent_directory",
      allow_session_rule: "permission.persistence.session_rule",
      allow_persistent_rule: "permission.persistence.persistent_rule",
    }[choice.id] ?? "";
    return {
      label,
      scope: present(scopeKey, undefined, choice.scope),
      persistence: present(persistenceKey, undefined, choice.persistence),
    };
  };
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
  return (
    <section className="interaction-card" role="alert">
      <div className="interaction-title"><Warning size={18} weight="fill" /><strong>{isPermission ? tr("web.permission_title", { tool: interaction.tool_name ?? "" }) : tr("web.needs_input")}</strong>{interaction.agent_task_id ? <small>{tr("web.subagent", { id: interaction.agent_task_id })}</small> : null}</div>
      {isPermission ? <>
        {interaction.operation && <p>{tr("web.operation", { value: interaction.operation })}</p>}
        <p>{interaction.subject}</p>
        {reason && <p>{tr("web.reason", { value: reason })}</p>}
        {grant && <p>{tr("web.grant", { value: grant })}</p>}
        {(interaction.http_method || interaction.http_target) && <p>HTTP: {interaction.http_method} {interaction.http_target}</p>}
        {interaction.command && <p>{tr("web.command", { value: interaction.command })}</p>}
        {interaction.cwd && <p>{tr("web.directory", { value: interaction.cwd })}</p>}
        {interaction.shell_note && <p>{present("permission.shell_note", { cwd: interaction.cwd ?? "" }, interaction.shell_note)}</p>}
        {interaction.preview && <pre className="permission-preview">{interaction.preview}</pre>}
      </> : <p>{interaction.question}</p>}
      {isPermission && interaction.risk_flags?.length && <small>{tr("web.risk", { value: interaction.risk_flags.join(", ") })}</small>}
      {isPermission && interaction.targets?.length && <div className="permission-scope">
        <small><strong>{tr("web.targets")}</strong> <code>{interaction.targets.join("; ")}</code></small>
        {interaction.principal && <small><strong>{tr("web.principal")}</strong> {interaction.principal}</small>}
      </div>}
      {interaction.context && <small>{interaction.context}</small>}
      {error && <p role="status">{error}{kept ? tr("web.kept_choice", { choice: kept }) : ""}</p>}
      {isPermission ? <div className="interaction-actions">
        {interaction.choices?.map((choice) => {
          const text = choiceText(choice);
          return <button
          key={choice.id}
          className={`button ${choice.id === "deny" ? "secondary" : "primary"}`}
          disabled={Boolean(submitting)}
          onClick={() => choose(choice.id)}
        >{submitting === choice.id ? tr("web.submitting") : text.label}<small>{text.scope} · {text.persistence}</small></button>;
        })}
      </div> : <form className="ask-form" onSubmit={(event) => { event.preventDefault(); if (answer.trim() && !submitting) { setSubmitting("answer"); const sent = respond(interaction.request_id, answer.trim()); Promise.resolve(sent).then((accepted) => { if (accepted === false) { setSubmitting(""); setError(t("web.answer_failed")); } }); } }}>
        {interaction.options?.map((option) => <button type="button" className="option-button" key={option} disabled={Boolean(submitting)} onClick={() => { if (submitting) return; setSubmitting(option); const sent = respond(interaction.request_id, option); Promise.resolve(sent).then((accepted) => { if (accepted === false) setSubmitting(""); }); }}>{option}</button>)}
        <input aria-label={tr("web.answer")} value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder={tr("web.answer_placeholder")} />
        <button className="icon-button" aria-label={tr("web.submit_answer")} disabled={Boolean(submitting)}><PaperPlaneRight size={16} /></button>
      </form>}
    </section>
  );
}

function MessageAttachments({ sessionId, attachments }: { sessionId: string; attachments?: Attachment[] }) {
  if (!attachments?.length) return null;
  return <div className="message-attachments">{attachments.map((attachment) => (
    <a key={attachment.id} href={api.attachmentUrl(sessionId, attachment.id)} target="_blank" rel="noreferrer" title={`${attachment.filename} · ${attachment.width}×${attachment.height}`}>
      <img src={api.attachmentThumbnailUrl(sessionId, attachment.id)} alt={attachment.filename} />
      <span>{attachment.filename}</span>
    </a>
  ))}</div>;
}

function Timeline({ state, respond, cancelQueued }: { state: ViewState; respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>; cancelQueued: (commandId: string) => void }) {
  const tr = useT();
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [state.history.length, state.active_turn?.content, state.active_turn?.tools.length]);
  return (
    <div className="timeline">
      {!state.history.length && !state.active_turn && <div className="welcome">
        <Sparkle size={30} weight="duotone" />
        <h2>{tr("web.ready")}</h2>
        <p>{tr("web.ready_help")}</p>
      </div>}
      {state.history.map((turn, index) => <div className="turn" key={turn.turn_id || `${index}`}>
        <div className="message user-message"><span>{tr("web.you")}</span>{turn.user && <p>{turn.user}</p>}<MessageAttachments sessionId={state.session.session_id} attachments={turn.attachments} /></div>
        <div className="message assistant-message"><span>Wright{turn.status && turn.status !== "completed" ? ` · ${turn.status}` : ""}</span>{turn.assistant ? <MarkdownContent content={turn.assistant} /> : <p>{tr("web.no_answer")}</p>}</div>
        {turn.tools?.map((tool) => <ToolCard key={tool.call_id} sessionId={state.session.session_id} tool={tool} />)}
      </div>)}
      {state.active_turn && <div className="turn active-turn">
        <div className="message user-message"><span>{tr("web.you")}</span>{state.active_turn.prompt && <p>{state.active_turn.prompt}</p>}<MessageAttachments sessionId={state.session.session_id} attachments={state.active_turn.attachments} /></div>
        {state.active_turn.reasoning && <details className="reasoning" open={!state.active_turn.content}>
          <summary><Brain size={16} />Reasoning</summary>
          <div>{state.active_turn.reasoning}</div>
        </details>}
        {state.active_turn.tools.map((tool) => <ToolCard key={tool.call_id} sessionId={state.session.session_id} tool={tool} />)}
        <div className="message assistant-message streaming"><span>Wright</span>{state.active_turn.content ? <MarkdownContent content={state.active_turn.content} /> : <span className="thinking"><i />Working…</span>}</div>
      </div>}
      {(state.agents ?? []).map((agent) => <section className="agent-progress" key={agent.task_id}>
        <strong>Subagent {agent.task_id} · depth {agent.depth} · {agent.status}</strong>
        {agent.content && <MarkdownContent content={agent.content} />}
        {agent.tools.map((tool) => <ToolCard key={tool.call_id} sessionId={state.session.session_id} tool={tool} />)}
      </section>)}
      {state.pending_interactions.map((item) => <InteractionCard key={item.request_id} interaction={item} respond={respond} />)}
      {state.session.execution === "queued" && <section className="queue-list" aria-label={tr("web.directory_queue")}>
        <strong>Queued</strong>
        <p>{state.session.queue_reason || "Waiting for the current task in this directory to finish."}</p>
      </section>}
      {state.session.execution === "waiting_for_input" && <p className="system-notice" role="status">{tr("web.waiting_notice")}</p>}
      {state.queued_commands.length > 0 && <section className="queue-list" aria-label={tr("web.queued_instructions")}>
        <strong>{state.queued_commands.length} queued instruction{state.queued_commands.length === 1 ? "" : "s"}</strong>
        {state.queued_commands.map((item) => <div className="queue-item" key={item.command_id}>
          <span>{item.prompt || tr("web.attached_images")}{item.attachments?.length ? ` · ${tr(item.attachments.length === 1 ? "web.image_count_one" : "web.image_count_many", { count: item.attachments.length })}` : ""}</span>
          <button type="button" className="button subtle" onClick={() => cancelQueued(item.command_id)}>{tr("web.remove_queued")}</button>
        </div>)}
      </section>}
      {state.notices.slice(-5).map((notice) => <div className="system-notice" role="status" key={notice.id}>{present(notice.code, notice.params, notice.text)}</div>)}
      <div ref={bottom} />
    </div>
  );
}

export function Composer({
  sessionId,
  connection,
  draft,
  updateDraft,
  submit,
  cancel,
  running,
}: {
  sessionId: string;
  connection: ViewState["connection"];
  draft: Draft;
  updateDraft: (sessionId: string, patch: (current: Draft) => Draft) => void;
  submit: (commandId: string, prompt: string, attachmentIds: string[]) => Promise<"accepted" | "rejected" | "unknown">;
  cancel: () => Promise<void>;
  running: boolean;
}) {
  const tr = useT();
  const [uploading, setUploading] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const upload = async (files: FileList | File[]) => {
    const owner = sessionId;
    const selected = Array.from(files);
    if (!selected.length) return;
    setUploading(true);
    updateDraft(owner, (current) => ({ ...current, reason: "" }));
    const uploaded: Attachment[] = [];
    const errors: string[] = [];
    for (const file of selected) {
      try { uploaded.push(await api.uploadAttachment(owner, file) as Attachment); }
      catch (reason) { errors.push(String(reason)); }
    }
    if (uploaded.length) {
      updateDraft(owner, (current) => ({ ...current, attachments: [...current.attachments, ...uploaded] }));
    }
    if (errors.length) updateDraft(owner, (current) => ({ ...current, reason: errors.join("; ") }));
    setUploading(false);
  };
  const remove = async (attachment: Attachment) => {
    const owner = sessionId;
    try {
      await api.deleteAttachment(owner, attachment.id);
      updateDraft(owner, (current) => ({ ...current, attachments: current.attachments.filter((item) => item.id !== attachment.id) }));
    } catch (reason) {
      updateDraft(owner, (current) => ({ ...current, reason: String(reason) }));
    }
  };
  const send = async (event: FormEvent) => {
    event.preventDefault();
    const current = draft;
    if (!current.prompt.trim() && !current.attachments.length) return;
    const commandId = current.commandId && current.phase !== "rejected" ? current.commandId : crypto.randomUUID();
    updateDraft(sessionId, (item) => ({ ...item, commandId, phase: "awaiting", reason: "" }));
    const outcome = await submit(commandId, current.prompt.trim(), current.attachments.map((attachment) => attachment.id));
    const next = commandAfter(outcome === "accepted" ? "accepted" : outcome === "rejected" ? "rejected" : "disconnected");
    if (next.phase === "accepted") {
      updateDraft(sessionId, (item) => item.commandId === commandId ? emptyDraft() : item);
    }
    else {
      updateDraft(sessionId, (item) => ({
        ...item,
        commandId: next.keepId ? commandId : null,
        phase: next.phase,
        reason: next.phase === "rejected"
          ? "The server rejected this command. Edit it and send again to start a new one. Acceptance only means the command was received."
          : "Not confirmed. Retry keeps this command id and does not start a second one.",
      }));
    }
  };
  const offline = connection === "disconnected" || connection === "reconnecting" || connection === "closed";
  const hint = draft.reason
    || (uploading ? "Uploading images…" : "")
    || (draft.phase === "awaiting" ? "Waiting for the server to accept this command." : "")
    || (offline ? "Offline. The draft stays here until this command is confirmed." : "Drop, paste, or attach images · Enter to send · Shift+Enter for newline");
  return <form className="composer" onSubmit={(event) => { send(event).catch((reason) => updateDraft(sessionId, (item) => ({ ...item, phase: "unknown", reason: String(reason) }))); }} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); upload(event.dataTransfer.files); }}>
    <input ref={input} className="file-input" type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => { if (event.target.files) upload(event.target.files); event.target.value = ""; }} />
    {draft.attachments.length > 0 && <div className="composer-attachments">{draft.attachments.map((attachment) => <div className="composer-attachment" key={attachment.id}><img src={api.attachmentThumbnailUrl(sessionId, attachment.id)} alt="" /><span>{attachment.filename}</span><button type="button" onClick={() => remove(attachment)} aria-label={tr("web.remove", { name: attachment.filename })}><X size={12} /></button></div>)}</div>}
    <textarea aria-label={tr("web.message")} value={draft.prompt} onPaste={(event) => { if (event.clipboardData.files.length) upload(event.clipboardData.files); }} onChange={(event) => updateDraft(sessionId, (item) => ({ ...item, prompt: event.target.value }))} placeholder={running ? tr("web.queue_placeholder") : tr("web.message_placeholder")} rows={2} onKeyDown={(event) => {
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
    }} />
    <div className="composer-actions">
      <button type="button" className="icon-button" title={tr("web.attach")} onClick={() => input.current?.click()}><Paperclip size={16} /></button>
      <span>{hint}</span>
      {running && <button type="button" className="button stop" onClick={() => { cancel().catch(() => undefined); }}><Square size={13} weight="fill" />{tr("web.stop")}</button>}
      <button className="button primary" disabled={uploading || draft.phase === "awaiting" || (!draft.prompt.trim() && !draft.attachments.length)}><PaperPlaneRight size={15} />{draft.phase === "unknown" ? tr("web.retry") : tr("web.send")}</button>
    </div>
  </form>;
}

export function Inspector({ state, sessionId, open, close }: { state: ViewState; sessionId: string; open: boolean; close: () => void }) {
  const tr = useT();
  const [tab, setTab] = useState<InspectorTab>("changes");
  const [changes, setChanges] = useState<Array<{ path: string; status: string }> | null>(null);
  const [changesError, setChangesError] = useState("");
  const [warning, setWarning] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [patch, setPatch] = useState("");
  const [truncated, setTruncated] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    setSelected(null);
    setPatch("");
    setChanges(null);
    setChangesError("");
  }, [sessionId]);
  const refresh = useCallback(() => {
    const request = ++generation.current;
    const owner = sessionId;
    api.changes(owner).then((result) => {
      if (generation.current !== request) return;
      setChanges(result.changes);
      setWarning(result.local_warning);
      setChangesError("");
    }).catch((error) => {
      if (generation.current !== request) return;
      setChangesError(String(error));
      setChanges(null);
    });
  }, [sessionId]);
  useEffect(() => { refresh(); }, [refresh, state.session.status]);
  const openPatch = (path: string) => {
    const request = ++generation.current;
    const owner = sessionId;
    setSelected(path);
    setPatch("");
    api.patch(owner, path).then((result) => {
      if (generation.current !== request) return;
      setPatch(result.patch);
      setTruncated(result.truncated);
    }).catch((error) => {
      if (generation.current !== request) return;
      setPatch("");
      setChangesError(String(error));
      setTruncated(false);
    });
  };
  return <aside className={`inspector ${open ? "responsive-open" : ""}`}>
    <div className="inspector-tabs" role="tablist">
      {(["changes", "plan", "details"] as InspectorTab[]).map((item) => <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{tr(`web.tab.${item}`)}</button>)}
      <button className="icon-button responsive-only inspector-close" onClick={close} aria-label={tr("web.close_inspector")}><X size={16} /></button>
    </div>
    {tab === "changes" && <div className="inspector-body">
      <div className="section-title"><span><FileCode size={16} />{changes ? tr(changes.length === 1 ? "web.files_changed_one" : "web.files_changed_many", { count: changes.length }) : tr("web.changes_unavailable")}</span><button className="icon-button" title={tr("web.refresh_changes")} onClick={refresh}><ArrowClockwise size={15} /></button></div>
      {warning && <div className="local-warning"><Warning size={15} />{tr("web.local_warning")}</div>}
      {changesError && <p className="form-error" role="alert">{changesError}</p>}
      <div className="change-list">{(changes ?? []).map((change) => <button key={change.path} className={selected === change.path ? "selected" : ""} onClick={() => openPatch(change.path)}><b>{change.status}</b><span>{change.path}</span></button>)}</div>
      {selected && <div className="patch">{truncated ? <p className="empty-small">{tr("web.binary_patch")}</p> : <DiffViewer patch={patch} filename={selected} />}</div>}
      {changes && !changes.length && !changesError && <p className="empty-small">{tr("web.tree_clean")}</p>}
    </div>}
    {tab === "plan" && <div className="inspector-body">
      <div className="section-title"><span>{tr("web.plan")}</span><small>{state.plan.status ?? tr("web.plan_empty")}</small></div>
      {state.plan.objective && <p className="plan-objective">{state.plan.objective}</p>}
      <ol className="plan-list">{state.plan.steps?.map((step) => <li key={step.id} className={`plan-${step.status}`}><span>{step.status === "completed" ? <Check size={13} /> : <i />}</span><div><strong>{step.title}</strong>{step.note && <small>{step.note}</small>}</div></li>)}</ol>
      {!state.plan.steps?.length && <p className="empty-small">{tr("web.no_plan")}</p>}
    </div>}
    {tab === "details" && <div className="inspector-body details-grid">
      <label>{tr("web.detail_session")}</label><code>{state.session.session_id}</code>
      <label>Environment</label><span>{state.session.environment}</span>
      <label>Branch</label><code>{state.session.branch_name || "current checkout"}</code>
      <label>Execution root</label><code>{state.session.execution_root}</code>
      <label>Request usage</label><span>{formatCount(state.usage.request_prompt_tokens)} in · {formatCount(state.usage.request_completion_tokens)} out</span>
      <label>Task total</label><span>{formatCount(state.usage.prompt_tokens)} in · {formatCount(state.usage.completion_tokens)} out · {formatCount(state.usage.total_tokens)} total</span>
    </div>}
  </aside>;
}

export function NewSessionDialog({
  project,
  close,
  create,
}: {
  project: Record<string, unknown>;
  close: () => void;
  create: (environment: string, prompt: string, model?: string) => Promise<void>;
}) {
  const tr = useT();
  const dirtyCheckout = Boolean(project.dirty_checkout);
  const defaultEnvironment = dirtyCheckout
    ? "local"
    : String(project.default_environment ?? "local");
  const models = Array.isArray(project.models) ? (project.models as string[]) : [];
  const defaultModel = String(project.default_model ?? (models[0] || ""));
  const [environment, setEnvironment] = useState(defaultEnvironment);
  const [model, setModel] = useState(defaultModel);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const trap = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); close(); return; }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), select:not([disabled]), textarea:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", trap);
    return () => { document.removeEventListener("keydown", trap); previous?.focus(); };
  }, [close]);
  return <div className="dialog-backdrop" role="presentation"><section ref={dialogRef} className="dialog" role="dialog" aria-modal="true" aria-labelledby="new-session-title">
    <div className="dialog-head"><div><span>{tr("web.new_session_kicker")}</span><h2 id="new-session-title">{tr("web.choose_environment")}</h2></div><button className="icon-button" onClick={close} title={tr("web.close_esc")} aria-label={tr("web.close")}><X size={18} /></button></div>
    <div className="environment-grid" role="group" aria-label={tr("web.environment")}>
      <button aria-pressed={environment === "worktree"} className={environment === "worktree" ? "selected" : ""} disabled={!project.git} onClick={() => setEnvironment("worktree")}><GitBranch size={22} /><strong>{tr("web.isolated")}</strong><span>{tr("web.isolated_help")}</span>{!dirtyCheckout && <em>{tr("web.recommended")}</em>}</button>
      <button aria-pressed={environment === "local"} className={environment === "local" ? "selected" : ""} onClick={() => setEnvironment("local")}><TerminalWindow size={22} /><strong>{tr("web.current_checkout")}</strong><span>{tr("web.current_help")}</span>{dirtyCheckout && <em>{tr("web.recommended_dirty")}</em>}</button>
    </div>
    {models.length > 0 && <div className="dialog-field">
      <label className="field-label" htmlFor="first-model">{tr("web.model_optional")} <span>{tr("web.optional")}</span></label>
      <select id="first-model" className="dialog-select" value={model} onChange={(event) => setModel(event.target.value)}>
        {models.map((m) => <option key={m} value={m}>{m}</option>)}
      </select>
    </div>}
    <label className="field-label" htmlFor="first-prompt">First instruction <span>optional</span></label>
    <textarea autoFocus id="first-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder={tr("web.first_prompt")} rows={3} />
    {error && <p className="form-error">{error}</p>}
    <div className="dialog-actions"><button className="button secondary" onClick={close}>{tr("web.cancel")}</button><button className="button primary" disabled={busy} onClick={() => { setBusy(true); setError(""); create(environment, prompt, model).catch((reason) => { setError(String(reason)); setBusy(false); }); }}>{busy ? <CircleNotch className="spin" size={15} /> : <Plus size={15} />}{tr("web.create_session")}</button></div>
  </section></div>;
}

export default function App() {
  const tr = useT();
  const [project, setProject] = useState<Record<string, unknown> | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [state, setState] = useState<ViewState | null>(null);
  const [dialog, setDialog] = useState(false);
  const [fatal, setFatal] = useState("");
  const [railOpen, setRailOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const socket = useRef<WebSocket | null>(null);
  const commandWaiters = useRef(new Map<string, (outcome: "accepted" | "rejected" | "unknown") => void>());
  const epoch = useRef(0);
  const streamId = useRef("");
  const respondIds = useRef(new Map<string, string>());
  const cancelId = useRef<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const draftsRef = useRef(drafts);
  draftsRef.current = drafts;
  const activeSent = useRef(new Set<string>());
  const updateDraft = useCallback((sessionId: string, patch: (current: Draft) => Draft) => {
    setDrafts((current) => {
      const previous = current[sessionId] ?? emptyDraft();
      return { ...current, [sessionId]: patch(previous) };
    });
  }, []);

  const refreshSessions = useCallback(async () => {
    const items = await api.sessions();
    setSessions(items);
    return items;
  }, []);

  useEffect(() => {
    api.preferences().then((prefs) => setLocale(prefs.interface_language)).catch(() => undefined);
  }, []);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setDialog(true);
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        setRailOpen((prev) => !prev);
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        setInspectorOpen((prev) => !prev);
      } else if (e.key === "Escape") {
        setDialog(false);
        setRailOpen(false);
        setInspectorOpen(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  useEffect(() => {
    bootstrap().then(async () => {
      const [projectData, items] = await Promise.all([api.project(), refreshSessions()]);
      setProject(projectData);
      const first = items.find((item) => item.active);
      if (first) setSelected(first.session_id); else setDialog(true);
    }).catch((error) => setFatal(String(error)));
  }, [refreshSessions]);

  useEffect(() => {
    if (!selected) { setState(null); return; }
    const generation = ++epoch.current;
    const sessionId = selected;
    socket.current?.close();
    let retryTimer: number | undefined;
    const owned = () => epoch.current === generation;
    const connect = (snapshot: Snapshot, attempt = 0) => {
      if (!owned()) return;
      streamId.current = snapshot.stream_id;
      const sentHere = new Set<string>();
      activeSent.current = sentHere;
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${protocol}://${location.host}/api/v1/sessions/${sessionId}/stream?stream_id=${snapshot.stream_id}&last_seq=${snapshot.last_seq}`);
      socket.current = ws;
      ws.onopen = () => {
        if (!owned()) { ws.close(); return; }
        setState((current) => current && current.session.session_id === sessionId ? { ...current, connection: "connected" } : current);
        const pending = draftsRef.current[sessionId];
        if (pending?.commandId && (pending.phase === "awaiting" || pending.phase === "unknown")) {
          api.commandStatus(sessionId, pending.commandId).then((record) => {
            if (!owned()) return;
            if (record.status === "unknown") {
              updateDraft(sessionId, (item) => ({ ...item, phase: "unknown", reason: "The server does not know whether this command finished. It will not be sent with a new id." }));
            } else {
              updateDraft(sessionId, () => emptyDraft());
            }
          }).catch(() => {
            if (!owned()) return;
            updateDraft(sessionId, (item) => ({ ...item, phase: "unknown", reason: "Not confirmed. Retry keeps this command id." }));
          });
        }
      };
      ws.onclose = () => {
        for (const id of sentHere) {
          const waiter = commandWaiters.current.get(id);
          if (waiter) {
            commandWaiters.current.delete(id);
            waiter("unknown");
          }
        }
        if (!owned()) return;
        setState((current) => current && current.session.session_id === sessionId ? { ...current, connection: "reconnecting" } : current);
        retryTimer = window.setTimeout(() => {
          if (!owned()) return;
          api.snapshot(sessionId).then((fresh) => {
            if (!owned()) return;
            setState({ ...initialView(fresh), connection: "reconnecting" });
            connect(fresh, attempt + 1);
          }).catch(() => { if (owned()) connect(snapshot, attempt + 1); });
        }, Math.min(10_000, 500 * (2 ** Math.min(attempt, 5))));
      };
      ws.onmessage = (message) => {
        if (!owned()) return;
        let body: unknown;
        try { body = JSON.parse(message.data); }
        catch { return; }
        if (body && typeof body === "object" && "snapshot" in (body as Record<string, unknown>)) {
          const snapshot = (body as { snapshot: Snapshot }).snapshot;
          if (snapshot.session?.session_id && snapshot.session.session_id !== sessionId) return;
          streamId.current = snapshot.stream_id;
          setState({ ...initialView(snapshot), connection: "connected" });
          refreshSessions().catch(() => undefined);
          return;
        }
        const parsed = parseEvent(body, { sessionId, streamId: streamId.current });
        if (!parsed.ok) {
          if (parsed.reason === "malformed" || parsed.reason === "session") return;
          setState((current) => current && current.session.session_id === sessionId ? { ...current, resync: true } : current);
          return;
        }
        const event = parsed.event;
        if (event.type === "command.accepted" || event.type === "command.rejected") {
          const waiter = commandWaiters.current.get(String(event.payload.command_id ?? ""));
          if (waiter) {
            commandWaiters.current.delete(String(event.payload.command_id ?? ""));
            waiter(event.type === "command.accepted" ? "accepted" : "rejected");
          }
        }
        setState((current) => {
          if (!current || current.session.session_id !== sessionId) return current;
          const applied = applyEvent(current, event);
          return applied;
        });
        if ([
          "turn.started", "turn.completed", "turn.failed", "turn.cancelled",
          "interaction.requested", "interaction.resolved", "session.status_changed",
        ].includes(event.type)) {
          refreshSessions().catch(() => undefined);
        }
      };
    };
    api.snapshot(sessionId).then((snapshot) => {
      if (!owned()) return;
      streamId.current = snapshot.stream_id;
      setState(initialView(snapshot));
      connect(snapshot);
    }).catch((error) => { if (owned()) setFatal(String(error)); });
    return () => { epoch.current += 1; if (retryTimer !== undefined) window.clearTimeout(retryTimer); socket.current?.close(); };
  }, [selected, refreshSessions, updateDraft]);

  useEffect(() => {
    const timer = window.setInterval(() => refreshSessions().catch(() => undefined), 5_000);
    return () => window.clearInterval(timer);
  }, [refreshSessions]);

  const sendCommand = (payload: Record<string, unknown>) => {
    const id = String(payload.command_id ?? "");
    if (socket.current?.readyState !== WebSocket.OPEN) return Promise.resolve("unknown" as const);
    activeSent.current.add(id);
    return new Promise<"accepted" | "rejected" | "unknown">((resolve) => {
      commandWaiters.current.set(id, resolve);
      socket.current?.send(JSON.stringify(payload));
    });
  };
  const create = async (environment: string, prompt: string, model?: string) => {
    const snapshot = await api.create({ environment, prompt: prompt || undefined, model: model || undefined });
    await refreshSessions();
    setSelected(snapshot.session.session_id);
    setDialog(false);
  };
  const changeModel = async (newModel: string) => {
    const sessionId = selected;
    const generation = epoch.current;
    if (!sessionId) return;
    try {
      await api.setModel(sessionId, newModel);
      if (epoch.current !== generation) return;
      setState((current) => current && current.session.session_id === sessionId ? { ...current, session: { ...current.session, model: newModel } } : current);
      refreshSessions().catch(() => undefined);
    } catch (error) {
      if (epoch.current === generation) setFatal(String(error));
    }
  };
  const archiveSession = async () => {
    if (!state) return;
    if (!window.confirm("Archive this session? A clean isolated worktree may be removed.")) return;
    await api.archive(state.session.session_id);
    setSelected(null);
    setState(null);
    await refreshSessions();
  };
  const selectSession = async (session: SessionSummary) => {
    if (session.recoverable === false) {
      setFatal("This archived session is history-only because its clean worktree was removed.");
      return;
    }
    if (!session.active) {
      const snapshot = await api.create({ resume_session_id: session.session_id });
      await refreshSessions();
      setSelected(snapshot.session.session_id);
    } else setSelected(session.session_id);
    setRailOpen(false);
  };
  useEffect(() => {
    if (!state?.resync || !selected) return;
    const generation = epoch.current;
    const sessionId = selected;
    api.snapshot(sessionId).then((snapshot) => {
      if (epoch.current !== generation) return;
      streamId.current = snapshot.stream_id;
      setState({ ...initialView(snapshot), connection: "connected" });
    }).catch((error) => {
      if (epoch.current === generation) setFatal(String(error));
    });
  }, [state?.resync, selected]);

  const execution = state?.session.execution ?? state?.session.status;
  const running = execution === "running" || execution === "waiting_for_input";
  const directoryQueued = execution === "queued";
  const contextTokens = state?.usage.context_tokens;
  const contextLimit = state?.usage.context_limit;
  const contextKnown = contextTokens != null && contextLimit != null && contextLimit > 0;
  const contextPercent = contextKnown ? Math.min(100, Math.round((contextTokens / contextLimit) * 100)) : 0;
  const draft = selected ? (drafts[selected] ?? emptyDraft()) : emptyDraft();

  if (fatal && !project) return <main className="fatal"><Warning size={28} /><h1>{tr("web.fatal_title")}</h1><p>{fatal}</p><button className="button primary" onClick={() => location.reload()}>{tr("web.reload")}</button></main>;
  const connection = state?.connection ?? "connecting";
  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><span className="brand-mark">W</span><div><strong>Wright</strong><small>{String(project?.name ?? tr("web.local"))}</small></div></div>
      <div className="top-status">
        <span className={`connection ${connection}`}><StatusDot status={connection} />{tr(`web.connection.${connection}`)}</span>
        <ModelSelector
          currentModel={state?.session.model ?? String(project?.default_model ?? tr("web.configured_model"))}
          models={Array.isArray(project?.models) ? (project.models as string[]) : []}
          running={Boolean(running || directoryQueued)}
          onSelect={changeModel}
        />
        <span className="context-meter" title={contextKnown ? tr("web.context_title", { used: contextTokens, limit: contextLimit }) : tr("web.context_unknown_title")}><i style={{ width: `${contextPercent}%` }} />{contextKnown ? tr("web.context_percent", { percent: contextPercent }) : tr("web.context_unknown")}</span>
        <span>{tr("web.active", { count: `${sessions.filter((item) => item.active).length}/${project?.capacity as number ?? 0}` })}</span>
        <label className="language-control">
          <span>{tr("web.language_label")}</span>
          <select
            aria-label={tr("web.language_label")}
            title={tr("web.language_note")}
            value={getLocale()}
            onChange={(event) => {
              const next = setLocale(event.target.value);
              api.setPreference(next).catch(() => undefined);
            }}
          >
            <option value="en">English</option>
            <option value="zh-CN">简体中文</option>
          </select>
        </label>
      </div>
      <button className="icon-button mobile-sessions" onClick={() => setRailOpen(true)} title={tr("web.open_sessions_title")} aria-label={tr("web.open_sessions")}><SidebarSimple size={18} /></button>
    </header>
    <div className="workspace-grid">
      <SessionRail sessions={sessions} selected={selected} onSelect={(session) => selectSession(session).catch((error) => setFatal(String(error)))} onCreate={() => setDialog(true)} open={railOpen} onClose={() => setRailOpen(false)} />
      <main className="conversation">
        {state ? <>
          <div className="conversation-head"><div><span className="eyebrow">{state.session.environment === "worktree" ? tr("web.worktree") : tr("web.checkout")}</span><h1>{state.session.user_goal && state.session.user_goal !== "(interactive session)" ? state.session.user_goal : tr("web.session_label", { id: state.session.session_id.slice(0, 6) })}</h1></div><div className="session-actions"><button className="icon-button inspector-trigger" title={tr("web.open_inspector")} onClick={() => setInspectorOpen(true)}><FileCode size={17} /></button><button className="icon-button" title={tr("web.close_session")} aria-label={tr("web.close_session")} onClick={() => api.close(state.session.session_id).then(() => { setSelected(null); setState(null); refreshSessions(); }).catch((error) => setFatal(String(error)))}><SidebarSimple size={17} /></button><button className="icon-button" title={tr("web.archive")} onClick={() => archiveSession().catch((error) => setFatal(String(error)))}><Archive size={17} /></button></div></div>
          <Timeline
            state={state}
            respond={(requestId, answer) => {
              const existing = respondIds.current.get(requestId);
              const id = existing ?? crypto.randomUUID();
              respondIds.current.set(requestId, id);
              return sendCommand({
                type: "interaction.respond",
                command_id: id,
                request_id: requestId,
                answer,
              }).then((outcome) => {
                if (outcome === "accepted" || outcome === "rejected") respondIds.current.delete(requestId);
                return outcome === "accepted";
              });
            }}
            cancelQueued={(targetCommandId) => {
              const id = crypto.randomUUID();
              sendCommand({ type: "turn.cancel_queued", command_id: id, target_command_id: targetCommandId }).catch(() => undefined);
            }}
          />
          <Composer
            sessionId={state.session.session_id}
            connection={state.session.lifecycle === "closing" || state.session.lifecycle === "closed" || state.session.status === "closing" || state.session.status === "closed" ? "closed" : state.connection}
            draft={draft}
            updateDraft={updateDraft}
            running={Boolean(running || directoryQueued)}
            submit={(commandId, prompt, attachmentIds) => sendCommand({ type: "turn.submit", command_id: commandId, prompt, attachment_ids: attachmentIds })}
            cancel={async () => {
              const id = cancelId.current ?? crypto.randomUUID();
              cancelId.current = id;
              const outcome = await sendCommand({ type: "turn.cancel", command_id: id });
              if (outcome === "accepted" || outcome === "rejected") cancelId.current = null;
            }}
          />
        </> : <div className="no-session"><TerminalWindow size={36} weight="duotone" /><h2>{tr("web.no_session")}</h2><p>{tr("web.no_session_help")}</p><button className="button primary" onClick={() => setDialog(true)}><Plus size={15} />{tr("web.new_session_button")}</button></div>}
      </main>
      {state && selected ? <Inspector state={state} sessionId={selected} open={inspectorOpen} close={() => setInspectorOpen(false)} /> : <aside className="inspector empty-inspector"><FileCode size={24} /><p>{tr("web.inspector_empty")}</p></aside>}
    </div>
    {fatal && project && <div className="toast" role="alert"><Warning size={16} /><span>{fatal}</span><button className="icon-button" onClick={() => setFatal("")} aria-label={tr("web.dismiss")}><X size={14} /></button></div>}
    {dialog && project && <NewSessionDialog project={project} close={() => setDialog(false)} create={create} />}
  </div>;
}
