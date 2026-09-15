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
import { api, bootstrap } from "./api";
import { applyEvent } from "./reducer";
import type { Interaction, SessionSummary, Snapshot, UiEvent, ViewState } from "./types";

type InspectorTab = "changes" | "plan" | "details";

const initialView = (snapshot: Snapshot): ViewState => ({ ...snapshot, seen: [], connection: "connecting" });
const commandId = () => crypto.randomUUID();

function StatusDot({ status }: { status: string }) {
  return <span className={`status-dot status-${status}`} aria-hidden="true" />;
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
        <div className="rail-buttons"><button className="icon-button responsive-only" onClick={onClose} title="Close sessions" aria-label="Close sessions"><X size={16} /></button><button className="icon-button" onClick={onCreate} title="New session" aria-label="New session"><Plus size={16} /></button></div>
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
  return (
    <section className="tool-card">
      <button className="tool-summary" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`tool-icon ${done ? (tool.ok ? "success" : "danger") : "running"}`}>
          {done ? (tool.ok ? <Check size={14} /> : <X size={14} />) : <CircleNotch className="spin" size={14} />}
        </span>
        <Code size={16} />
        <strong>{tool.name}</strong>
        <span>{done ? (tool.ok ? "completed" : "failed") : "running"}</span>
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
      {interaction.context && <small>{interaction.context}</small>}
      {isPermission ? <div className="interaction-actions">
        <button className="button secondary" onClick={() => respond(interaction.request_id, "n")}>Deny</button>
        <button className="button primary" onClick={() => respond(interaction.request_id, "y")}>Allow once</button>
        {interaction.offer_always && <button className="button subtle" onClick={() => respond(interaction.request_id, "a")}>Always allow</button>}
      </div> : <form className="ask-form" onSubmit={(event) => { event.preventDefault(); if (answer.trim()) respond(interaction.request_id, answer.trim()); }}>
        {interaction.options?.map((option) => <button type="button" className="option-button" key={option} onClick={() => respond(interaction.request_id, option)}>{option}</button>)}
        <input aria-label="Answer" value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="Type an answer…" />
        <button className="icon-button" aria-label="Submit answer"><PaperPlaneRight size={16} /></button>
      </form>}
    </section>
  );
}

function Timeline({ state, respond }: { state: ViewState; respond: (requestId: string, answer: unknown) => void }) {
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
        <div className="message user-message"><span>You</span><p>{turn.user}</p></div>
        <div className="message assistant-message"><span>Wright</span><div className="answer-copy">{turn.assistant}</div></div>
      </div>)}
      {state.active_turn && <div className="turn active-turn">
        <div className="message user-message"><span>You</span><p>{state.active_turn.prompt}</p></div>
        {state.active_turn.reasoning && <details className="reasoning" open={!state.active_turn.content}>
          <summary><Brain size={16} />Reasoning</summary>
          <div>{state.active_turn.reasoning}</div>
        </details>}
        {state.active_turn.tools.map((tool) => <ToolCard key={tool.call_id} tool={tool} />)}
        <div className="message assistant-message streaming"><span>Wright</span><div className="answer-copy">{state.active_turn.content || <span className="thinking"><i />Working…</span>}</div></div>
      </div>}
      {state.pending_interactions.map((item) => <InteractionCard key={item.request_id} interaction={item} respond={respond} />)}
      <div ref={bottom} />
    </div>
  );
}

function Composer({ running, submit, cancel }: { running: boolean; submit: (prompt: string) => void; cancel: () => void }) {
  const [value, setValue] = useState("");
  const send = (event: FormEvent) => {
    event.preventDefault();
    if (!value.trim()) return;
    submit(value.trim());
    setValue("");
  };
  return <form className="composer" onSubmit={send}>
    <textarea aria-label="Message Wright" value={value} onChange={(event) => setValue(event.target.value)} placeholder={running ? "Queue another instruction…" : "Message Wright…"} rows={2} onKeyDown={(event) => {
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
    }} />
    <div className="composer-actions">
      <span>Enter to send · Shift+Enter for newline</span>
      {running && <button type="button" className="button stop" onClick={cancel}><Square size={13} weight="fill" />Stop</button>}
      <button className="button primary" disabled={!value.trim()}><PaperPlaneRight size={15} />Send</button>
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
      {selected && <div className="patch"><div className="patch-title">{selected}</div>{truncated ? <p>Binary or patch larger than 1 MiB. Metadata only.</p> : <pre>{patch || "No textual diff."}</pre>}</div>}
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
      <label>Request usage</label><span>{state.usage.prompt_tokens.toLocaleString()} in · {state.usage.completion_tokens.toLocaleString()} out</span>
      <label>Task total</label><span>{state.usage.total_tokens.toLocaleString()} tokens</span>
    </div>}
  </aside>;
}

