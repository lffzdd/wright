import type { AgentView, Attachment, Interaction, ToolState, UiEvent, ViewState } from "./types";
import { validSessionStatus } from "./protocol";

// Locked against Python notice_display/notice_text by notice-contract.json.
function noticeMeta(event: UiEvent): { code?: string; params?: Record<string, string> } {
  if (event.type === "turn.failed") {
    return { code: "notice.turn_failed", params: { detail: String(event.payload.error || event.payload.status || "unknown error") } };
  }
  if (event.type === "turn.cancelled") return { code: "notice.turn_cancelled" };
  if (event.type === "system.checkpoint_error") {
    return { code: "notice.checkpoint_failed", params: { detail: String(event.payload.error || "unknown error") } };
  }
  if (event.payload.kind === "completion_rejected") return { code: "notice.completion_another" };
  if (event.payload.kind === "context_compact") {
    return { code: "notice.context_compact", params: { count: String(Math.trunc(Number(event.payload.folded_count || 0))) } };
  }
  if (typeof event.payload.code === "string" && event.payload.code) {
    const raw = event.payload.params;
    const params = raw && typeof raw === "object" && !Array.isArray(raw)
      ? Object.fromEntries(Object.entries(raw as Record<string, unknown>).map(([key, value]) => [key, String(value)]))
      : undefined;
    return { code: event.payload.code, params };
  }
  return {};
}

function noticeText(event: UiEvent): string {
  if (event.type === "turn.failed") return `Turn failed: ${String(event.payload.error || event.payload.status || "unknown error")}`;
  if (event.type === "turn.cancelled") return "Turn cancelled.";
  if (event.type === "system.checkpoint_error") return `Checkpoint failed: ${String(event.payload.error || "unknown error")}`;
  if (event.type === "command.rejected") return String(event.payload.reason || "Command rejected");
  if (event.type === "task.updated") {
    const task = event.payload.task as Record<string, unknown> | undefined;
    return String(task?.description || event.payload.description || "Background task updated");
  }
  if (event.payload.kind === "completion_rejected") return "Completion check requested another attempt.";
  if (event.payload.kind === "context_compact") return "Older tool results were folded to save context.";
  return String(event.payload.text || "System notice");
}

function addNotice(state: ViewState, event: UiEvent): ViewState {
  return {
    ...state,
    notices: [...state.notices, {
      id: event.event_id,
      type: event.type,
      kind: typeof event.payload.kind === "string" ? event.payload.kind : undefined,
      text: noticeText(event),
      ...noticeMeta(event),
    }].slice(-50),
  };
}

function knownNumber(value: unknown, current: number | null): number | null {
  if (value === undefined) return current;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return null;
}

function upsertTool(tools: ToolState[], payload: Record<string, unknown>): ToolState[] {
  const callId = String(payload.call_id ?? "");
  const index = tools.findIndex((tool) => tool.call_id === callId);
  const previous = index >= 0 ? tools[index] : { call_id: callId, name: String(payload.name ?? "tool") };
  const output = eventOutput(previous.output, payload);
  const next = {
    ...previous,
    ...payload,
    name: String(payload.name ?? previous.name ?? "tool"),
    output,
  } as ToolState;
  return index >= 0 ? tools.map((tool, i) => (i === index ? next : tool)) : [...tools, next];
}

function eventOutput(previous: string | undefined, payload: Record<string, unknown>): string | undefined {
  if (payload.output === undefined) return previous;
  return `${previous ?? ""}${String(payload.output ?? "")}`;
}

function toolResult(payload: Record<string, unknown>): Record<string, unknown> {
  // These are the fields serialized by backend ToolResult.to_dict().
  const fields = ["ok", "err", "data", "summary", "content", "artifacts", "retention", "retention_key", "display"];
  const result = Object.fromEntries(fields.filter((key) => key in payload).map((key) => [key, payload[key]]));
  if (Array.isArray(result.artifacts)) {
    result.artifacts = result.artifacts.map((artifact: unknown) => artifact && typeof artifact === "object"
      ? Object.fromEntries(Object.entries(artifact).filter(([key]) => key !== "storage_path")) : artifact);
  }
  return result;
}

