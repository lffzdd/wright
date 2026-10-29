import { describe, expect, it } from "vitest";
import contract from "./session-status-contract.json";
import { applyEvent } from "./reducer";
import type { UiEvent, ViewState } from "./types";

function emit(state: ViewState, type: string, payload: Record<string, unknown>) {
  const seq = state.last_seq + 1;
  const event: UiEvent = {
    version: 2, stream_id: "stream", seq, event_id: `event-${seq}`, emitted_at: "now",
    project_id: "project", session_id: "session", turn_id: "turn", type, payload,
  };
  return applyEvent(state, event);
}

function active(): ViewState {
  const state: ViewState = {
    stream_id: "stream", last_seq: 0, seen: [], connection: "connected",
    session: { session_id: "session", status: "queued", active: true, model: "first",
      lifecycle: "open", execution: "queued", queue_reason: "old reason" },
    history: [], timeline: [], active_turn: null, plan: {}, pending_interactions: [], notices: [],
    queued_commands: [], queue_depth: 0,
    usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null,
      request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
      context_tokens: null, context_limit: null },
  };
  return emit(emit(state, "turn.started", { prompt: "go" }), "content.delta", { piece: "before" });
}

describe("shared session status contract", () => {
  it("derives status from retained fields across partial updates", () => {
    let state = emit(active(), "session.status_changed", { session_id: "session", lifecycle: "closing" });
    state = emit(state, "session.status_changed", { session_id: "session", execution: "running" });
    expect(state.session.status).toBe("closing");
    expect(state.session.lifecycle).toBe("closing");
    state = emit(state, "session.status_changed", { session_id: "session", lifecycle: "open" });
    expect(state.session.status).toBe("running");
  });

  it.each(contract.valid)("applies $name", ({ payload }) => {
    const state = active();
    const next = emit(state, "session.status_changed", payload);
    const fields = payload as Record<string, unknown>;
    const child = Boolean(fields.agent_depth);
    const signal = !child && fields.turn_signal === "model_turn_started";
    expect(next.resync).toBe(false);
    expect(next.active_turn?.segment).toBe(signal ? 1 : 0);
    for (const key of ["model", "lifecycle", "execution", "queue_reason"] as const) {
      expect(next.session[key]).toBe(!child && key in fields ? fields[key] : state.session[key]);
    }
    if (!child && (fields.lifecycle === "closing" || fields.lifecycle === "closed")) {
      expect(next.session.status).toBe(fields.lifecycle);
    }
    const after = emit(next, "content.delta", { piece: "after" });
    expect(after.timeline?.filter((item) => item.role === "assistant").map((item) => item.text))
      .toEqual(signal ? ["before", "after"] : ["beforeafter"]);
  });

  it.each(contract.invalid)("requests resync for $name", ({ payload }) => {
    const state = active();
    expect(emit(state, "session.status_changed", payload)).toEqual({ ...state, resync: true });
  });
});
