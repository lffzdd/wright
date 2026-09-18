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
import { applyEvent } from "./reducer";
import type { Attachment, Interaction, SessionSummary, Snapshot, UiEvent, ViewState } from "./types";

type InspectorTab = "changes" | "plan" | "details";

const initialView = (snapshot: Snapshot): ViewState => ({
  ...snapshot,
  notices: snapshot.notices ?? [],
  queued_commands: snapshot.queued_commands ?? [],
  queue_depth: (snapshot.queued_commands ?? []).length,
  usage: {
    ...snapshot.usage,
    request_prompt_tokens: snapshot.usage.request_prompt_tokens ?? 0,
    request_completion_tokens: snapshot.usage.request_completion_tokens ?? 0,
    request_total_tokens: snapshot.usage.request_total_tokens ?? 0,
  },
  seen: [],
  connection: "connecting",
});
const commandId = () => crypto.randomUUID();

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
        title={running ? "Cannot change model while turn is running" : "Change session model"}
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
              placeholder="Custom model…"
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
  const active = sessions.filter((item) => item.active);
  const history = sessions.filter((item) => !item.active);
  return (
    <aside className={`session-rail ${open ? "responsive-open" : ""}`}>
      <div className="rail-heading">
        <span>SESSIONS</span>
        <div className="rail-buttons">
          <button className="icon-button responsive-only" onClick={onClose} title="Close sessions (Esc)" aria-label="Close sessions"><X size={16} /></button>
          <button className="icon-button" onClick={onCreate} title="New session (⌘K)" aria-label="New session (⌘K)"><Plus size={16} /></button>
        </div>
      </div>
      <div className="session-list">
        {active.map((session) => (
          <button key={session.session_id} className={`session-item ${selected === session.session_id ? "selected" : ""}`} onClick={() => onSelect(session)}>
            <StatusDot status={session.status} />
            <span className="session-copy">
              <strong>{session.user_goal && session.user_goal !== "(interactive session)" ? session.user_goal : `Session ${session.session_id.slice(0, 6)}`}</strong>
              <small>{session.environment} · {session.model ?? "configured model"}</small>
            </span>
            {Boolean(session.pending_interactions) && <Warning size={15} weight="fill" className="warning-icon" />}
          </button>
        ))}
        {!active.length && <p className="empty-small">No active sessions</p>}
      </div>
      <div className="rail-heading history-heading"><span>HISTORY</span></div>
      <div className="session-list history-list">
        {history.map((session) => (
          <button
            key={session.session_id}
            className="session-item"
            disabled={session.recoverable === false}
            title={session.recoverable === false ? "Archived worktree was removed; checkpoint is retained for history only" : "Resume session"}
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

function ToolCard({ tool }: { tool: NonNullable<ViewState["active_turn"]>["tools"][number] }) {
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
      </div>}
    </section>
  );
}

function InteractionCard({ interaction, respond }: { interaction: Interaction; respond: (requestId: string, answer: unknown) => void }) {
  const [answer, setAnswer] = useState("");
  const isPermission = interaction.kind === "permission";
  return (
    <section className="interaction-card" role="alert">
      <div className="interaction-title"><Warning size={18} weight="fill" /><strong>{isPermission ? `Permission · ${interaction.tool_name}` : "Wright needs input"}</strong></div>
      <p>{isPermission ? interaction.subject || interaction.reason : interaction.question}</p>
      {isPermission && interaction.risk_flags && <small>Risk: {interaction.risk_flags}</small>}
      {isPermission && interaction.offer_always && interaction.remember_rule && <div className="permission-scope">
        <small><strong>Scope:</strong> <code>{interaction.remember_rule}</code></small>
        <small><strong>Persistence:</strong> {interaction.remember_persists ? "Across sessions" : "This session only"}</small>
        {interaction.revoke_hint && <small><strong>Revoke:</strong> {interaction.revoke_hint}</small>}
      </div>}
      {interaction.context && <small>{interaction.context}</small>}
      {isPermission ? <div className="interaction-actions">
        <button className="button secondary" onClick={() => respond(interaction.request_id, "n")}>Deny</button>
        <button className="button primary" onClick={() => respond(interaction.request_id, "y")}>Allow once</button>
        {interaction.offer_always && <button className="button subtle" onClick={() => respond(interaction.request_id, "a")}>Allow this scope</button>}
      </div> : <form className="ask-form" onSubmit={(event) => { event.preventDefault(); if (answer.trim()) respond(interaction.request_id, answer.trim()); }}>
        {interaction.options?.map((option) => <button type="button" className="option-button" key={option} onClick={() => respond(interaction.request_id, option)}>{option}</button>)}
        <input aria-label="Answer" value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="Type an answer…" />
        <button className="icon-button" aria-label="Submit answer"><PaperPlaneRight size={16} /></button>
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

function Timeline({ state, respond, cancelQueued }: { state: ViewState; respond: (requestId: string, answer: unknown) => void; cancelQueued: (commandId: string) => void }) {
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [state.history.length, state.active_turn?.content, state.active_turn?.tools.length]);
  return (
    <div className="timeline">
      {!state.history.length && !state.active_turn && <div className="welcome">
        <Sparkle size={30} weight="duotone" />
        <h2>Ready in this workspace</h2>
        <p>Ask Wright to inspect, change, or verify the project. Tools and diffs stay visible while it works.</p>
      </div>}
      {state.history.map((turn, index) => <div className="turn" key={`${index}-${turn.user.slice(0, 20)}`}>
        <div className="message user-message"><span>You</span>{turn.user && <p>{turn.user}</p>}<MessageAttachments sessionId={state.session.session_id} attachments={turn.attachments} /></div>
        <div className="message assistant-message"><span>Wright</span><MarkdownContent content={turn.assistant} /></div>
      </div>)}
      {state.active_turn && <div className="turn active-turn">
        <div className="message user-message"><span>You</span>{state.active_turn.prompt && <p>{state.active_turn.prompt}</p>}<MessageAttachments sessionId={state.session.session_id} attachments={state.active_turn.attachments} /></div>
        {state.active_turn.reasoning && <details className="reasoning" open={!state.active_turn.content}>
          <summary><Brain size={16} />Reasoning</summary>
          <div>{state.active_turn.reasoning}</div>
        </details>}
        {state.active_turn.tools.map((tool) => <ToolCard key={tool.call_id} tool={tool} />)}
        <div className="message assistant-message streaming"><span>Wright</span>{state.active_turn.content ? <MarkdownContent content={state.active_turn.content} /> : <span className="thinking"><i />Working…</span>}</div>
      </div>}
      {state.pending_interactions.map((item) => <InteractionCard key={item.request_id} interaction={item} respond={respond} />)}
      {state.queued_commands.length > 0 && <section className="queue-list" aria-label="Queued instructions">
        <strong>{state.queued_commands.length} queued instruction{state.queued_commands.length === 1 ? "" : "s"}</strong>
        {state.queued_commands.map((item) => <div className="queue-item" key={item.command_id}>
          <span>{item.prompt || "Attached images"}{item.attachments?.length ? ` · ${item.attachments.length} image${item.attachments.length === 1 ? "" : "s"}` : ""}</span>
          <button type="button" className="button subtle" onClick={() => cancelQueued(item.command_id)}>Remove</button>
        </div>)}
      </section>}
      {state.notices.slice(-5).map((notice) => <div className="system-notice" role="status" key={notice.id}>{notice.text}</div>)}
      <div ref={bottom} />
    </div>
  );
}

function Composer({ sessionId, running, submit, cancel }: { sessionId: string; running: boolean; submit: (prompt: string, attachmentIds: string[]) => void; cancel: () => void }) {
  const [value, setValue] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    setValue("");
    setAttachments([]);
    setError("");
  }, [sessionId]);
  const upload = async (files: FileList | File[]) => {
    const selected = Array.from(files);
    if (!selected.length) return;
    setUploading(true); setError("");
    try {
      const uploaded = await Promise.all(selected.map((file) => api.uploadAttachment(sessionId, file) as Promise<Attachment>));
      setAttachments((current) => [...current, ...uploaded]);
    } catch (reason) { setError(String(reason)); }
    finally { setUploading(false); }
  };
  const remove = async (attachment: Attachment) => {
    try { await api.deleteAttachment(sessionId, attachment.id); setAttachments((current) => current.filter((item) => item.id !== attachment.id)); }
    catch (reason) { setError(String(reason)); }
  };
  const send = (event: FormEvent) => {
    event.preventDefault();
    if (!value.trim() && !attachments.length) return;
    submit(value.trim(), attachments.map((attachment) => attachment.id));
    setValue("");
    setAttachments([]);
  };
  return <form className="composer" onSubmit={send} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); upload(event.dataTransfer.files); }}>
    <input ref={input} className="file-input" type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => { if (event.target.files) upload(event.target.files); event.target.value = ""; }} />
    {attachments.length > 0 && <div className="composer-attachments">{attachments.map((attachment) => <div className="composer-attachment" key={attachment.id}><img src={api.attachmentThumbnailUrl(sessionId, attachment.id)} alt="" /><span>{attachment.filename}</span><button type="button" onClick={() => remove(attachment)} aria-label={`Remove ${attachment.filename}`}><X size={12} /></button></div>)}</div>}
    <textarea aria-label="Message Wright" value={value} onPaste={(event) => { if (event.clipboardData.files.length) upload(event.clipboardData.files); }} onChange={(event) => setValue(event.target.value)} placeholder={running ? "Queue another instruction…" : "Message Wright…"} rows={2} onKeyDown={(event) => {
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
    }} />
    <div className="composer-actions">
      <button type="button" className="icon-button" title="Attach images" onClick={() => input.current?.click()}><Paperclip size={16} /></button>
      <span>{error || (uploading ? "Uploading images…" : "Drop, paste, or attach images · Enter to send · Shift+Enter for newline")}</span>
      {running && <button type="button" className="button stop" onClick={cancel}><Square size={13} weight="fill" />Stop</button>}
      <button className="button primary" disabled={uploading || (!value.trim() && !attachments.length)}><PaperPlaneRight size={15} />Send</button>
    </div>
  </form>;
}

