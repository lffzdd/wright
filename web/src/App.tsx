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
  Composer, Inspector, SessionRail, Timeline, emptyDraft,
  type Draft,
} from "./workspace/widgets";
import type { FileReference } from "./types";

export type { Draft };
export { Composer, Inspector, InteractionCard, visibleModels } from "./workspace/widgets";

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
  const [browsing, setBrowsing] = useState<"draft" | "preview" | "live">("draft");
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
  const [draftPolicy, setDraftPolicy] = useState({ interaction_mode: "agent", permission_mode: "default" });
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
        setBrowsing("draft");
        setSelected(null);
        setState(null);
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
      const projectKey = String(projectData.project_id ?? "");
      const remembered = localStorage.getItem(`wright.viewed.${projectKey}`);
      const match = items.find((item) => item.session_id === remembered);
      if (match?.active) {
        setBrowsing("live");
        setSelected(match.session_id);
      } else if (match) {
        setBrowsing("preview");
        setSelected(match.session_id);
      } else {
        setBrowsing("draft");
        setSelected(null);
      }
    }).catch((error) => setFatal(String(error)));
  }, [refreshSessions]);

  useEffect(() => {
    if (!selected || browsing !== "live") {
      if (!selected) setState(null);
      return;
    }
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
  }, [selected, browsing, refreshSessions, updateDraft]);

  useEffect(() => {
    if (browsing !== "preview" || !selected) return;
    const generation = ++epoch.current;
    const sessionId = selected;
    socket.current?.close();
    api.preview(sessionId).then((preview) => {
      if (epoch.current !== generation) return;
      const body = preview as unknown as { session: Snapshot["session"]; history?: Snapshot["history"] };
      setState({
        ...initialView({
          stream_id: "preview",
          last_seq: 0,
          session: { ...body.session, active: false },
          history: body.history ?? [],
          active_turn: null,
          plan: {},
          pending_interactions: [],
          notices: [],
          queued_commands: [],
          queue_depth: 0,
          usage: {
            prompt_tokens: null, completion_tokens: null, total_tokens: null,
            request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
            context_tokens: null, context_limit: null,
          },
        }),
        connection: "closed",
      });
    }).catch((error) => { if (epoch.current === generation) setFatal(String(error)); });
  }, [browsing, selected]);

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
  const projectKey = () => String(workspaceId.current || launchId.current || "");
  const draftKey = projectKey() ? `draft:${projectKey()}` : "draft:local";
  const remember = (sessionId: string | null) => {
    const key = projectKey();
    if (!key) return;
    if (sessionId) localStorage.setItem(`wright.viewed.${key}`, sessionId);
    else localStorage.removeItem(`wright.viewed.${key}`);
  };
  const beginTurn = async (owner: string, commandId: string, prompt: string, attachmentIds: string[], documentIds: string[] = [], references: FileReference[] = []) => {
    const surface = projectKey();
    const generation = epoch.current;
    const current = draftsRef.current[owner] ?? emptyDraft();
    const clientRequestId = current.clientRequestId || crypto.randomUUID();
    updateDraft(owner, (item) => ({ ...item, clientRequestId, commandId, phase: "awaiting", reason: "" }));
    try {
      let sessionId = current.boundSessionId;
      const environment = current.environment ?? "local";
      const registered = workspaceId.current;
      const foreign = Boolean(registered && launchId.current && registered !== launchId.current);
      const open = (body: Record<string, unknown>) => foreign
        ? api.createInWorkspace(registered as string, body)
        : api.create(body);
      if (!sessionId && browsing === "preview" && selected) {
        const created = await open({ resume: selected, client_request_id: clientRequestId });
        if (projectKey() !== surface) return "rejected" as const;
        sessionId = created.session.session_id;
        updateDraft(owner, (item) => item.clientRequestId === clientRequestId ? { ...item, boundSessionId: sessionId } : item);
      } else if (!sessionId) {
        const created = await open({
          environment,
          client_request_id: clientRequestId,
          interaction_mode: draftPolicy.interaction_mode,
          permission_mode: draftPolicy.permission_mode,
        });
        if (projectKey() !== surface) return "rejected" as const;
        sessionId = created.session.session_id;
        updateDraft(owner, (item) => item.clientRequestId === clientRequestId ? { ...item, boundSessionId: sessionId } : item);
      }
      if (!sessionId) return "rejected" as const;
      const imageIds = [...attachmentIds];
      const docIds = [...documentIds];
      for (const file of current.localFiles ?? []) {
        if (file.image) {
          const record = await api.uploadAttachment(sessionId, file.file) as { id: string };
          imageIds.push(record.id);
        } else {
          const record = await api.uploadDocument(sessionId, file.file);
          docIds.push(record.id);
        }
      }
      if (projectKey() !== surface) return "rejected" as const;
      const accepted = await api.submitTurn(sessionId, {
        prompt, command_id: commandId, attachment_ids: imageIds, document_ids: docIds, references,
      });
      if (projectKey() !== surface) return "rejected" as const;
      remember(sessionId);
      setBrowsing("live");
      setSelected(sessionId);
      refreshSessions().catch(() => undefined);
      if (accepted && (accepted as { duplicate?: boolean }).duplicate !== true) {
        updateDraft(owner, () => emptyDraft());
      }
      return "accepted" as const;
    } catch (error) {
      if (projectKey() === surface && epoch.current === generation) {
        updateDraft(owner, (item) => ({ ...item, phase: "unknown", reason: String(error) }));
      }
      return "unknown" as const;
    }
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
    remember(session.session_id);
    if (session.active) setBrowsing("live");
    else setBrowsing("preview");
    setSelected(session.session_id);
    setRailOpen(false);
    if (session.recoverable === false) {
      setFatal("This checkpoint can be read as history. Its execution directory is gone, so it cannot run again.");
    }
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
  const composerOwner = browsing === "live" && selected ? selected : draftKey;
  const draft = drafts[composerOwner] ?? emptyDraft();
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
      <SessionRail sessions={sessions} selected={selected} onSelect={(session) => selectSession(session).catch((error) => setFatal(String(error)))} onCreate={() => { setBrowsing("draft"); setSelected(null); setState(null); }} open={railOpen} footer={[state?.session.model, state?.connection].filter(Boolean).join(" · ")}>
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
          {browsing === "preview" && <p className="hint">{state.session.recoverable === false ? tr("web.archived") : tr("web.history_view")}</p>}
          <div className="composer-wrap">
            <Composer
              sessionId={state.session.session_id}
              projectId={projectKey() || null}
              connection={state.session.lifecycle === "closing" || state.session.lifecycle === "closed" || state.session.status === "closing" || state.session.status === "closed" ? "closed" : browsing === "preview" ? "connected" : state.connection}
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
              submit={(commandId, prompt, attachmentIds, documentIds, references) => browsing === "live"
                ? sendCommand({ type: "turn.submit", command_id: commandId, prompt, attachment_ids: attachmentIds, document_ids: documentIds ?? [], references: references ?? [] })
                : beginTurn(composerOwner, commandId, prompt, attachmentIds, documentIds ?? [], references ?? [])}
              cancel={async () => {
                const id = cancelId.current ?? crypto.randomUUID();
                cancelId.current = id;
                const outcome = await sendCommand({ type: "turn.cancel", command_id: id });
                if (outcome === "accepted" || outcome === "rejected") cancelId.current = null;
              }}
              cancelAll={async () => {
                const id = crypto.randomUUID();
                await sendCommand({ type: "turn.cancel_all", command_id: id });
              }}
            />
          </div>
        </> : <div className="conversation">
          <div className="conversation-head"><div className="task-heading"><h1>{tr("web.no_session")}</h1></div></div>
          <p className="hint">{tr("web.no_session_help")}</p>
          <div className="composer-wrap">
            <Composer
              sessionId={draftKey}
              projectId={projectKey() || null}
              chooseEnvironment
              git={Boolean(project?.git)}
              workLabel={String(project?.project_root ?? "")}
              connection="connected"
              draft={draft}
              updateDraft={updateDraft}
              running={false}
              models={Array.isArray(project?.models) ? project.models as string[] : []}
              currentModel={String(project?.default_model ?? "")}
              interactionMode={draftPolicy.interaction_mode}
              permissionMode={draftPolicy.permission_mode}
              onPolicy={async (body) => { setDraftPolicy((current) => ({ ...current, ...body })); }}
              submit={(commandId, prompt, attachmentIds, documentIds, references) => beginTurn(draftKey, commandId, prompt, attachmentIds, documentIds ?? [], references ?? [])}
              cancel={async () => undefined}
            />
          </div>
        </div>}
      </main>
      {state && selected ? <Inspector state={state} sessionId={selected} open={inspectorOpen} narrow={narrowInspector} focusPath={focusPath} close={() => { setInspectorOpen(false); setNarrowInspector(false); api.setPreference({ inspector_open: false }).catch(() => undefined); }} /> : <aside className={`inspector ${inspectorOpen ? "" : "is-closed"} ${narrowInspector ? "narrow-open" : ""}`}><p className="empty-small">{tr("web.inspector_empty")}</p></aside>}
    </div>
    {railOpen && <button className="sidebar-backdrop" aria-label={tr("web.close_sessions")} onClick={() => setRailOpen(false)} />}
    {fatal && project && <div className="toast" role="alert"><Warning size={16} /><span>{fatal}</span><button className="icon-button" onClick={() => setFatal("")} aria-label={tr("web.dismiss")}><X size={14} /></button></div>}
    {panel === "search" && <SearchDialog sessionId={browsing === "live" ? selected : null} projectId={workspaceId.current || launchProject || null} close={() => setPanel(null)} openFile={(path) => { setFocusPath(path); setPanel(null); setInspectorOpen(true); setNarrowInspector(true); }} />}
    {panel === "memory" && <MemoryDialog sessionId={selected} close={() => setPanel(null)} />}
    {panel === "rules" && <RulesDialog sessionId={selected} close={() => setPanel(null)} />}
    {panel === "schedules" && <SchedulesDialog sessionId={selected} projectId={workspaceId.current || launchProject || null} close={() => setPanel(null)} />}
    {panel === "settings" && <SettingsDialog theme={theme} inspectorOpen={inspectorOpen} close={() => setPanel(null)} save={savePreference} />}
  </div>;
}
