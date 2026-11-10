import { Archive, Brain, Clock, Copy, DotsThree, Gear, GitBranch, MagnifyingGlass, Moon, PencilSimple, Scroll, SidebarSimple, Sun, Warning, X, XCircle } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { api, ApiError, bootstrap } from "./api";
import { browserLocale, setLocale, useT } from "./i18n";
import { paintTheme, paintedTheme, systemTheme } from "./theme";
import { parseEvent } from "./protocol";
import { applyEvent } from "./reducer";
import type { FileReference, SessionSummary, Snapshot, ViewState } from "./types";
import { Composer, EnvironmentChoice } from "./components/composer/Composer";
import { emptyDraft, type Draft } from "./components/composer/draft";
import { Inspector } from "./components/inspector/Inspector";
import { InspectorResizeHandle } from "./components/inspector/InspectorResizeHandle";
import { MemoryPage, RulesPage, SchedulesPage, SearchDialog, SettingsPage } from "./components/panels/panels";
import { clampInspectorWidth, defaultInspectorWidth, parseInspectorWidth } from "./inspectorWidth";
import { ContextRing } from "./components/session/ContextRing";
import { SessionRail } from "./components/session/SessionRail";
import { sessionTitle } from "./components/session/session";
import { AddWorkspaceDialog } from "./components/session/AddWorkspaceDialog";
import { confirmDialog, DialogHost } from "./components/ui/dialogs";
import { InlineRename } from "./components/ui/InlineRename";
import { MenuButton } from "./components/ui/Menu";
import { InteractionDock, Timeline } from "./components/timeline/Timeline";
import { OpenFileContext } from "./components/code/openFile";