function childIdentity(event: UiEvent): { taskId: string; depth: number } | null {
  const taskId = String(event.payload.agent_task_id ?? "");
  const depth = Number(event.payload.agent_depth ?? 0);
  if (depth > 0 || taskId) return { taskId: taskId || "child", depth: depth || 1 };
  return null;
}

function applyAgent(state: ViewState, event: UiEvent, identity: { taskId: string; depth: number }): ViewState {
  const agents = [...(state.agents ?? [])];
  const index = agents.findIndex((agent) => agent.task_id === identity.taskId);
  const current: AgentView = index >= 0
    ? agents[index]
    : { task_id: identity.taskId, depth: identity.depth, content: "", tools: [], status: "running" };
  let next = current;
  if (event.type === "content.delta") next = { ...current, content: current.content + String(event.payload.piece ?? "") };
  else if (event.type === "content.final") next = { ...current, content: String(event.payload.content ?? ""), status: "finished" };
  else if (event.type.startsWith("tool.")) {
    const phase = event.type === "tool.output" ? undefined : event.type === "tool.finished"
      ? (event.payload.ok ? "succeeded" : "failed")
      : event.type.slice("tool.".length);
    next = { ...current, tools: upsertTool(current.tools, { ...event.payload, ...(phase ? { phase } : {}) }) };
  } else if (event.type === "usage.request") {
    next = {
      ...current,
      request_usage: {
        prompt_tokens: knownNumber(event.payload.prompt_tokens, current.request_usage?.prompt_tokens ?? null),
        completion_tokens: knownNumber(event.payload.completion_tokens, current.request_usage?.completion_tokens ?? null),
        total_tokens: knownNumber(event.payload.total_tokens, current.request_usage?.total_tokens ?? null),
      },
    };
  }
  const updated = index >= 0 ? agents.map((agent, i) => (i === index ? next : agent)) : [...agents, next];
  return { ...state, agents: updated };
}

function turnKey(state: ViewState, event: UiEvent): string {
  return state.active_turn?.turn_id || event.turn_id || event.event_id;
}

/** Text and reasoning share a model-step cursor and retain their timeline positions. */
function segmentId(state: ViewState, event: UiEvent, kind: "text" | "reasoning" = "text"): string {
  return `${turnKey(state, event)}:${kind === "text" ? "assistant" : "reasoning"}:${state.active_turn?.segment ?? 0}`;
}

function upsertSegment(state: ViewState, event: UiEvent, text: (previous: string) => string, kind: "text" | "reasoning" = "text"): ViewState {
  if (!state.timeline) return state;
  const id = segmentId(state, event, kind);
  const index = state.timeline.findIndex((item) => item.id === id);
  const previous = index >= 0 ? state.timeline[index].text ?? "" : "";
  const value = text(previous);
  if (index < 0 && !value) return state;
  const item = { id, turn_id: turnKey(state, event), kind, role: "assistant", text: value };
  return {
    ...state,
    timeline: index >= 0 ? state.timeline.map((old, i) => i === index ? item : old) : [...state.timeline, item],
  };
}

function closeSegment(state: ViewState, event: UiEvent): ViewState {
  if (!state.active_turn || !state.timeline) return state;
  const open = state.timeline.some((item) => (item.id === segmentId(state, event) || item.id === segmentId(state, event, "reasoning")) && item.text);
  if (!open) return state;
  return { ...state, active_turn: { ...state.active_turn, segment: (state.active_turn.segment ?? 0) + 1, content: "", reasoning: "" } };
}