function Inspector({ state, sessionId, open, close }: { state: ViewState; sessionId: string; open: boolean; close: () => void }) {
  const [tab, setTab] = useState<InspectorTab>("changes");
  const [changes, setChanges] = useState<Array<{ path: string; status: string }>>([]);
  const [warning, setWarning] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [patch, setPatch] = useState("");
  const [truncated, setTruncated] = useState(false);
  const refresh = useCallback(() => api.changes(sessionId).then((result) => { setChanges(result.changes); setWarning(result.local_warning); }).catch(() => setChanges([])), [sessionId]);
  useEffect(() => { refresh(); }, [refresh, state.session.status]);
  const openPatch = (path: string) => {
    setSelected(path);
    api.patch(sessionId, path).then((result) => { setPatch(result.patch); setTruncated(result.truncated); }).catch((error) => { setPatch(String(error)); setTruncated(false); });
  };
  return <aside className={`inspector ${open ? "responsive-open" : ""}`}>
    <div className="inspector-tabs" role="tablist">
      {(["changes", "plan", "details"] as InspectorTab[]).map((item) => <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item}</button>)}
      <button className="icon-button responsive-only inspector-close" onClick={close} aria-label="Close inspector"><X size={16} /></button>
    </div>
    {tab === "changes" && <div className="inspector-body">
      <div className="section-title"><span><FileCode size={16} />{changes.length} file{changes.length === 1 ? "" : "s"} changed</span><button className="icon-button" title="Refresh changes" onClick={refresh}><ArrowClockwise size={15} /></button></div>
      {warning && <div className="local-warning"><Warning size={15} />Local mode may include changes from before this session.</div>}
      <div className="change-list">{changes.map((change) => <button key={change.path} className={selected === change.path ? "selected" : ""} onClick={() => openPatch(change.path)}><b>{change.status}</b><span>{change.path}</span></button>)}</div>
      {selected && <div className="patch">{truncated ? <p className="empty-small">Binary or patch larger than 1 MiB. Metadata only.</p> : <DiffViewer patch={patch} filename={selected} />}</div>}
      {!changes.length && <p className="empty-small">Working tree is clean.</p>}
    </div>}
    {tab === "plan" && <div className="inspector-body">
      <div className="section-title"><span>Plan</span><small>{state.plan.status ?? "empty"}</small></div>
      {state.plan.objective && <p className="plan-objective">{state.plan.objective}</p>}
      <ol className="plan-list">{state.plan.steps?.map((step) => <li key={step.id} className={`plan-${step.status}`}><span>{step.status === "completed" ? <Check size={13} /> : <i />}</span><div><strong>{step.title}</strong>{step.note && <small>{step.note}</small>}</div></li>)}</ol>
      {!state.plan.steps?.length && <p className="empty-small">No active plan for this turn.</p>}
    </div>}
    {tab === "details" && <div className="inspector-body details-grid">
      <label>Session</label><code>{state.session.session_id}</code>
      <label>Environment</label><span>{state.session.environment}</span>
      <label>Branch</label><code>{state.session.branch_name || "current checkout"}</code>
      <label>Execution root</label><code>{state.session.execution_root}</code>
      <label>Request usage</label><span>{state.usage.request_prompt_tokens.toLocaleString()} in · {state.usage.request_completion_tokens.toLocaleString()} out</span>
      <label>Task total</label><span>{state.usage.prompt_tokens.toLocaleString()} in · {state.usage.completion_tokens.toLocaleString()} out · {state.usage.total_tokens.toLocaleString()} total</span>
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
    <div className="dialog-head"><div><span>NEW SESSION</span><h2 id="new-session-title">Choose an execution environment</h2></div><button className="icon-button" onClick={close} title="Close (Esc)" aria-label="Close"><X size={18} /></button></div>
    <div className="environment-grid" role="group" aria-label="Execution environment">
      <button aria-pressed={environment === "worktree"} className={environment === "worktree" ? "selected" : ""} disabled={!project.git} onClick={() => setEnvironment("worktree")}><GitBranch size={22} /><strong>Isolated worktree</strong><span>Starts from current HEAD. Uncommitted checkout changes are not copied.</span>{!dirtyCheckout && <em>Recommended</em>}</button>
      <button aria-pressed={environment === "local"} className={environment === "local" ? "selected" : ""} onClick={() => setEnvironment("local")}><TerminalWindow size={22} /><strong>Current checkout</strong><span>Uses existing files, including current uncommitted changes. One active session max.</span>{dirtyCheckout && <em>Recommended for current changes</em>}</button>
    </div>
    {models.length > 0 && <div className="dialog-field">
      <label className="field-label" htmlFor="first-model">Model <span>optional</span></label>
      <select id="first-model" className="dialog-select" value={model} onChange={(event) => setModel(event.target.value)}>
        {models.map((m) => <option key={m} value={m}>{m}</option>)}
      </select>
    </div>}
    <label className="field-label" htmlFor="first-prompt">First instruction <span>optional</span></label>
    <textarea autoFocus id="first-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="What should Wright work on?" rows={3} />
    {error && <p className="form-error">{error}</p>}
    <div className="dialog-actions"><button className="button secondary" onClick={close}>Cancel</button><button className="button primary" disabled={busy} onClick={() => { setBusy(true); setError(""); create(environment, prompt, model).catch((reason) => { setError(String(reason)); setBusy(false); }); }}>{busy ? <CircleNotch className="spin" size={15} /> : <Plus size={15} />}Create session</button></div>
  </section></div>;
}