function NewSessionDialog({ project, close, create }: { project: Record<string, unknown>; close: () => void; create: (environment: string, prompt: string) => Promise<void> }) {
  const defaultEnvironment = String(project.default_environment ?? "local");
  const [environment, setEnvironment] = useState(defaultEnvironment);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return <div className="dialog-backdrop" role="presentation"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="new-session-title">
    <div className="dialog-head"><div><span>NEW SESSION</span><h2 id="new-session-title">Choose an execution environment</h2></div><button className="icon-button" onClick={close} aria-label="Close"><X size={18} /></button></div>
    <div className="environment-grid">
      <button className={environment === "worktree" ? "selected" : ""} disabled={!project.git} onClick={() => setEnvironment("worktree")}><GitBranch size={22} /><strong>Isolated worktree</strong><span>Starts from current HEAD. Uncommitted checkout changes are not copied.</span><em>Recommended</em></button>
      <button className={environment === "local" ? "selected" : ""} onClick={() => setEnvironment("local")}><TerminalWindow size={22} /><strong>Current checkout</strong><span>Uses existing files, including current uncommitted changes. One active session max.</span></button>
    </div>
    <label className="field-label" htmlFor="first-prompt">First instruction <span>optional</span></label>
    <textarea id="first-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="What should Wright work on?" rows={3} />
    {error && <p className="form-error">{error}</p>}
    <div className="dialog-actions"><button className="button secondary" onClick={close}>Cancel</button><button className="button primary" disabled={busy} onClick={() => { setBusy(true); setError(""); create(environment, prompt).catch((reason) => { setError(String(reason)); setBusy(false); }); }}>{busy ? <CircleNotch className="spin" size={15} /> : <Plus size={15} />}Create session</button></div>
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
    api.snapshot(selected).then((snapshot) => {
      if (disposed) return;
      setState(initialView(snapshot));
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${protocol}://${location.host}/api/v1/sessions/${selected}/stream?stream_id=${snapshot.stream_id}&last_seq=${snapshot.last_seq}`);
      socket.current = ws;
      ws.onopen = () => setState((current) => current ? { ...current, connection: "connected" } : current);
      ws.onclose = () => setState((current) => current ? { ...current, connection: "disconnected" } : current);
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
    }).catch((error) => setFatal(String(error)));
    return () => { disposed = true; socket.current?.close(); };
  }, [selected, refreshSessions]);

  const send = (payload: Record<string, unknown>) => {
    if (socket.current?.readyState !== WebSocket.OPEN) { setFatal("Session stream is disconnected. Refresh to reconnect."); return; }
    socket.current.send(JSON.stringify(payload));
  };
  const create = async (environment: string, prompt: string) => {
    const snapshot = await api.create({ environment, prompt: prompt || undefined });
    await refreshSessions();
    setSelected(snapshot.session.session_id);
    setDialog(false);
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
        <span>{state?.session.model ?? "configured model"}</span>
        <span className="context-meter" title={`${state?.usage.context_tokens ?? 0} / ${state?.usage.context_limit ?? 0} context tokens`}><i style={{ width: `${contextPercent}%` }} />Context {contextPercent}%</span>
        <span>{sessions.filter((item) => item.active).length}/{project?.capacity as number ?? 0} active</span>
      </div>
      <button className="icon-button mobile-sessions" onClick={() => setRailOpen(true)} aria-label="Open sessions"><SidebarSimple size={18} /></button>
    </header>
    <div className="workspace-grid">
      <SessionRail sessions={sessions} selected={selected} onSelect={(session) => selectSession(session).catch((error) => setFatal(String(error)))} onCreate={() => setDialog(true)} open={railOpen} onClose={() => setRailOpen(false)} />
      <main className="conversation">
        {state ? <>
          <div className="conversation-head"><div><span className="eyebrow">{state.session.environment === "worktree" ? "ISOLATED WORKTREE" : "CURRENT CHECKOUT"}</span><h1>{state.session.user_goal && state.session.user_goal !== "(interactive session)" ? state.session.user_goal : `Session ${state.session.session_id.slice(0, 6)}`}</h1></div><div className="session-actions"><button className="icon-button inspector-trigger" title="Open inspector" onClick={() => setInspectorOpen(true)}><FileCode size={17} /></button><button className="icon-button" title="Close session" onClick={() => api.close(state.session.session_id).then(() => { setSelected(null); setState(null); refreshSessions(); }).catch((error) => setFatal(String(error)))}><SidebarSimple size={17} /></button><button className="icon-button" title="Archive session" onClick={() => api.archive(state.session.session_id).then(() => { setSelected(null); setState(null); refreshSessions(); }).catch((error) => setFatal(String(error)))}><Archive size={17} /></button></div></div>
          <Timeline state={state} respond={(requestId, answer) => send({ type: "interaction.respond", command_id: commandId(), request_id: requestId, answer })} />
          <Composer running={Boolean(running)} submit={(prompt) => send({ type: "turn.submit", command_id: commandId(), prompt })} cancel={() => send({ type: "turn.cancel", command_id: commandId() })} />
        </> : <div className="no-session"><TerminalWindow size={36} weight="duotone" /><h2>No session selected</h2><p>Create an isolated task or resume a saved checkpoint.</p><button className="button primary" onClick={() => setDialog(true)}><Plus size={15} />New session</button></div>}
      </main>
      {state && selected ? <Inspector state={state} sessionId={selected} open={inspectorOpen} close={() => setInspectorOpen(false)} /> : <aside className="inspector empty-inspector"><FileCode size={24} /><p>Changes, plan, and details appear here.</p></aside>}
    </div>
    {fatal && project && <div className="toast" role="alert"><Warning size={16} /><span>{fatal}</span><button className="icon-button" onClick={() => setFatal("")} aria-label="Dismiss"><X size={14} /></button></div>}
    {dialog && project && <NewSessionDialog project={project} close={() => setDialog(false)} create={create} />}
  </div>;
}
