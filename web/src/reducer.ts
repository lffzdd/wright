import type { Interaction, ToolState, UiEvent, ViewState } from "./types";

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
    }].slice(-50),
  };
}

function upsertTool(tools: ToolState[], payload: Record<string, unknown>): ToolState[] {
  const callId = String(payload.call_id ?? "");
  const index = tools.findIndex((tool) => tool.call_id === callId);
  const previous = index >= 0 ? tools[index] : { call_id: callId, name: String(payload.name ?? "tool") };
  const next = {
    ...previous,
    ...payload,
    output: String(previous.output ?? "") + String(payload.output ?? ""),
  } as ToolState;
  return index >= 0 ? tools.map((tool, i) => (i === index ? next : tool)) : [...tools, next];
}

export function applyEvent(state: ViewState, event: UiEvent): ViewState {
  if (state.seen.includes(event.event_id)) return state;
  const seen = [...state.seen.slice(-1999), event.event_id];
  let next: ViewState = {
    ...state,
    queued_commands: state.queued_commands ?? [],
    seen,
    last_seq: Math.max(state.last_seq, event.seq),
  };
  if (event.type === "turn.started") {
    next.active_turn = {
      turn_id: event.turn_id,
      prompt: String(event.payload.prompt ?? ""),
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
      const fallback = event.type === "turn.failed"
        ? `Turn failed: ${String(event.payload.error ?? event.payload.status ?? "unknown error")}`
        : event.type === "turn.cancelled" ? "Turn cancelled." : "";
      next.history = [...next.history, { user: next.active_turn.prompt, assistant: next.active_turn.content || fallback }];
    }
    next.active_turn = null;
    next.session = { ...next.session, status: "idle", agent_status: event.type.split(".")[1] };
    if (event.type !== "turn.completed") next = addNotice(next, event);
  } else if (event.type === "interaction.requested") {
    next.pending_interactions = [...next.pending_interactions, event.payload as Interaction];
  } else if (event.type === "interaction.resolved") {
    next.pending_interactions = next.pending_interactions.filter(
      (item) => item.request_id !== String(event.payload.request_id ?? ""),
    );
  } else if (event.type === "usage.request") {
    next.usage = {
      ...next.usage,
      request_prompt_tokens: Number(event.payload.prompt_tokens ?? 0),
      request_completion_tokens: Number(event.payload.completion_tokens ?? 0),
      request_total_tokens: Number(event.payload.total_tokens ?? 0),
    };
  } else if (event.type === "usage.task") {
    next.usage = {
      ...next.usage,
      prompt_tokens: Number(event.payload.prompt_tokens ?? 0),
      completion_tokens: Number(event.payload.completion_tokens ?? 0),
      total_tokens: Number(event.payload.total_tokens ?? 0),
    };
  } else if (event.type === "command.accepted") {
    if (event.payload.command === "turn.submit" && event.payload.queued && !event.payload.duplicate) {
      const commandId = String(event.payload.command_id ?? "");
      if (!next.queued_commands.some((item) => item.command_id === commandId)) {
        next.queued_commands = [...next.queued_commands, {
          command_id: commandId,
          prompt: String(event.payload.prompt ?? ""),
        }];
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
    if (typeof model === "string" && model.trim()) {
      next.session = { ...next.session, model: model.trim() };
    }
  }
  return next;
}
