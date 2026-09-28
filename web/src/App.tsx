import { Archive, Brain, Clock, Folder, Gear, GitBranch, MagnifyingGlass, Scroll, SidebarSimple, Sun, Warning, X } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, bootstrap } from "./api";
import { getLocale, setLocale, useT } from "./i18n";
import { parseEvent } from "./protocol";
import { applyEvent } from "./reducer";
import type { SessionSummary, Snapshot, ViewState } from "./types";
import { MemoryDialog, RulesDialog, SchedulesDialog, SearchDialog, SettingsDialog } from "./workspace/panels";
import { VisualFixture } from "./workspace/visual-fixture";
import {
  Composer, Inspector, NewSessionDialog, SessionRail, Timeline, emptyDraft,
  type Draft,
} from "./workspace/widgets";

export type { Draft };
export { Composer, Inspector, InteractionCard, NewSessionDialog, visibleModels } from "./workspace/widgets";

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

type WorkspaceRow = { project_id: string; name: string; root: string; selected?: boolean; exists?: boolean };
type Panel = null | "search" | "memory" | "rules" | "schedules" | "settings";

export default function App() {
  const tr = useT();
  const [project, setProject] = useState<Record<string, unknown> | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [state, setState] = useState<ViewState | null>(null);
  const [dialog, setDialog] = useState(false);
  const [fatal, setFatal] = useState("");
  const [railOpen, setRailOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [narrowInspector, setNarrowInspector] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [workspaces, setWorkspaces] = useState<WorkspaceRow[]>([]);
  const [panel, setPanel] = useState<Panel>(null);
  const [focusPath, setFocusPath] = useState<string | null>(null);
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
  const workspaceId = useRef<string | null>(null);
  const launchId = useRef<string | null>(null);
  const updateDraft = useCallback((sessionId: string, patch: (current: Draft) => Draft) => {
    setDrafts((current) => {
      const previous = current[sessionId] ?? emptyDraft();
      return { ...current, [sessionId]: patch(previous) };
    });
  }, []);

  const refreshSessions = useCallback(async () => {
    const id = workspaceId.current;
    const items = id && launchId.current && id !== launchId.current
      ? await api.workspaceSessions(id)
      : await api.sessions();
    setSessions(items);
    return items;
  }, []);

  const applyTheme = (next: "dark" | "light") => {
    setTheme(next);
    document.documentElement.classList.toggle("light", next === "light");
    document.documentElement.classList.toggle("dark", next !== "light");
  };

  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("visual") === "fixture") {
      setLocale("en");
      applyTheme("dark");
      return;
    }
    document.documentElement.classList.add("dark");
    api.preferences().then((prefs) => {
      setLocale(prefs.interface_language);
      applyTheme(prefs.theme);
      setInspectorOpen(prefs.inspector_open);
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setDialog(true);
      } else if ((event.metaKey || event.ctrlKey) && event.key === ".") {
        event.preventDefault();
        const id = cancelId.current ?? crypto.randomUUID();
        cancelId.current = id;
        sendCommand({ type: "turn.cancel", command_id: id }).then((outcome) => {
          if (outcome === "accepted" || outcome === "rejected") cancelId.current = null;
        });
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "b") {
        event.preventDefault();
        setRailOpen((prev) => !prev);
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "j") {
        event.preventDefault();
        setInspectorOpen((prev) => !prev);
        setNarrowInspector((prev) => !prev);
      } else if (event.key === "Escape") {
        setDialog(false);
        setPanel(null);
        setRailOpen(false);
        setNarrowInspector(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  useEffect(() => {
    bootstrap().then(async () => {
      const [projectData, registry] = await Promise.all([
        api.project(),
        api.workspaces().catch(() => null),
      ]);
      setProject(projectData);
      launchId.current = String(projectData.project_id ?? "");
      if (registry) {
        setWorkspaces(registry.projects.map((item) => ({ ...item, selected: item.project_id === registry.selected_project_id })));
        workspaceId.current = registry.selected_project_id;
      }
      const items = await refreshSessions();
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
          return applyEvent(current, event);
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
    const registered = workspaceId.current;
    const snapshot = registered && launchId.current && registered !== launchId.current
      ? await api.createInWorkspace(registered, { environment, prompt: prompt || undefined, model: model || undefined })
      : await api.create({ environment, prompt: prompt || undefined, model: model || undefined });
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
    if (!window.confirm(`Archive ${state.session.user_goal || state.session.session_id}? A clean isolated worktree may be removed. The project directory stays.`)) return;
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
      const registered = workspaceId.current;
      const foreign = Boolean(registered && launchId.current && registered !== launchId.current);
      const snapshot = foreign
        ? await api.createInWorkspace(registered as string, { resume_session_id: session.session_id })
        : await api.create({ resume_session_id: session.session_id });
      await refreshSessions();
      setSelected(snapshot.session.session_id);
    } else setSelected(session.session_id);
    setRailOpen(false);
  };
  const savePreference = async (patch: { theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string }) => {
    const saved = await api.setPreference(patch);
    if (patch.theme) applyTheme(saved.theme);
    if (patch.inspector_open !== undefined) setInspectorOpen(saved.inspector_open);
    if (patch.interface_language) setLocale(saved.interface_language);
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
  const draft = selected ? (drafts[selected] ?? emptyDraft()) : emptyDraft();
  const activeCount = sessions.filter((item) => item.active).length;
  const branch = String(state?.session.branch_name ?? project?.branch ?? "");
  const dirty = Number(project?.uncommitted_count ?? 0);
  const projectName = String(project?.name ?? tr("web.local"));
  const launchProject = String(project?.project_id ?? "");

  if (new URLSearchParams(window.location.search).get("visual") === "fixture") return <VisualFixture />;
  if (fatal && !project) return <main className="fatal"><Warning size={28} /><h1>{tr("web.fatal_title")}</h1><p>{fatal}</p><button className="button primary" onClick={() => location.reload()}>{tr("web.reload")}</button></main>;
  const connection = state?.connection ?? "connecting";
  const statusClass = execution === "waiting_for_input" ? "waiting" : execution === "failed" ? "failed" : running ? "running" : "";
  const breakdown = state?.context_breakdown;
  const contextWidth = breakdown?.limit ? Math.min(100, Math.round(((breakdown.total ?? 0) / breakdown.limit) * 100)) : 0;
  return <div className="app-viewport">
    <header className="topbar">
      <div className="brand">
        <button className="icon-button rail-toggle" onClick={() => setRailOpen(true)} aria-label={tr("web.open_sessions")}><SidebarSimple size={14} /></button>
        <span className="brand-mark">W</span>
        <strong>Wright</strong>
        <span className="sep">/</span>
        <span className="crumb" title={String(project?.project_root ?? "")}>{projectName}</span>
      </div>
      <div className="top-center">
        {branch && <div className="branch-pill"><GitBranch size={12} /><span>{branch}</span>{dirty > 0 ? <span className="dirty">{dirty} uncommitted</span> : null}</div>}
        <button type="button" className="search-trigger" onClick={() => setPanel("search")}><MagnifyingGlass size={14} /><span>{tr("web.search")}</span></button>
      </div>
      <div className="top-status">
        <span className="active-count">{tr("web.active", { count: `${activeCount}/${project?.capacity as number ?? 0}` })}</span>
        <select aria-label={tr("web.language_label")} title={tr("web.language_note")} value={getLocale()} onChange={(event) => {
          const next = setLocale(event.target.value);
          api.setPreference({ interface_language: next }).catch(() => undefined);
        }}>
          <option value="en">EN</option>
          <option value="zh-CN">中文</option>
        </select>
        <button type="button" className="icon-button" title={tr("web.theme")} onClick={() => savePreference({ theme: theme === "dark" ? "light" : "dark" }).catch((error) => setFatal(String(error)))}><Sun size={14} /></button>
        <button type="button" className="icon-button" aria-label={tr("web.open_inspector")} onClick={() => { setInspectorOpen(true); setNarrowInspector(true); }}><SidebarSimple size={14} /></button>
      </div>
    </header>
    <div className="workspace-grid">
      <SessionRail sessions={sessions} selected={selected} onSelect={(session) => selectSession(session).catch((error) => setFatal(String(error)))} onCreate={() => setDialog(true)} open={railOpen} footer={[state?.session.model, state?.connection].filter(Boolean).join(" · ")}>
        <div>
          <div className="section-label"><span>{tr("web.workspaces")}</span></div>
          {workspaces.map((item) => <button type="button" key={item.project_id} className={`nav-row ${item.selected ? "selected" : ""}`} onClick={() => {
            workspaceId.current = item.project_id;
            setWorkspaces((current) => current.map((row) => ({ ...row, selected: row.project_id === item.project_id })));
            api.selectWorkspace(item.project_id).then(() => refreshSessions()).catch((error) => setFatal(String(error)));
          }}><Folder size={14} />{item.name}</button>)}
          {!workspaces.length && <p className="empty-small">{tr("web.launch_project")}</p>}
          <button type="button" className="nav-row" onClick={() => {
            const path = window.prompt("Project directory to register. Files stay on disk, and a running task keeps its execution root.");
            if (!path) return;
            api.registerWorkspace(path).then(() => api.workspaces()).then((registry) => setWorkspaces(registry.projects)).catch((error) => setFatal(String(error)));
          }}>{tr("web.register_workspace")}</button>
          <button type="button" className="nav-row" onClick={() => {
            const current = workspaces.find((item) => item.selected) ?? workspaces[0];
            if (!current) return;
            if (!window.confirm(`Unregister ${current.name}? This removes it from the list only. Files and task data stay.`)) return;
            api.unregisterWorkspace(current.project_id).then(() => api.workspaces()).then((registry) => {
              setWorkspaces(registry.projects);
              workspaceId.current = registry.selected_project_id;
              return refreshSessions();
            }).catch((error) => setFatal(String(error)));
          }}>{tr("web.unregister_selected")}</button>
        </div>
        <div>
          <div className="section-label"><span>{tr("web.capabilities")}</span></div>
          <button type="button" className="nav-row" onClick={() => setPanel("memory")}><Brain size={14} />{tr("web.memory")}</button>
          <button type="button" className="nav-row" onClick={() => setPanel("rules")}><Scroll size={14} />{tr("web.rules")}</button>
          <button type="button" className="nav-row" onClick={() => setPanel("schedules")}><Clock size={14} />{tr("web.schedules")}</button>
          <button type="button" className="nav-row" onClick={() => setPanel("settings")}><Gear size={14} />{tr("web.settings")}</button>
        </div>
      </SessionRail>
      <main className="conversation">
        {state ? <>
          <div className="conversation-head">
            <div className="task-heading">
              <span className="task-id-pill">{state.session.session_id.slice(0, 8)}</span>
              <h1>{state.session.user_goal && state.session.user_goal !== "(interactive session)" ? state.session.user_goal : tr("web.session_label", { id: state.session.session_id.slice(0, 6) })}</h1>
            </div>
            <div className="task-status">
              <span className={`status-badge ${statusClass}`}><span className={`dot ${running ? "pulse" : ""}`} />{execution ? tr(`web.status.${execution}`) : tr(`web.connection.${connection}`)}</span>
              {typeof breakdown?.total === "number" && <div className="context-meter" title={tr("web.estimate")}>
                <span className="figures"><span>{breakdown.total} / {breakdown.limit ?? "unknown"}</span><span className="muted">{contextWidth}%</span></span>
                <span className="meter slim"><i style={{ width: `${contextWidth}%` }} /></span>
              </div>}
              <button className="icon-button" title={tr("web.archive")} aria-label={tr("web.archive")} onClick={() => archiveSession().catch((error) => setFatal(String(error)))}><Archive size={14} /></button>
              <button className="icon-button" title={tr("web.close_session")} aria-label={tr("web.close_session")} onClick={() => {
                if (!window.confirm(`Close ${state.session.user_goal || state.session.session_id}? The task stops and the checkpoint stays.`)) return;
                api.close(state.session.session_id).then(() => { setSelected(null); setState(null); refreshSessions(); }).catch((error) => setFatal(String(error)));
              }}><X size={14} /></button>
              <button className="icon-button" aria-label={tr("web.open_inspector")} onClick={() => { setInspectorOpen(true); setNarrowInspector(true); }}><SidebarSimple size={14} /></button>
            </div>
          </div>
          <Timeline
            state={state}
            respond={(requestId, answer) => {
              const existing = respondIds.current.get(requestId);
              const id = existing ?? crypto.randomUUID();
              respondIds.current.set(requestId, id);
              return sendCommand({ type: "interaction.respond", command_id: id, request_id: requestId, answer }).then((outcome) => {
                if (outcome === "accepted" || outcome === "rejected") respondIds.current.delete(requestId);
                return outcome === "accepted";
              });
            }}
            cancelQueued={(targetCommandId) => {
              const id = crypto.randomUUID();
              sendCommand({ type: "turn.cancel_queued", command_id: id, target_command_id: targetCommandId }).catch(() => undefined);
            }}
          />
          <div className="composer-wrap">
            <Composer
              sessionId={state.session.session_id}
              connection={state.session.lifecycle === "closing" || state.session.lifecycle === "closed" || state.session.status === "closing" || state.session.status === "closed" ? "closed" : state.connection}
              draft={draft}
              updateDraft={updateDraft}
              running={Boolean(running || directoryQueued)}
              models={Array.isArray(project?.models) ? project.models as string[] : []}
              currentModel={state.session.model ?? ""}
              onModel={(model) => changeModel(model).catch((error) => setFatal(String(error)))}
              interactionMode={state.session.interaction_mode ?? "agent"}
              permissionMode={state.session.permission_mode ?? "default"}
              onPolicy={async (body) => {
                await api.policy(state.session.session_id, body);
                setState((current) => {
                  if (!current || current.session.session_id !== state.session.session_id) return current;
                  const interaction = body.interaction_mode;
                  const permission = body.permission_mode;
                  return {
                    ...current,
                    session: {
                      ...current.session,
                      ...(interaction === "agent" || interaction === "plan" || interaction === "ask" ? { interaction_mode: interaction } : {}),
                      ...(permission ? { permission_mode: permission } : {}),
                    },
                  };
                });
              }}
              submit={(commandId, prompt, attachmentIds, documentIds) => sendCommand({ type: "turn.submit", command_id: commandId, prompt, attachment_ids: attachmentIds, document_ids: documentIds ?? [] })}
              cancel={async () => {
                const id = cancelId.current ?? crypto.randomUUID();
                cancelId.current = id;
                const outcome = await sendCommand({ type: "turn.cancel", command_id: id });
                if (outcome === "accepted" || outcome === "rejected") cancelId.current = null;
              }}
            />
          </div>
        </> : <div className="no-session"><h2>{tr("web.no_session")}</h2><p>{tr("web.no_session_help")}</p><button className="button primary" onClick={() => setDialog(true)}>{tr("web.new_session")}</button></div>}
      </main>
      {state && selected ? <Inspector state={state} sessionId={selected} open={inspectorOpen} narrow={narrowInspector} focusPath={focusPath} close={() => { setInspectorOpen(false); setNarrowInspector(false); api.setPreference({ inspector_open: false }).catch(() => undefined); }} /> : <aside className={`inspector ${inspectorOpen ? "" : "is-closed"} ${narrowInspector ? "narrow-open" : ""}`}><p className="empty-small">{tr("web.inspector_empty")}</p></aside>}
    </div>
    {railOpen && <button className="sidebar-backdrop" aria-label={tr("web.close_sessions")} onClick={() => setRailOpen(false)} />}
    {fatal && project && <div className="toast" role="alert"><Warning size={16} /><span>{fatal}</span><button className="icon-button" onClick={() => setFatal("")} aria-label={tr("web.dismiss")}><X size={14} /></button></div>}
    {dialog && project && <NewSessionDialog project={project} close={() => setDialog(false)} create={create} />}
    {panel === "search" && <SearchDialog sessionId={selected} projectId={workspaceId.current || launchProject || null} close={() => setPanel(null)} openFile={(path) => { setFocusPath(path); setPanel(null); setInspectorOpen(true); setNarrowInspector(true); }} />}
    {panel === "memory" && <MemoryDialog sessionId={selected} close={() => setPanel(null)} />}
    {panel === "rules" && <RulesDialog sessionId={selected} close={() => setPanel(null)} />}
    {panel === "schedules" && <SchedulesDialog sessionId={selected} projectId={workspaceId.current || launchProject || null} close={() => setPanel(null)} />}
    {panel === "settings" && <SettingsDialog theme={theme} inspectorOpen={inspectorOpen} close={() => setPanel(null)} save={savePreference} />}
  </div>;
}
