import type { AgentView, Attachment, Interaction, ToolState, UiEvent, ViewState } from "./types";

function noticeMeta(event: UiEvent): { code?: string; params?: Record<string, string> } {
  if (event.type === "turn.failed") {
    return { code: "notice.turn_failed", params: { detail: String(event.payload.error ?? event.payload.status ?? "unknown error") } };
  }
  if (event.type === "turn.cancelled") return { code: "notice.turn_cancelled" };
  if (event.type === "system.checkpoint_error") {
    return { code: "notice.checkpoint_failed", params: { detail: String(event.payload.error ?? "unknown error") } };
  }
  if (event.payload.kind === "completion_rejected") return { code: "notice.completion_another" };
  if (event.payload.kind === "context_compact") {
    return { code: "notice.context_compact", params: { count: String(Number(event.payload.folded_count ?? 0)) } };
  }
  if (typeof event.payload.code === "string" && event.payload.code) {
    const raw = event.payload.params;
    const params = raw && typeof raw === "object"
      ? Object.fromEntries(Object.entries(raw as Record<string, unknown>).map(([key, value]) => [key, String(value)]))
      : undefined;
    return { code: event.payload.code, params };
  }
  return {};
}

function noticeText(event: UiEvent): string {
  if (event.type === "turn.failed") return `Turn failed: ${String(event.payload.error ?? event.payload.status ?? "unknown error")}`;
  if (event.type === "turn.cancelled") return "Turn cancelled.";
  if (event.type === "system.checkpoint_error") return `Checkpoint failed: ${String(event.payload.error ?? "unknown error")}`;
  if (event.type === "command.rejected") return String(event.payload.reason ?? "Command rejected");
  if (event.type === "task.updated") {
    const task = event.payload.task as Record<string, unknown> | undefined;
    return String(task?.description ?? event.payload.description ?? "Background task updated");
  }
  if (event.payload.kind === "completion_rejected") return "Completion check requested another attempt.";
  if (event.payload.kind === "context_compact") return `Context compacted (${Number(event.payload.folded_count ?? 0)} items).`;
  return String(event.payload.text ?? "System notice");
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
    const phase = event.type === "tool.finished"
      ? (event.payload.ok ? "succeeded" : "failed")
      : event.type.slice("tool.".length);
    next = { ...current, tools: upsertTool(current.tools, { ...event.payload, phase }) };
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

export function applyEvent(state: ViewState, event: UiEvent): ViewState {
  if (state.resync) return state;
  if (event.version !== 2) return { ...state, resync: true };
  if (event.stream_id !== state.stream_id) return { ...state, resync: true };
  if (event.session_id !== state.session.session_id) return state;
  if (event.seq <= state.last_seq) return state;
  if (event.seq !== state.last_seq + 1) return { ...state, resync: true };
  if (state.seen.includes(event.event_id)) return state;
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
  if (event.type === "turn.started") {
    next.active_turn = {
      turn_id: event.turn_id,
      run_id: typeof event.payload.run_id === "string" ? event.payload.run_id : undefined,
      prompt: String(event.payload.prompt ?? ""),
      attachments: Array.isArray(event.payload.attachments) ? event.payload.attachments as Attachment[] : [],
      reasoning: "",
      content: "",
      tools: [],
    };
    next.session = { ...state.session, status: "running" };
    const commandId = String(event.payload.command_id ?? "");
    next.queued_commands = next.queued_commands.filter((item) => item.command_id !== commandId);
    next.queue_depth = next.queued_commands.length;
  } else if (event.type === "reasoning.delta" && next.active_turn) {
    next.active_turn = { ...next.active_turn, reasoning: next.active_turn.reasoning + String(event.payload.piece ?? "") };
  } else if (event.type === "content.delta" && next.active_turn) {
    next.active_turn = { ...next.active_turn, content: next.active_turn.content + String(event.payload.piece ?? "") };
  } else if (event.type === "content.final" && next.active_turn) {
    next.active_turn = { ...next.active_turn, content: String(event.payload.content ?? "") };
  } else if (event.type.startsWith("tool.") && next.active_turn) {
    const phase = event.type === "tool.finished"
      ? (event.payload.ok ? "succeeded" : "failed")
      : event.type.slice("tool.".length);
    next.active_turn = {
      ...next.active_turn,
      tools: upsertTool(next.active_turn.tools, { ...event.payload, phase }),
    };
    if (
      event.type === "tool.finished" &&
      ["create_plan", "update_plan", "replan", "get_plan"].includes(String(event.payload.name ?? "")) &&
      event.payload.data && typeof event.payload.data === "object"
    ) {
      next.plan = event.payload.data as ViewState["plan"];
    }
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
    if (finished && next.timeline && !next.timeline.some((item) => item.kind === "text" && item.text === finished)) {
      next.timeline = [...next.timeline, { id: event.event_id, kind: "text", role: "assistant", text: finished }];
    }
    next.active_turn = null;
    next.session = { ...next.session, status: "idle", agent_status: event.type.split(".")[1] };
    if (event.type !== "turn.completed") next = addNotice(next, event);
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
    } else if (event.payload.command === "turn.cancel") {
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
  } else if (event.type === "session.status_changed") {
    const model = event.payload.model;
    const execution = event.payload.execution;
    const lifecycle = event.payload.lifecycle;
    const queueReason = event.payload.queue_reason;
    const knownExecution = execution === "idle" || execution === "running" || execution === "queued" || execution === "waiting_for_input";
    next.session = {
      ...next.session,
      ...(typeof model === "string" && model.trim() ? { model: model.trim() } : {}),
      ...(lifecycle === "open" || lifecycle === "closing" || lifecycle === "closed" ? { lifecycle } : {}),
      ...(knownExecution ? { execution } : {}),
      ...(typeof queueReason === "string" ? { queue_reason: queueReason } : {}),
      ...(knownExecution ? { status: lifecycle === "closing" || lifecycle === "closed" ? lifecycle : execution } : {}),
    };
  }
  if (next.timeline && event.type.startsWith("tool.")) {
    const callId = String(event.payload.call_id ?? "");
    if (callId) {
      const phase = event.type === "tool.finished" ? (event.payload.ok ? "succeeded" : "failed") : event.type.slice("tool.".length);
      const result = event.payload.data && typeof event.payload.data === "object" ? event.payload.data as Record<string, unknown> : undefined;
      const index = next.timeline.findIndex((item) => item.id === callId);
      if (index >= 0) {
        const copy = next.timeline.slice();
        copy[index] = { ...copy[index], phase, ...(result ? { result } : {}) };
        next.timeline = copy;
      } else {
        next.timeline = [...next.timeline, { id: callId, kind: "tool", name: String(event.payload.name ?? ""), phase, ...(result ? { result } : {}) }];
      }
    }
  }
  if (next.timeline && event.type === "content.final") {
    const text = String(event.payload.content ?? "");
    if (text && !next.timeline.some((item) => item.kind === "text" && item.text === text)) {
      next.timeline = [...next.timeline, { id: event.event_id, kind: "text", role: "assistant", text }];
    }
  }
  return next;
}