export function applyEvent(state: ViewState, event: UiEvent): ViewState {
  if (state.resync) return state;
  if (event.version !== 2) return { ...state, resync: true };
  if (event.stream_id !== state.stream_id) return { ...state, resync: true };
  if (event.session_id !== state.session.session_id) return state;
  if (event.seq <= state.last_seq) return state;
  if (event.seq !== state.last_seq + 1) return { ...state, resync: true };
  if (state.seen.includes(event.event_id)) return state;
  if (event.type === "session.status_changed" && !validSessionStatus(event.payload, event.session_id)) {
    return { ...state, resync: true };
  }
  const seen = [...state.seen.slice(-1999), event.event_id];
  const child = childIdentity(event);
  let next: ViewState = {
    ...state,
    agents: state.agents ?? [],
    queued_commands: state.queued_commands ?? [],
    seen,
    last_seq: event.seq,
    resync: false,
  };
  if (child && event.type !== "interaction.requested" && event.type !== "interaction.resolved") {
    return applyAgent(next, event, child);
  }
  if (event.type === "plan.updated") {
    if (event.payload.plan && typeof event.payload.plan === "object") next.plan = event.payload.plan as ViewState["plan"];
  } else if (event.type === "turn.started") {
    next.active_turn = {
      turn_id: event.turn_id,
      run_id: typeof event.payload.run_id === "string" ? event.payload.run_id : undefined,
      prompt: String(event.payload.prompt ?? ""),
      attachments: Array.isArray(event.payload.attachments) ? event.payload.attachments as Attachment[] : [],
      reasoning: "",
      content: "",
      tools: [],
      segment: 0,
    };
    // Notices belong to the turn that produced them; a new turn starts clean.
    next.notices = [];
    next.session = { ...state.session, status: "running" };
    if (next.timeline) next.timeline = [...next.timeline, {
      id: `${event.turn_id || event.event_id}:user`, kind: "text", role: "user",
      turn_id: event.turn_id, text: next.active_turn.prompt, attachments: next.active_turn.attachments,
    }];
    const commandId = String(event.payload.command_id ?? "");
    next.queued_commands = next.queued_commands.filter((item) => item.command_id !== commandId);
    next.queue_depth = next.queued_commands.length;
  } else if (event.type === "turn.interjected") {
    // A message sent while the turn ran: it joins this turn after what has
    // streamed so far, and the reply continues in a new segment below it.
    const commandId = String(event.payload.command_id ?? "");
    next = closeSegment(next, event);
    if (next.timeline) next.timeline = [...next.timeline, {
      id: `${commandId || event.event_id}:interjection`, kind: "text", role: "user",
      turn_id: event.turn_id, text: String(event.payload.prompt ?? ""), interjection: true,
    }];
    next.queued_commands = next.queued_commands.filter((item) => item.command_id !== commandId);
    next.queue_depth = next.queued_commands.length;
  } else if (event.type === "reasoning.delta" && next.active_turn) {
    const piece = String(event.payload.piece ?? "");
    next.active_turn = { ...next.active_turn, reasoning: next.active_turn.reasoning + piece };
    next = upsertSegment(next, event, (previous) => previous + piece, "reasoning");
  } else if (event.type === "content.delta" && next.active_turn) {
    const piece = String(event.payload.piece ?? "");
    next.active_turn = { ...next.active_turn, content: next.active_turn.content + piece };
    next = upsertSegment(next, event, (previous) => previous + piece);
  } else if (event.type === "content.final" && next.active_turn) {
    const content = String(event.payload.content ?? "");
    next.active_turn = { ...next.active_turn, content };
    next = upsertSegment(next, event, () => content);
  } else if (event.type.startsWith("tool.") && next.active_turn) {
    if (event.type === "tool.planned" || event.type === "tool.awaiting_approval" || event.type === "tool.running") next = closeSegment(next, event);
    const phase = event.type === "tool.output" ? undefined : event.type === "tool.finished"
      ? (event.payload.ok ? "succeeded" : "failed")
      : event.type.slice("tool.".length);
    const active = next.active_turn as NonNullable<ViewState["active_turn"]>;
    next.active_turn = {
      ...active,
      tools: upsertTool(active.tools, { ...event.payload, ...(phase ? { phase } : {}) }),
    };
  } else if (["turn.completed", "turn.failed", "turn.cancelled"].includes(event.type)) {
    if (next.active_turn) {
      const status = event.type === "turn.completed" ? "completed" : event.type === "turn.cancelled" ? "cancelled" : "failed";
      const fallback = event.type === "turn.failed"
        ? `Turn failed: ${String(event.payload.error ?? event.payload.status ?? "unknown error")}`
        : event.type === "turn.cancelled" ? "Turn cancelled." : "";
      next.history = [...next.history, {
        turn_id: event.turn_id,
        run_id: String(event.payload.run_id ?? next.active_turn.run_id ?? ""),
        status,
        user: next.active_turn.prompt,
        assistant: next.active_turn.content || fallback,
        attachments: next.active_turn.attachments,
        tools: next.active_turn.tools,
      }];
    }
    const finished = next.active_turn?.content ?? "";
    const turnId = next.active_turn?.turn_id || event.turn_id;
    if (finished && next.timeline && !next.timeline.some((item) => item.kind === "text" && item.role === "assistant" && item.turn_id === turnId)) {
      next.timeline = [...next.timeline, { id: `${turnId || event.event_id}:assistant`, turn_id: turnId, kind: "text", role: "assistant", text: finished }];
    }
    if (event.type !== "turn.completed" && next.timeline) {
      // The outcome sits where the turn ended, not pinned under later turns.
      const runId = String(event.payload.run_id ?? next.active_turn?.run_id ?? "");
      const status = event.type === "turn.cancelled" ? "cancelled" : "failed";
      const detail = event.type === "turn.failed" ? String(event.payload.error ?? "") : "";
      const id = `${runId || turnId || event.event_id}:status`;
      next.timeline = [...next.timeline.filter((item) => item.id !== id), { id, turn_id: turnId, run_id: runId, kind: "status", status, text: detail }];
    }
    next.active_turn = null;
    next.session = { ...next.session, status: "idle", agent_status: event.type.split(".")[1] };
    if (event.type !== "turn.completed" && !next.timeline) next = addNotice(next, event);
  } else if (event.type === "interaction.requested") {
    next.pending_interactions = [...next.pending_interactions, event.payload as Interaction];
  } else if (event.type === "interaction.resolved") {
    const requestId = String(event.payload.request_id ?? "");
    next.pending_interactions = next.pending_interactions.filter((item) => item.request_id !== requestId);
    if (next.timeline) next.timeline = next.timeline.filter((item) => item.kind !== "approval" || item.id !== requestId);
  } else if (event.type === "usage.request") {
    next.usage = {
      ...next.usage,
      request_prompt_tokens: knownNumber(event.payload.prompt_tokens, next.usage.request_prompt_tokens),
      request_completion_tokens: knownNumber(event.payload.completion_tokens, next.usage.request_completion_tokens),
      request_total_tokens: knownNumber(event.payload.total_tokens, next.usage.request_total_tokens),
      context_tokens: knownNumber(event.payload.context_tokens, next.usage.context_tokens),
      context_limit: knownNumber(event.payload.context_limit, next.usage.context_limit),
    };
  } else if (event.type === "usage.task") {
    next.usage = {
      ...next.usage,
      prompt_tokens: knownNumber(event.payload.prompt_tokens, next.usage.prompt_tokens),
      completion_tokens: knownNumber(event.payload.completion_tokens, next.usage.completion_tokens),
      total_tokens: knownNumber(event.payload.total_tokens, next.usage.total_tokens),
    };
  } else if (event.type === "command.accepted") {
    if (event.payload.command === "turn.submit" && event.payload.queued && !event.payload.duplicate) {
      const commandId = String(event.payload.command_id ?? "");
      if (!next.queued_commands.some((item) => item.command_id === commandId)) {
        const queued = {
          command_id: commandId,
          prompt: String(event.payload.prompt ?? ""),
        } as { command_id: string; prompt: string; attachments?: Attachment[] };
        const attachments = Array.isArray(event.payload.attachments) ? event.payload.attachments as Attachment[] : [];
        if (attachments.length) queued.attachments = attachments;
        next.queued_commands = [...next.queued_commands, queued];
      }
      next.queue_depth = next.queued_commands.length;
    } else if (event.payload.command === "turn.cancel_queued") {
      const target = String(event.payload.target_command_id ?? "");
      next.queued_commands = next.queued_commands.filter((item) => item.command_id !== target);
      next.queue_depth = next.queued_commands.length;
    } else if (event.payload.command === "turn.cancel_all") {
      next.queued_commands = [];
      next.queue_depth = 0;
    }
  } else if (event.type === "command.rejected") {
    if (event.payload.command === "turn.submit") {
      const commandId = String(event.payload.command_id ?? "");
      next.queued_commands = next.queued_commands.filter((item) => item.command_id !== commandId);
      next.queue_depth = next.queued_commands.length;
    }
    next = addNotice(next, event);
  } else if (["system.notice", "system.checkpoint_error", "task.updated"].includes(event.type)) {
    next = addNotice(next, event);
  } else if (event.type === "session.policy_updated") {
    const interaction = event.payload.interaction_mode;
    const permission = event.payload.permission_mode;
    const executionMode = event.payload.execution_mode;
    next.permissions_revision = event.seq;
    next.session = {
      ...next.session,
      ...(executionMode === "sandbox" || executionMode === "host" ? { execution_mode: executionMode } : {}),
      ...(interaction === "agent" || interaction === "plan" || interaction === "ask" ? { interaction_mode: interaction } : {}),
      ...(permission === "default" || permission === "acceptEdits" || permission === "bypass" || permission === "plan" ? { permission_mode: permission } : {}),
    };
  } else if (event.type === "session.status_changed") {
    if (event.payload.turn_signal === "model_turn_started") next = closeSegment(next, event);
    const model = event.payload.model;
    const execution = event.payload.execution;
    const lifecycle = event.payload.lifecycle;
    const queueReason = event.payload.queue_reason;
    const knownExecution = execution === "idle" || execution === "running" || execution === "queued" || execution === "waiting_for_input" || execution === "cancelling";
    const effectiveLifecycle = lifecycle ?? next.session.lifecycle;
    const effectiveExecution = knownExecution ? execution : next.session.execution;
    next.session = {
      ...next.session,
      ...(typeof model === "string" && model.trim() ? { model: model.trim() } : {}),
      ...(lifecycle === "open" || lifecycle === "closing" || lifecycle === "closed" ? { lifecycle } : {}),
      ...(knownExecution ? { execution } : {}),
      ...(typeof queueReason === "string" ? { queue_reason: queueReason } : {}),
      ...(lifecycle !== undefined || knownExecution ? {
        status: effectiveLifecycle === "closing" || effectiveLifecycle === "closed"
          ? effectiveLifecycle : effectiveExecution ?? next.session.status,
      } : {}),
    };
  }
  if (next.timeline && event.type.startsWith("tool.")) {
    const callId = String(event.payload.call_id ?? "");
    if (callId) {
      const index = next.timeline.findIndex((item) => item.id === callId);
      const previous = index >= 0 ? next.timeline[index] : { id: callId, kind: "tool" as const };
      const phase = event.type === "tool.output" ? previous.phase : event.type === "tool.finished"
        ? (event.payload.ok ? "succeeded" : "failed") : event.type.slice("tool.".length);
      const item = {
        ...previous, phase,
        call_id: callId,
        name: String(event.payload.name ?? previous.name ?? "tool"),
        arguments: event.payload.arguments as Record<string, unknown> | undefined ?? previous.arguments,
        output: eventOutput(previous.output, event.payload),
        // Keep the same ToolResult envelope as the server snapshot.
        result: event.type === "tool.finished" ? toolResult(event.payload) : previous.result,
      };
      next.timeline = index >= 0 ? next.timeline.map((old, i) => i === index ? item : old) : [...next.timeline, item];
    }
  }
  if (next.timeline && event.type === "content.final" && !state.active_turn) {
    const text = String(event.payload.content ?? "");
    const turnId = next.active_turn?.turn_id || event.turn_id;
    const id = `${turnId || event.event_id}:assistant`;
    const item = { id, turn_id: turnId, kind: "text" as const, role: "assistant", text };
    next.timeline = next.timeline.some((old) => old.id === id)
      ? next.timeline.map((old) => old.id === id ? item : old)
      : [...next.timeline, item];
  }
  return next;
}
