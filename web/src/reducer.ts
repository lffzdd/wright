import type { Interaction, ToolState, UiEvent, ViewState } from "./types";

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
  let next: ViewState = { ...state, seen, last_seq: Math.max(state.last_seq, event.seq) };
  if (event.type === "turn.started") {
    next.active_turn = {
      turn_id: event.turn_id,
      prompt: String(event.payload.prompt ?? ""),
      reasoning: "",
      content: "",
      tools: [],
    };
    next.session = { ...state.session, status: "running" };
  } else if (event.type === "reasoning.delta" && next.active_turn) {
    next.active_turn = { ...next.active_turn, reasoning: next.active_turn.reasoning + String(event.payload.piece ?? "") };
  } else if (event.type === "content.delta" && next.active_turn) {
    next.active_turn = { ...next.active_turn, content: next.active_turn.content + String(event.payload.piece ?? "") };
  } else if (event.type === "content.final" && next.active_turn) {
    next.active_turn = { ...next.active_turn, content: String(event.payload.content ?? "") };
  } else if (event.type.startsWith("tool.") && next.active_turn) {
    next.active_turn = { ...next.active_turn, tools: upsertTool(next.active_turn.tools, event.payload) };
    if (
      event.type === "tool.finished" &&
      ["create_plan", "update_plan", "replan", "get_plan"].includes(String(event.payload.name ?? "")) &&
      event.payload.data && typeof event.payload.data === "object"
    ) {
      next.plan = event.payload.data as ViewState["plan"];
    }
  } else if (["turn.completed", "turn.failed", "turn.cancelled"].includes(event.type)) {
    if (next.active_turn) {
      next.history = [...next.history, { user: next.active_turn.prompt, assistant: next.active_turn.content }];
    }
    next.active_turn = null;
    next.session = { ...next.session, status: "idle", agent_status: event.type.split(".")[1] };
  } else if (event.type === "interaction.requested") {
    next.pending_interactions = [...next.pending_interactions, event.payload as Interaction];
  } else if (event.type === "interaction.resolved") {
    next.pending_interactions = next.pending_interactions.filter(
      (item) => item.request_id !== String(event.payload.request_id ?? ""),
    );
  } else if (event.type === "usage.task") {
    next.usage = {
      ...next.usage,
      prompt_tokens: Number(event.payload.prompt_tokens ?? 0),
      completion_tokens: Number(event.payload.completion_tokens ?? 0),
      total_tokens: Number(event.payload.total_tokens ?? 0),
    };
  }
  return next;
}