export default function App() {
  const [project, setProject] = useState<Record<string, unknown> | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [state, setState] = useState<ViewState | null>(null);
  const [dialog, setDialog] = useState(false);
  const [fatal, setFatal] = useState("");
  const [railOpen, setRailOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const socket = useRef<WebSocket | null>(null);

  const refreshSessions = useCallback(async () => {
    const items = await api.sessions();
    setSessions(items);
    return items;
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
    socket.current?.close();
    let disposed = false;
    let retryTimer: number | undefined;
    const connect = (snapshot: Snapshot, attempt = 0) => {
      if (disposed) return;
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${protocol}://${location.host}/api/v1/sessions/${selected}/stream?stream_id=${snapshot.stream_id}&last_seq=${snapshot.last_seq}`);
      socket.current = ws;
      ws.onopen = () => setState((current) => current ? { ...current, connection: "connected" } : current);
      ws.onclose = () => {
        if (disposed) return;
        setState((current) => current ? { ...current, connection: "disconnected" } : current);
        retryTimer = window.setTimeout(() => {
          api.snapshot(selected).then((fresh) => {
            if (disposed) return;
            setState(initialView(fresh));
            connect(fresh, attempt + 1);
          }).catch(() => connect(snapshot, attempt + 1));
        }, Math.min(10_000, 500 * (2 ** Math.min(attempt, 5))));
      };
      ws.onmessage = (message) => {
        const event = JSON.parse(message.data) as UiEvent | { type: "snapshot_required"; snapshot: Snapshot };
        if ("snapshot" in event) {
          setState({ ...initialView(event.snapshot), connection: "connected" });
          refreshSessions().catch(() => undefined);
        } else {
          setState((current) => current ? applyEvent(current, event as UiEvent) : current);
          if ([
            "turn.started", "turn.completed", "turn.failed", "turn.cancelled",
            "interaction.requested", "interaction.resolved", "session.status_changed",
          ].includes(event.type)) {
            refreshSessions().catch(() => undefined);
          }
        }
      };
    };
    api.snapshot(selected).then((snapshot) => {
      if (disposed) return;
      setState(initialView(snapshot));
      connect(snapshot);
    }).catch((error) => setFatal(String(error)));
    return () => { disposed = true; if (retryTimer !== undefined) window.clearTimeout(retryTimer); socket.current?.close(); };
  }, [selected, refreshSessions]);

  useEffect(() => {
    const timer = window.setInterval(() => refreshSessions().catch(() => undefined), 5_000);
    return () => window.clearInterval(timer);
  }, [refreshSessions]);

  const send = (payload: Record<string, unknown>) => {
    if (socket.current?.readyState !== WebSocket.OPEN) { setFatal("Session stream is disconnected. Refresh to reconnect."); return; }
    socket.current.send(JSON.stringify(payload));
  };
  const create = async (environment: string, prompt: string, model?: string) => {
    const snapshot = await api.create({ environment, prompt: prompt || undefined, model: model || undefined });
    await refreshSessions();
    setSelected(snapshot.session.session_id);
    setDialog(false);
  };
  const changeModel = async (newModel: string) => {
    if (!selected) return;
    try {
      await api.setModel(selected, newModel);
      setState((current) => current ? { ...current, session: { ...current.session, model: newModel } } : current);
      refreshSessions().catch(() => undefined);
    } catch (error) {
      setFatal(String(error));
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
  const running = state?.session.status === "running";
  const contextPercent = state ? Math.min(100, Math.round((state.usage.context_tokens / Math.max(1, state.usage.context_limit)) * 100)) : 0;

  if (fatal && !project) return <main className="fatal"><Warning size={28} /><h1>Wright Web could not start</h1><p>{fatal}</p><button className="button primary" onClick={() => location.reload()}>Reload</button></main>;
  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><span className="brand-mark">W</span><div><strong>Wright</strong><small>{String(project?.name ?? "Local Web")}</small></div></div>
      <div className="top-status">
        <span className={`connection ${state?.connection ?? "connecting"}`}><StatusDot status={state?.connection ?? "connecting"} />{state?.connection ?? "connecting"}</span>
        <ModelSelector
          currentModel={state?.session.model ?? String(project?.default_model ?? "configured model")}
          models={Array.isArray(project?.models) ? (project.models as string[]) : []}
          running={Boolean(running)}
          onSelect={changeModel}
        />
        <span className="context-meter" title={`${state?.usage.context_tokens ?? 0} / ${state?.usage.context_limit ?? 0} context tokens`}><i style={{ width: `${contextPercent}%` }} />Context {contextPercent}%</span>
        <span>{sessions.filter((item) => item.active).length}/{project?.capacity as number ?? 0} active</span>
      </div>
      <button className="icon-button mobile-sessions" onClick={() => setRailOpen(true)} title="Open sessions (⌘B)" aria-label="Open sessions"><SidebarSimple size={18} /></button>
    </header>
    <div className="workspace-grid">
      <SessionRail sessions={sessions} selected={selected} onSelect={(session) => selectSession(session).catch((error) => setFatal(String(error)))} onCreate={() => setDialog(true)} open={railOpen} onClose={() => setRailOpen(false)} />
      <main className="conversation">
        {state ? <>
          <div className="conversation-head"><div><span className="eyebrow">{state.session.environment === "worktree" ? "ISOLATED WORKTREE" : "CURRENT CHECKOUT"}</span><h1>{state.session.user_goal && state.session.user_goal !== "(interactive session)" ? state.session.user_goal : `Session ${state.session.session_id.slice(0, 6)}`}</h1></div><div className="session-actions"><button className="icon-button inspector-trigger" title="Open inspector (⌘J)" onClick={() => setInspectorOpen(true)}><FileCode size={17} /></button><button className="icon-button" title="Close session" onClick={() => api.close(state.session.session_id).then(() => { setSelected(null); setState(null); refreshSessions(); }).catch((error) => setFatal(String(error)))}><SidebarSimple size={17} /></button><button className="icon-button" title="Archive session" onClick={() => archiveSession().catch((error) => setFatal(String(error)))}><Archive size={17} /></button></div></div>
          <Timeline
            state={state}
            respond={(requestId, answer) => send({ type: "interaction.respond", command_id: commandId(), request_id: requestId, answer })}
            cancelQueued={(targetCommandId) => send({ type: "turn.cancel_queued", command_id: commandId(), target_command_id: targetCommandId })}
          />
          <Composer sessionId={state.session.session_id} running={Boolean(running)} submit={(prompt, attachmentIds) => send({ type: "turn.submit", command_id: commandId(), prompt, attachment_ids: attachmentIds })} cancel={() => send({ type: "turn.cancel", command_id: commandId() })} />
        </> : <div className="no-session"><TerminalWindow size={36} weight="duotone" /><h2>No session selected</h2><p>Create an isolated task or resume a saved checkpoint.</p><button className="button primary" onClick={() => setDialog(true)}><Plus size={15} />New session</button></div>}
      </main>
      {state && selected ? <Inspector state={state} sessionId={selected} open={inspectorOpen} close={() => setInspectorOpen(false)} /> : <aside className="inspector empty-inspector"><FileCode size={24} /><p>Changes, plan, and details appear here.</p></aside>}
    </div>
    {fatal && project && <div className="toast" role="alert"><Warning size={16} /><span>{fatal}</span><button className="icon-button" onClick={() => setFatal("")} aria-label="Dismiss"><X size={14} /></button></div>}
    {dialog && project && <NewSessionDialog project={project} close={() => setDialog(false)} create={create} />}
  </div>;
}
