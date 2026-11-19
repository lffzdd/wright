import { useCallback, useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { api, ApiError } from "../api";
import { parseEvent } from "../protocol";
import { applyEvent } from "../reducer";
import type { SessionSummary, Snapshot, ViewState } from "../types";
import { emptyDraft, type Draft } from "../components/composer/draft";

export type Browsing = "draft" | "preview" | "live";
export type CommandOutcome = "accepted" | "rejected" | "unknown";

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

/**
 * The open session's view: a live event stream for running sessions, a
 * read-only preview for saved ones. `epoch` changes whenever the view is
 * replaced, so async work started for an older view can tell it is stale.
 */
export function useSessionStream({ selected, browsing, setBrowsing, sessions, workspaceId, refreshSessions, draftsRef, updateDraft, setFatal }: {
  selected: string | null;
  browsing: Browsing;
  setBrowsing: Dispatch<SetStateAction<Browsing>>;
  sessions: SessionSummary[];
  workspaceId: MutableRefObject<string | null>;
  refreshSessions: () => Promise<SessionSummary[]>;
  draftsRef: MutableRefObject<Record<string, Draft>>;
  updateDraft: (sessionId: string, patch: (current: Draft) => Draft) => void;
  setFatal: (message: string) => void;
}) {
  const [state, setState] = useState<ViewState | null>(null);
  const stateRef = useRef<ViewState | null>(null);
  stateRef.current = state;
  const socket = useRef<WebSocket | null>(null);
  const commandWaiters = useRef(new Map<string, (outcome: CommandOutcome) => void>());
  const epoch = useRef(0);
  const streamId = useRef("");
  const respondIds = useRef(new Map<string, string>());
  const activeSent = useRef(new Set<string>());

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

  const sendCommand = useCallback((payload: Record<string, unknown>) => {
    const id = String(payload.command_id ?? "");
    if (socket.current?.readyState !== WebSocket.OPEN) return Promise.resolve("unknown" as const);
    activeSent.current.add(id);
    return new Promise<CommandOutcome>((resolve) => {
      commandWaiters.current.set(id, resolve);
      socket.current?.send(JSON.stringify(payload));
    });
  }, []);

  /** Answers an approval or question; a retry reuses the first command id. */
  const respondToInteraction = useCallback((requestId: string, answer: unknown) => {
    const existing = respondIds.current.get(requestId);
    const id = existing ?? crypto.randomUUID();
    respondIds.current.set(requestId, id);
    return sendCommand({ type: "interaction.respond", command_id: id, request_id: requestId, answer }).then((outcome) => {
      if (outcome === "accepted" || outcome === "rejected") respondIds.current.delete(requestId);
      return outcome === "accepted";
    });
  }, [sendCommand]);

  return { state, setState, stateRef, epoch, sendCommand, respondToInteraction };
}