/** The server no longer has this session open; its checkpoint remains. */
function sessionGone(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

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
type CapabilityCounts = { memory: number | null; rules: number | null; schedules: number | null };
type Panel = null | "search";
/** A capability screen that replaces the conversation in the main area. */
type Page = null | "memory" | "rules" | "schedules" | "settings";


export default function App() {
  const tr = useT();
  const [project, setProject] = useState<Record<string, unknown> | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [state, setState] = useState<ViewState | null>(null);
  const stateRef = useRef<ViewState | null>(null);
  stateRef.current = state;
  const [browsing, setBrowsing] = useState<"draft" | "preview" | "live">("draft");
  const browsingRef = useRef(browsing);
  browsingRef.current = browsing;
  const [fatal, setFatal] = useState("");
  const [railOpen, setRailOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [inspectorWidth, setInspectorWidth] = useState(() => defaultInspectorWidth());
  const [narrowInspector, setNarrowInspector] = useState(false);
  const [compactViewport, setCompactViewport] = useState(() => window.matchMedia?.("(max-width: 1024px)").matches ?? false);
  const inspectorVisibility = useRef({ open: inspectorOpen, narrow: narrowInspector, compact: compactViewport });
  inspectorVisibility.current = { open: inspectorOpen, narrow: narrowInspector, compact: compactViewport };
  const [theme, setTheme] = useState<"dark" | "light">(paintedTheme);
  const [serverLatency, setServerLatency] = useState<number | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceRow[]>([]);
  const [capabilityCounts, setCapabilityCounts] = useState<CapabilityCounts>({ memory: null, rules: null, schedules: null });
  const [panel, setPanel] = useState<Panel>(null);
  const panelRef = useRef(panel);
  panelRef.current = panel;
  const [page, setPage] = useState<Page>(null);
  const pageRef = useRef(page);
  pageRef.current = page;
  const [renaming, setRenaming] = useState<string | null>(null);
  const [addingWorkspace, setAddingWorkspace] = useState(false);
  const [callFocus, setCallFocus] = useState<{ sessionId: string; callId: string; request: number } | null>(null);
  const [fileFocus, setFileFocus] = useState<{ sessionId: string; path: string; line?: number; request: number } | null>(null);
  const [booted, setBooted] = useState(false);
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
  const launchRoot = useRef<string | undefined>(undefined);
  const sessionListGeneration = useRef(0);
  const workspaceSelection = useRef<Promise<unknown>>(Promise.resolve());
  const persistWorkspaceSelection = useCallback((projectId: string) => {
    workspaceSelection.current = workspaceSelection.current.catch(() => undefined).then(() => api.selectWorkspace(projectId));
    workspaceSelection.current.catch((error) => { if (workspaceId.current === projectId) setFatal(String(error)); });
  }, []);
  const updateDraft = useCallback((sessionId: string, patch: (current: Draft) => Draft) => {
    setDrafts((current) => {
      const previous = current[sessionId] ?? emptyDraft();
      return { ...current, [sessionId]: patch(previous) };
    });
  }, []);

  // The event stream carries text and tool progress; derived inspector data
  // (context categories, accessed files, subagents) lives in the snapshot.
  useEffect(() => {
    if (!selected || browsing !== "live" || !state || state.resync) return;
    const sessionId = selected;
    const generation = epoch.current;
    const timer = window.setTimeout(() => {
      api.snapshot(sessionId).then((snapshot) => {
        if (epoch.current !== generation) return;
        setState((current) => current?.session.session_id === sessionId && snapshot.last_seq >= current.last_seq ? {
          ...current,
          plan: snapshot.plan,
          context_breakdown: snapshot.context_breakdown,
          accessed_files: snapshot.accessed_files,
          subagents: snapshot.subagents,
        } : current);
      }).catch(() => undefined);
    }, 200);
    return () => window.clearTimeout(timer);
  }, [selected, browsing, state?.last_seq, state?.resync]);

  const refreshSessions = useCallback(async () => {
    const request = ++sessionListGeneration.current;
    const items = (await api.sessions(true)).map((item) => ({
      ...item,
      project_id: item.project_id ?? launchId.current ?? undefined,
      project_root: item.project_root ?? launchRoot.current,
    }));
    if (request === sessionListGeneration.current) setSessions(items);
    return items;
  }, []);

  useEffect(() => {
    if (!booted) return;
    let mounted = true;
    let pending = false;
    const measure = async () => {
      if (pending) return;
      pending = true;
      const startedAt = performance.now();
      try {
        const result = await api.health();
        if (mounted && result.ok) setServerLatency(Math.round(performance.now() - startedAt));
        else if (mounted) setServerLatency(null);
      } catch {
        if (mounted) setServerLatency(null);
      } finally {
        pending = false;
      }
    };
    void measure();
    const timer = window.setInterval(() => void measure(), 10_000);
    return () => { mounted = false; window.clearInterval(timer); };
  }, [booted]);

  const selectedProjectId = String(workspaceId.current || project?.project_id || "");
  useEffect(() => {
    // Counts belong to the workspace, so they show even with no task open.
    if (page !== null) return;
    if (!selectedProjectId) {
      setCapabilityCounts({ memory: null, rules: null, schedules: null });
      return;
    }
    let current = true;
    const loadCounts = async () => {
      const [memoryResult, rulesResult, schedulesResult] = await Promise.allSettled([
        api.memory(selectedProjectId),
        api.rules(selectedProjectId),
        api.projectSchedules(selectedProjectId),
      ]);
      if (!current) return;
      const memory = memoryResult.status === "fulfilled" ? memoryResult.value : null;
      const core = memory?.core && typeof memory.core === "object" ? memory.core as Record<string, unknown> : {};
      const semantic = Array.isArray(memory?.semantic) ? memory.semantic : [];
      const episodes = Array.isArray(memory?.episodes) ? memory.episodes : [];
      setCapabilityCounts({
        memory: memoryResult.status === "fulfilled"
          ? [core.human_profile, core.project_anchor].filter((value) => typeof value === "string" && value.trim().length > 0).length + semantic.length + episodes.length
          : null,
        rules: rulesResult.status === "fulfilled"
          ? (rulesResult.value.rules ?? []).filter((rule) => typeof rule.id === "string" && rule.id.length > 0).length
          : null,
        schedules: schedulesResult.status === "fulfilled" ? schedulesResult.value.length : null,
      });
    };
    void loadCounts();
    return () => { current = false; };
  }, [selectedProjectId, page]);

  const themeFollowsSystem = useRef(false);
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-color-scheme: light)");
    if (!media) return;
    const changed = () => { if (themeFollowsSystem.current) applyTheme(systemTheme()); };
    media.addEventListener?.("change", changed);
    return () => media.removeEventListener?.("change", changed);
  }, []);

  const applyTheme = (next: "dark" | "light") => {
    setTheme(next);
    paintTheme(next);
  };

  useEffect(() => {
    const media = window.matchMedia?.("(max-width: 1024px)");
    if (!media) return;
    const changed = () => { setCompactViewport(media.matches); setNarrowInspector(false); };
    media.addEventListener("change", changed);
    return () => media.removeEventListener("change", changed);
  }, []);

  useEffect(() => { setFileFocus(null); setCallFocus(null); }, [selected]);

  const closeInspector = useCallback(() => {
    setNarrowInspector(false);
    if (!inspectorVisibility.current.compact) {
      setInspectorOpen(false);
      api.setPreference({ inspector_open: false }).catch(() => undefined);
    }
  }, []);
  const openInspector = useCallback(() => {
    setInspectorOpen(true);
    setNarrowInspector(true);
    setRailOpen(false);
    api.setPreference({ inspector_open: true }).catch(() => undefined);
  }, []);
  const openFileAt = useCallback((path: string, line?: number) => {
    if (!selected) return;
    setFileFocus((previous) => ({ sessionId: selected, path, line, request: (previous?.request ?? 0) + 1 }));
    openInspector();
  }, [selected, openInspector]);
  const toggleInspector = useCallback(() => {
    const current = inspectorVisibility.current;
    if (current.compact ? current.open && current.narrow : current.open) closeInspector();
    else openInspector();
  }, [closeInspector, openInspector]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPanel("search");
      } else if (
        // Browsers reserve Ctrl+N for a new window and never deliver it to the
        // page, so Alt+N is the shortcut that works in a tab.
        (event.altKey && !event.ctrlKey && !event.metaKey && event.code === "KeyN")
        || ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n")
      ) {
        event.preventDefault();
        setPage(null);
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
        setNarrowInspector(false);
        setRailOpen((prev) => !prev);
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "j") {
        event.preventDefault();
        toggleInspector();
      } else if (event.key === "Escape") {
        if (panelRef.current !== null) {
          event.preventDefault();
          setPanel(null);
          return;
        }
        if (pageRef.current !== null) {
          event.preventDefault();
          setPage(null);
          return;
        }
        if (inspectorVisibility.current.compact && inspectorVisibility.current.narrow) {
          event.preventDefault();
          closeInspector();
          return;
        }
        const interaction = stateRef.current?.pending_interactions.find((item) => item.kind === "permission");
        if (interaction && browsingRef.current === "live" && interaction.choices?.some((choice) => choice.id === "deny")) {
          event.preventDefault();
          event.stopPropagation();
          respondToInteraction(interaction.request_id, "deny");
          return;
        }
        setPanel(null);
        setRailOpen(false);
        setNarrowInspector(false);
      } else if (event.altKey && event.key === "Enter") {
        const interaction = stateRef.current?.pending_interactions.find((item) => item.kind === "permission");
        if (interaction && browsingRef.current === "live" && interaction.choices?.some((choice) => choice.id === "allow_once")) {
          event.preventDefault();
          event.stopPropagation();
          respondToInteraction(interaction.request_id, "allow_once");
        }
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);


  useEffect(() => {
    document.documentElement.classList.add("dark");
    let cancelled = false;
    bootstrap().then(async () => {
      if (cancelled) return;
      const [prefs, projectData, registry] = await Promise.all([
        api.preferences().catch(() => null),
        api.project(),
        api.workspaces().catch(() => null),
      ]);
      if (cancelled) return;
      if (prefs) {
        // Until a choice is saved, follow the browser's language and color scheme.
        setLocale(prefs.interface_language_saved === false ? browserLocale() : prefs.interface_language);
        themeFollowsSystem.current = prefs.theme_saved === false;
        applyTheme(themeFollowsSystem.current ? systemTheme() : prefs.theme);
        setInspectorOpen(prefs.inspector_open);
        const stored = parseInspectorWidth(prefs.inspector_width);
        setInspectorWidth(stored ?? defaultInspectorWidth());
      }
      setProject(projectData);
      launchId.current = String(projectData.project_id ?? "");
      launchRoot.current = typeof projectData.project_root === "string" ? projectData.project_root : undefined;
      if (registry) {
        setWorkspaces(registry.projects.map((item) => ({ ...item, selected: item.project_id === registry.selected_project_id })));
        workspaceId.current = registry.selected_project_id;
      }
      const items = await refreshSessions();
      if (cancelled) return;
      const storageKey = String(workspaceId.current || projectData.project_id || "");
      const remembered = storageKey ? localStorage.getItem(`wright.viewed.${storageKey}`) : null;
      const ownItems = items.filter((item) => !item.project_id || item.project_id === storageKey);
      const byId = ownItems.find((item) => item.session_id === remembered);
      const latest = [...ownItems].sort((left, right) => String(right.saved_at || "").localeCompare(String(left.saved_at || "")))[0];
      const match = byId ?? latest;
      if (match) {
        const key = String(workspaceId.current || projectData.project_id || "");
        if (key) localStorage.setItem(`wright.viewed.${key}`, match.session_id);
        if (match.active) {
          setBrowsing("live");
          setSelected(match.session_id);
        } else {
          setBrowsing("preview");
          setSelected(match.session_id);
        }
      } else {
        setBrowsing("draft");
        setSelected(null);
      }
      setBooted(true);
    }).catch((error) => {
      if (!cancelled) setFatal(String(error));
    });
    return () => { cancelled = true; };
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
          }).catch((error) => {
            if (!owned()) return;
            // The session was closed (for example to make room). Its history is
            // still on disk: show it instead of reconnecting forever.
            if (sessionGone(error)) { setBrowsing("preview"); refreshSessions().catch(() => undefined); return; }
            connect(snapshot, attempt + 1);
          });
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
          "interaction.requested", "interaction.resolved", "session.status_changed", "session.policy_updated",
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
    }).catch((error) => {
      if (!owned()) return;
      if (sessionGone(error)) { setBrowsing("preview"); refreshSessions().catch(() => undefined); return; }
      setFatal(String(error));
    });
    return () => { epoch.current += 1; if (retryTimer !== undefined) window.clearTimeout(retryTimer); socket.current?.close(); };
  }, [selected, browsing, refreshSessions, updateDraft]);

  useEffect(() => {
    if (browsing !== "preview" || !selected) return;
    const generation = ++epoch.current;
    const sessionId = selected;
    socket.current?.close();
    const owner = sessions.find((item) => item.session_id === sessionId)?.project_id || workspaceId.current || undefined;
    api.preview(sessionId, owner).then((preview) => {
      if (epoch.current !== generation) return;
      const body = preview as unknown as { session: Snapshot["session"]; history?: Snapshot["history"]; timeline?: Snapshot["timeline"]; plan?: Snapshot["plan"]; subagents?: Snapshot["subagents"]; accessed_files?: Snapshot["accessed_files"]; context_breakdown?: Snapshot["context_breakdown"] };
      setState({
        ...initialView({
          stream_id: "preview",
          last_seq: 0,
          session: { ...body.session, active: false },
          history: body.history ?? [],
          timeline: body.timeline ?? [],
          active_turn: null,
          plan: body.plan ?? {},
          pending_interactions: [],
          notices: [],
          queued_commands: [],
          queue_depth: 0,
          usage: {
            prompt_tokens: null, completion_tokens: null, total_tokens: null,
            request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
            context_tokens: null, context_limit: null,
          },
          context_breakdown: body.context_breakdown ?? undefined,
          subagents: body.subagents,
          accessed_files: body.accessed_files,
        }),
        connection: "connected",
      });
    }).catch((error) => { if (epoch.current === generation) setFatal(String(error)); });
    return () => { epoch.current += 1; };
  }, [browsing, selected]);

  useEffect(() => {
    if (!booted) return;
    const timer = window.setInterval(() => refreshSessions().catch(() => undefined), 5_000);
    return () => window.clearInterval(timer);
  }, [booted, refreshSessions]);

  const sendCommand = (payload: Record<string, unknown>) => {
    const id = String(payload.command_id ?? "");
    if (socket.current?.readyState !== WebSocket.OPEN) return Promise.resolve("unknown" as const);
    activeSent.current.add(id);
    return new Promise<"accepted" | "rejected" | "unknown">((resolve) => {
      commandWaiters.current.set(id, resolve);
      socket.current?.send(JSON.stringify(payload));
    });
  };
  const respondToInteraction = (requestId: string, answer: unknown) => {
    const existing = respondIds.current.get(requestId);
    const id = existing ?? crypto.randomUUID();
    respondIds.current.set(requestId, id);
    return sendCommand({ type: "interaction.respond", command_id: id, request_id: requestId, answer }).then((outcome) => {
      if (outcome === "accepted" || outcome === "rejected") respondIds.current.delete(requestId);
      return outcome === "accepted";
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
    const resumeOwner = selected && browsing === "preview"
      ? sessions.find((item) => item.session_id === selected)?.project_id || state?.session.project_id || surface
      : surface;
    const generation = epoch.current;
    const current = draftsRef.current[owner] ?? emptyDraft();
    const clientRequestId = current.clientRequestId || crypto.randomUUID();
    updateDraft(owner, (item) => ({ ...item, clientRequestId, commandId, phase: "awaiting", reason: browsing === "preview" ? tr("web.resuming_session") : tr("web.awaiting_accept") }));
    try {
      let sessionId = current.boundSessionId;
      const environment = current.environment ?? "local";
      const registered = resumeOwner;
      const foreign = Boolean(registered && launchId.current && registered !== launchId.current);
      const open = (body: Record<string, unknown>) => registered && (foreign || browsing === "preview")
        ? api.createInWorkspace(registered as string, body)
        : api.create(body);
      if (browsing === "preview" && selected) {
        const created = await open({ resume: selected, client_request_id: clientRequestId });
        sessionId = created.session.session_id;
        updateDraft(owner, (item) => item.clientRequestId === clientRequestId ? { ...item, boundSessionId: sessionId } : item);
      } else if (!sessionId) {
        const created = await open({
          environment,
          execution_mode: current.execution_mode ?? "sandbox",
          client_request_id: clientRequestId,
          interaction_mode: draftPolicy.interaction_mode,
          permission_mode: draftPolicy.permission_mode,
        });
        sessionId = created.session.session_id;
        updateDraft(owner, (item) => item.clientRequestId === clientRequestId ? { ...item, boundSessionId: sessionId } : item);
      }
      if (!sessionId) return "rejected" as const;
      updateDraft(owner, (item) => ({ ...item, reason: tr("web.sending_message") }));
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
      const accepted = await api.submitTurn(sessionId, {
        prompt, command_id: commandId, attachment_ids: imageIds, document_ids: docIds, references,
      });
      if (projectKey() === surface && epoch.current === generation) {
        remember(sessionId);
        setBrowsing("live");
        setSelected(sessionId);
      }
      refreshSessions().catch(() => undefined);
      if (accepted && (accepted as { duplicate?: boolean }).duplicate !== true) {
        updateDraft(owner, () => emptyDraft());
      }
      return "accepted" as const;
    } catch (error) {
      // A 4xx answer is a definite "no" with a reason the person can act on
      // (for example: every session slot is busy). Only network failures and
      // 5xx answers leave the outcome unknown.
      const definite = error instanceof ApiError && error.status >= 400 && error.status < 500;
      const reason = error instanceof Error ? error.message : String(error);
      updateDraft(owner, (item) => item.clientRequestId === clientRequestId ? { ...item, phase: definite ? "rejected" : "unknown", reason } : item);
      return definite ? "rejected" as const : "unknown" as const;
    }
  };
  // Viewing a saved session only reads it. Settings and the working tree need its
  // runtime, so asking for them resumes it without sending anything.
  const resumeSelected = async (): Promise<string> => {
    if (!selected) throw new Error("No session is selected");
    if (browsing === "live") return selected;
    const owner = sessions.find((item) => item.session_id === selected)?.project_id || state?.session.project_id || projectKey();
    const body = { resume: selected, client_request_id: crypto.randomUUID() };
    const created = await (owner ? api.createInWorkspace(owner as string, body) : api.create(body));
    const sessionId = created.session.session_id;
    remember(sessionId);
    setBrowsing("live");
    setSelected(sessionId);
    refreshSessions().catch(() => undefined);
    return sessionId;
  };
  const changeModel = async (newModel: string) => {
    const sessionId = await resumeSelected().catch((error) => { setFatal(String(error)); return null; });
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
  const archiveSession = async (sessionId?: string) => {
    const id = sessionId || state?.session.session_id;
    if (!id) return;
    const summary = sessions.find((item) => item.session_id === id) ?? (state?.session.session_id === id ? state.session : { session_id: id });
    if (!await confirmDialog({ title: tr("web.archive_title", { label: sessionTitle(summary, tr) }), message: tr("web.archive_help"), confirmLabel: tr("web.archive") })) return;
    const owner = sessions.find((item) => item.session_id === id)?.project_id || state?.session.project_id || workspaceId.current || undefined;
    await api.archive(id, owner);
    if (selected === id) {
      remember(null);
      setSelected(null);
      setState(null);
      setBrowsing("draft");
    }
    await refreshSessions();
  };
  const renameSession = async (session: { session_id: string; user_goal?: string }, title: string) => {
    setRenaming(null);
    const next = title;
    if (!next.trim() || next.trim() === sessionTitle(session, tr)) return;
    const owner = sessions.find((item) => item.session_id === session.session_id)?.project_id || workspaceId.current || undefined;
    await api.relabel(session.session_id, next.trim(), owner);
    setSessions((items) => items.map((item) => item.session_id === session.session_id ? { ...item, user_goal: next.trim() } : item));
    setState((currentState) => currentState && currentState.session.session_id === session.session_id
      ? { ...currentState, session: { ...currentState.session, user_goal: next.trim() } }
      : currentState);
  };
  const selectSession = async (session: SessionSummary) => {
    const owner = session.project_id || workspaces.find((item) => item.root === session.project_root)?.project_id;
    if (owner && owner !== workspaceId.current) {
      workspaceId.current = owner;
      setWorkspaces((items) => items.map((item) => ({ ...item, selected: item.project_id === owner })));
      persistWorkspaceSelection(owner);
    }
    setPage(null);
    remember(session.session_id);
    if (session.active) setBrowsing("live");
    else setBrowsing("preview");
    if (session.session_id !== selected) setState(null);
    setSelected(session.session_id);
    setRenaming(null);
    setRailOpen(false);
    if (session.recoverable === false) {
      setFatal("This checkpoint can be read as history. Its execution directory is gone, so it cannot run again.");
    }
  };
  const savePreference = async (patch: { theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string; inspector_width?: number }) => {
    const saved = await api.setPreference(patch);
    if (patch.theme) { themeFollowsSystem.current = false; applyTheme(saved.theme); }
    if (patch.inspector_open !== undefined) setInspectorOpen(saved.inspector_open);
    if (patch.interface_language) setLocale(saved.interface_language);
    if (patch.inspector_width !== undefined) {
      const stored = parseInspectorWidth(saved.inspector_width);
      setInspectorWidth(stored ?? defaultInspectorWidth());
    }
  };
  const commitInspectorWidth = useCallback((width: number) => {
    const next = clampInspectorWidth(width);
    setInspectorWidth(next);
    api.setPreference({ inspector_width: next }).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!state?.resync || !selected) return;
    const generation = epoch.current;
    const sessionId = selected;
    api.snapshot(sessionId).then((snapshot) => {
      if (epoch.current !== generation) return;
      streamId.current = snapshot.stream_id;
      setState({ ...initialView(snapshot), connection: "connected" });
    }).catch((error) => {
      if (epoch.current !== generation) return;
      if (sessionGone(error)) { setBrowsing("preview"); return; }
      setFatal(String(error));
    });
  }, [state?.resync, selected]);

  const execution = state?.session.execution ?? state?.session.status;
  const running = execution === "running" || execution === "waiting_for_input";
  const directoryQueued = execution === "queued";
  const composerOwner = selected && browsing !== "draft" ? selected : draftKey;
  const draft = drafts[composerOwner] ?? emptyDraft();
  const selectedWorkspace = workspaces.find((item) => item.project_id === workspaceId.current);
  const branch = String(state?.session.branch_name ?? (workspaceId.current === launchId.current ? project?.branch : "") ?? "");
  const dirty = workspaceId.current === launchId.current ? Number(project?.uncommitted_count ?? 0) : 0;
  const projectName = String(selectedWorkspace?.name ?? project?.name ?? tr("web.local"));
  const launchProject = String(project?.project_id ?? "");
  const planSteps = state?.plan.steps ?? [];
  const currentStep = planSteps.findIndex((step) => ["in_progress", "running", "active"].includes(step.status));
  const selectedProgress = planSteps.length && planSteps.some((step) => step.status !== "completed")
    ? tr("web.step_of", { current: currentStep >= 0 ? currentStep + 1 : Math.min(planSteps.length, planSteps.filter((step) => step.status === "completed").length + 1), total: planSteps.length })
    : "";

  if (fatal && !project) return <main className="fatal"><Warning size={28} /><h1>{tr("web.fatal_title")}</h1><p>{fatal}</p><button className="button primary" onClick={() => location.reload()}>{tr("web.reload")}</button></main>;
  const connection = state?.connection ?? (booted ? "idle" : "connecting");
  const agentStatus = state?.session.agent_status;
  const statusClass = state?.pending_interactions.length
    ? "waiting"
    : execution === "waiting_for_input" ? "waiting-input"
      : execution === "queued" ? "queued"
        : execution === "cancelling" ? "cancelling"
          : execution === "failed" || agentStatus === "failed" || agentStatus === "interrupted" ? "failed"
            : execution === "cancelled" || agentStatus === "cancelled" ? "cancelled"
              : execution === "completed" || agentStatus === "completed" ? "completed"
                : running ? "running" : "";
  const statusPulse = running || statusClass === "waiting";
  const terminalAgentStatus = agentStatus === "completed" || agentStatus === "failed" || agentStatus === "cancelled" || agentStatus === "interrupted";
  const displayStatus = execution === "idle" && terminalAgentStatus ? agentStatus : execution;
  // Idle and finished turns are already visible in the sidebar; the top bar only speaks up when something is happening.
  const statusVisible = Boolean(state?.pending_interactions.length) || ["waiting", "waiting-input", "queued", "cancelling", "failed", "running"].includes(statusClass)
    || connection === "disconnected" || connection === "reconnecting";
  const statusText = state?.pending_interactions.length
    ? tr("web.permission_required")
    : displayStatus ? tr(`web.status.${displayStatus}`) : tr(`web.connection.${connection}`);
  const breakdown = state?.context_breakdown;
  // The session list refreshes on its own, so a title named after the first turn shows up here too.
  const listed = state ? sessions.find((item) => item.session_id === state.session.session_id) : undefined;
  const headerTitle = state ? sessionTitle(listed?.user_goal ? { ...state.session, user_goal: listed.user_goal } : state.session, tr) : "";
  return <div className="app-viewport">
    <header className="topbar">
      <div className="brand">
        <button className="icon-button rail-toggle" onClick={() => { setNarrowInspector(false); setRailOpen(true); }} aria-label={tr("web.open_sessions")}><SidebarSimple size={14} /></button>
        <span className="brand-mark">W</span>
        <strong>Wright</strong>
        <span className="sep">/</span>
        <span className="crumb" title={String(selectedWorkspace?.root ?? project?.project_root ?? "")}>{projectName}</span>
      </div>
      <div className="top-center">
        {branch && <div className="branch-pill"><GitBranch size={12} /><span>{branch}</span>{dirty > 0 ? <span className="dirty">{tr("web.uncommitted", { count: dirty })}</span> : null}</div>}
        <button type="button" className="search-trigger" onClick={() => setPanel("search")}><MagnifyingGlass size={14} /><span>{tr("web.search")}</span><kbd className="kbd">{tr("web.shortcut_key", { key: "K" })}</kbd></button>
      </div>
      <div className="top-status">
        {statusVisible && <span className={`top-agent-state ${statusClass}`} role="status"><span className={`dot ${statusPulse ? "pulse" : ""}`} />{statusText}</span>}
        <button type="button" className="icon-button" title={tr("web.theme")} onClick={() => savePreference({ theme: theme === "dark" ? "light" : "dark" }).catch((error) => setFatal(String(error)))}>{theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}</button>
        {!page && state && selected ? <button type="button" className="icon-button" aria-label={tr((compactViewport ? inspectorOpen && narrowInspector : inspectorOpen) ? "web.close_inspector" : "web.open_inspector")} aria-expanded={compactViewport ? inspectorOpen && narrowInspector : inspectorOpen} aria-controls="workspace-inspector" onClick={toggleInspector}><SidebarSimple size={14} /></button> : null}
      </div>
    </header>
    <div
      className="workspace-grid"
      data-inspector={!page && state && selected && inspectorOpen ? "open" : "closed"}
      style={{ "--inspector-width": `${inspectorWidth}px` } as CSSProperties}
    >
      <SessionRail
        sessions={sessions}
        selected={page ? null : selected}
        onSelect={(session) => selectSession(session).catch((error) => setFatal(String(error)))}
        onCreate={() => { setPage(null); setBrowsing("draft"); setSelected(null); setState(null); remember(null); }}
        open={railOpen}
        selectedProgress={selectedProgress}
        footer={state?.session.model ?? String(project?.default_model ?? "")}
        connectionLatency={serverLatency}
        workspaces={workspaces}
        onSelectWorkspace={(projectId) => {
          workspaceId.current = projectId;
          setWorkspaces((current) => current.map((row) => ({ ...row, selected: row.project_id === projectId })));
          setSelected(null); setState(null); setBrowsing("draft"); setFileFocus(null); setRailOpen(false);
          persistWorkspaceSelection(projectId);
        }}
        onRegisterWorkspace={() => setAddingWorkspace(true)}
        onUnregisterWorkspace={(target) => {
          confirmDialog({ title: tr("web.unregister_confirm", { name: target.name }), message: tr("web.unregister_help"), confirmLabel: tr("web.unregister_workspace"), danger: true }).then((ok) => {
            if (!ok) return;
            return api.unregisterWorkspace(target.project_id).then(() => api.workspaces()).then((registry) => {
              setWorkspaces(registry.projects.map((item) => ({ ...item, selected: item.project_id === registry.selected_project_id })));
              workspaceId.current = registry.selected_project_id;
              setSelected(null); setState(null); setBrowsing("draft"); setFileFocus(null);
              return refreshSessions();
            });
          }).catch((error) => setFatal(String(error)));
        }}
        renaming={renaming}
        onRenameStart={(session) => setRenaming(session.session_id)}
        onRenameCancel={() => setRenaming(null)}
        onRenameSubmit={(session, title) => renameSession(session, title).catch((error) => setFatal(String(error)))}
        onArchive={(session) => archiveSession(session.session_id).catch((error) => setFatal(String(error)))}
      >
        {!workspaces.length && <p className="empty-small">{tr("web.launch_project")}</p>}
        <div>
          <div className="section-label"><span>{tr("web.capabilities")}</span></div>
          <button type="button" className={`nav-row${page === "memory" ? " selected" : ""}`} aria-current={page === "memory" ? "page" : undefined} onClick={() => { setPage("memory"); setRailOpen(false); }}><Brain size={14} />{tr("web.memory")}{capabilityCounts.memory !== null && capabilityCounts.memory > 0 && <span className="nav-count">{capabilityCounts.memory}</span>}</button>
          <button type="button" className={`nav-row${page === "rules" ? " selected" : ""}`} aria-current={page === "rules" ? "page" : undefined} onClick={() => { setPage("rules"); setRailOpen(false); }}><Scroll size={14} />{tr("web.rules")}{capabilityCounts.rules !== null && capabilityCounts.rules > 0 && <span className="nav-count">{capabilityCounts.rules}</span>}</button>
          <button type="button" className={`nav-row${page === "schedules" ? " selected" : ""}`} aria-current={page === "schedules" ? "page" : undefined} onClick={() => { setPage("schedules"); setRailOpen(false); }}><Clock size={14} />{tr("web.schedules")}{capabilityCounts.schedules !== null && capabilityCounts.schedules > 0 && <span className="nav-count">{capabilityCounts.schedules}</span>}</button>
          <button type="button" className={`nav-row${page === "settings" ? " selected" : ""}`} aria-current={page === "settings" ? "page" : undefined} onClick={() => { setPage("settings"); setRailOpen(false); }}><Gear size={14} />{tr("web.settings")}</button>
        </div>
      </SessionRail>
      {page ? <main className="conversation capability-main">
        {page === "memory" && <MemoryPage projectId={selectedProjectId || null} />}
        {page === "rules" && <RulesPage projectId={selectedProjectId || null} />}
        {page === "schedules" && <SchedulesPage sessionId={selected} projectId={selectedProjectId || null} />}
        {page === "settings" && <SettingsPage theme={theme} inspectorOpen={inspectorOpen} save={savePreference} />}
      </main> : <main className="conversation">
        {state ? <>
          <div className="conversation-head">
            <div className="task-heading">
              {renaming === state.session.session_id
                ? <InlineRename className="heading-rename" value={headerTitle} label={tr("web.rename_prompt")} onSubmit={(title) => renameSession(state.session, title).catch((error) => setFatal(String(error)))} onCancel={() => setRenaming(null)} />
                : <h1 title={headerTitle} onDoubleClick={() => setRenaming(state.session.session_id)}>{headerTitle}</h1>}
            </div>
            <div className="task-status">
              {typeof breakdown?.total === "number" && <ContextRing total={breakdown.total} limit={breakdown.limit} />}
              <MenuButton label={tr("web.session_actions")} items={() => [
                { label: tr("web.rename"), icon: <PencilSimple size={13} />, onSelect: () => setRenaming(state.session.session_id) },
                { label: tr("web.copy_session_id"), icon: <Copy size={13} />, onSelect: () => { navigator.clipboard?.writeText(state.session.session_id).catch(() => undefined); } },
                "separator",
                { label: tr("web.archive"), icon: <Archive size={13} />, onSelect: () => { archiveSession().catch((error) => setFatal(String(error))); } },
                { label: tr("web.close_session"), icon: <XCircle size={13} />, danger: true, onSelect: () => {
                  const sessionId = state.session.session_id;
                  confirmDialog({ title: tr("web.close_title", { label: headerTitle }), message: tr("web.close_help"), confirmLabel: tr("web.close_session"), danger: true }).then((ok) => {
                    if (!ok) return;
                    return api.close(sessionId).then(() => { setSelected(null); setState(null); refreshSessions(); });
                  }).catch((error) => setFatal(String(error)));
                } },
              ]}><DotsThree size={16} weight="bold" /></MenuButton>
            </div>
          </div>
          <OpenFileContext.Provider value={openFileAt}>
          <Timeline
            key={state.session.session_id}
            state={state}
            revealCall={callFocus?.sessionId === state.session.session_id ? callFocus : null}
            respond={(requestId, answer) => {
              return respondToInteraction(requestId, answer);
            }}
            cancelQueued={(targetCommandId) => {
              const id = crypto.randomUUID();
              sendCommand({ type: "turn.cancel_queued", command_id: id, target_command_id: targetCommandId }).catch(() => undefined);
            }}
          >
          {browsing === "preview" && <p className="hint conversation-footnote">{state.session.recoverable === false ? tr("web.history_unavailable") : tr("web.history_view")}</p>}
          {browsing === "live" && state.pending_interactions.length > 0 && (
            <InteractionDock
              sessionId={state.session.session_id}
              pending={state.pending_interactions}
              root={state.session.execution_root}
              respond={(requestId, answer) => respondToInteraction(requestId, answer)}
            />
          )}
          <div className="composer-wrap">
            <Composer
              key={state.session.session_id}
              sessionId={state.session.session_id}
              projectId={projectKey() || null}
              deferUploads={browsing === "preview"}
              connection={browsing === "preview" ? "connected" : (state.session.lifecycle === "closing" || state.session.lifecycle === "closed" || state.session.status === "closing" || state.session.status === "closed" ? "closed" : state.connection)}
              draft={draft}
              updateDraft={updateDraft}
              running={Boolean(running || directoryQueued)}
              models={Array.isArray(project?.models) ? project.models as string[] : []}
              currentModel={state.session.model ?? ""}
              onModel={(model) => changeModel(model).catch((error) => setFatal(String(error)))}
              interactionMode={state.session.interaction_mode ?? "agent"}
              permissionMode={state.session.permission_mode ?? "default"}
              executionMode={state.session.execution_mode ?? "sandbox"}
              onPolicy={async (body) => {
                const sessionId = await resumeSelected();
                await api.policy(sessionId, body);
                setState((current) => {
                  if (!current || current.session.session_id !== sessionId) return current;
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
          </Timeline>
          </OpenFileContext.Provider>
        </> : selected ? <p className="hint" role="status">{tr("web.loading_session")}</p> : <>
          <div className="conversation-head"><div className="task-heading"><h1>{tr("web.no_session")}</h1></div></div>
          <div className="conversation-body welcome-body">
          <div className="conversation-scroll">
          <div className="conversation-main">
          <div className="timeline welcome-pane">
            <h2 className="welcome-title">{tr("web.welcome_title", { name: projectName })}</h2>
            <p className="hint">{tr("web.welcome_help")}</p>
            <EnvironmentChoice sessionId={draftKey} draft={draft} updateDraft={updateDraft} git={Boolean(project?.git)} workLabel={selectedWorkspace?.root ?? (typeof project?.project_root === "string" ? project.project_root : "")} />
          </div>
          <div className="composer-wrap">
            <Composer
              key={draftKey}
              sessionId={draftKey}
              projectId={projectKey() || null}
              deferUploads
              connection="connected"
              draft={draft}
              updateDraft={updateDraft}
              running={false}
              models={Array.isArray(project?.models) ? project.models as string[] : []}
              currentModel={String(project?.default_model ?? "")}
              interactionMode={draftPolicy.interaction_mode}
              permissionMode={draftPolicy.permission_mode}
              executionMode={draft.execution_mode ?? "sandbox"}
              onPolicy={async (body) => { setDraftPolicy((current) => ({ ...current, ...body })); }}
              submit={(commandId, prompt, attachmentIds, documentIds, references) => beginTurn(draftKey, commandId, prompt, attachmentIds, documentIds ?? [], references ?? [])}
              cancel={async () => undefined}
            />
          </div>
          </div>
          </div>
          </div>
        </>}
      </main>}
      {!page && state && selected && inspectorOpen && !compactViewport ? (
        <InspectorResizeHandle width={inspectorWidth} onWidthChange={setInspectorWidth} onCommit={commitInspectorWidth} />
      ) : null}
      {!page && state && selected ? <Inspector key={selected} state={state} sessionId={selected} open={compactViewport ? inspectorOpen && narrowInspector : inspectorOpen} narrow={compactViewport && narrowInspector} focusPath={fileFocus?.sessionId === selected ? fileFocus.path : null} focusLine={fileFocus?.sessionId === selected ? fileFocus.line : undefined} focusRequest={fileFocus?.request} close={closeInspector} showCall={(callId) => {
        setCallFocus((previous) => ({ sessionId: selected, callId, request: (previous?.request ?? 0) + 1 }));
        // On narrow screens the inspector covers the conversation.
        if (compactViewport) closeInspector();
      }} onResume={resumeSelected} onFeedback={(feedback) => {
        const owner = selected;
        updateDraft(owner, (current) => ({ ...current, prompt: `${current.prompt.trimEnd()}${current.prompt.trim() ? "\n\n" : ""}${feedback}` }));
        if (compactViewport) closeInspector();
        requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus());
      }} /> : null}
    </div>
    {railOpen && <button className="sidebar-backdrop" aria-label={tr("web.close_sessions")} onClick={() => setRailOpen(false)} />}
    {state && selected && inspectorOpen && narrowInspector && compactViewport && <button className="inspector-backdrop" aria-label={tr("web.close_inspector")} onClick={closeInspector} tabIndex={-1} />}
    {fatal && project && <div className="toast" role="alert"><Warning size={16} /><span>{fatal}</span><button className="icon-button" onClick={() => setFatal("")} aria-label={tr("web.dismiss")}><X size={14} /></button></div>}
    {panel === "search" && <SearchDialog sessionId={browsing === "live" ? selected : null} projectId={workspaceId.current || launchProject || null} previewFiles={!selected} close={() => setPanel(null)} openFile={(path) => {
      if (!selected) return;
      setFileFocus((previous) => ({ sessionId: selected, path, request: (previous?.request ?? 0) + 1 }));
      setPanel(null); openInspector();
    }} openSession={async (sessionId) => {
      const session = sessions.find((item) => item.session_id === sessionId) ?? (await refreshSessions()).find((item) => item.session_id === sessionId);
      if (!session) { setFatal("This task is no longer available."); return; }
      await selectSession(session); setPanel(null);
    }} />}
    {addingWorkspace && <AddWorkspaceDialog close={() => setAddingWorkspace(false)} register={async (path) => {
      await api.registerWorkspace(path);
      const registry = await api.workspaces();
      setWorkspaces(registry.projects.map((item) => ({ ...item, selected: item.project_id === workspaceId.current })));
      await refreshSessions();
    }} />}
    <DialogHost />
  </div>;
}
