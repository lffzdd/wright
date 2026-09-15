import { describe, expect, it } from "vitest";
import { applyEvent } from "./reducer";
import type { UiEvent, ViewState } from "./types";

const state: ViewState = {
  stream_id: "stream",
  last_seq: 0,
  session: { session_id: "abc", status: "idle", active: true },
  history: [],
  active_turn: null,
  plan: { steps: [] },
  pending_interactions: [],
  usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, context_tokens: 0, context_limit: 100 },
  seen: [],
  connection: "connected",
};

function event(seq: number, type: string, payload: Record<string, unknown>, turn_id = "abc:1"): UiEvent {
  return { version: 1, stream_id: "stream", event_id: `event-${seq}`, seq, emitted_at: "now", project_id: "project", session_id: "abc", turn_id, type, payload };
}

describe("UI event reducer", () => {
  it("replaces a streamed draft with the final content", () => {
    let next = applyEvent(state, event(1, "turn.started", { prompt: "hello" }));
    next = applyEvent(next, event(2, "content.delta", { piece: "dra" }));
    next = applyEvent(next, event(3, "content.delta", { piece: "ft" }));
    next = applyEvent(next, event(4, "content.final", { content: "settled" }));
    expect(next.active_turn?.content).toBe("settled");
    expect(next.history).toHaveLength(0);
  });

  it("merges concurrent tools by call_id", () => {
    let next = applyEvent(state, event(1, "turn.started", { prompt: "go" }));
    next = applyEvent(next, event(2, "tool.started", { call_id: "a", name: "read" }));
    next = applyEvent(next, event(3, "tool.started", { call_id: "b", name: "search" }));
    next = applyEvent(next, event(4, "tool.finished", { call_id: "b", name: "search", ok: true, data: 2 }));
    expect(next.active_turn?.tools.map((tool) => tool.call_id)).toEqual(["a", "b"]);
    expect(next.active_turn?.tools[1].ok).toBe(true);
  });

  it("deduplicates event ids", () => {
    const delta = event(1, "turn.started", { prompt: "once" });
    const once = applyEvent(state, delta);
    expect(applyEvent(once, delta)).toBe(once);
  });
});
