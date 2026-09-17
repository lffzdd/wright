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
  notices: [],
  queued_commands: [],
  queue_depth: 0,
  usage: {
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
    request_prompt_tokens: 0,
    request_completion_tokens: 0,
    request_total_tokens: 0,
    context_tokens: 0,
    context_limit: 100,
  },
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
    next = applyEvent(next, event(2, "tool.planned", { call_id: "a", name: "read" }));
    next = applyEvent(next, event(3, "tool.awaiting_approval", { call_id: "b", name: "search" }));
    next = applyEvent(next, event(4, "tool.running", { call_id: "b", name: "search" }));
    next = applyEvent(next, event(5, "tool.finished", { call_id: "b", name: "search", ok: true, data: 2 }));
    expect(next.active_turn?.tools.map((tool) => tool.call_id)).toEqual(["a", "b"]);
    expect(next.active_turn?.tools[1].ok).toBe(true);
    expect(next.active_turn?.tools[0].phase).toBe("planned");
    expect(next.active_turn?.tools[1].phase).toBe("succeeded");
  });

  it("deduplicates event ids", () => {
    const delta = event(1, "turn.started", { prompt: "once" });
    const once = applyEvent(state, delta);
    expect(applyEvent(once, delta)).toBe(once);
  });

  it("applies model from session.status_changed", () => {
    const next = applyEvent(state, event(1, "session.status_changed", { session_id: "abc", model: "gpt-4o-mini" }));
    expect(next.session.model).toBe("gpt-4o-mini");
    expect(next.session.status).toBe("idle");
  });

  it("keeps request and task usage separate", () => {
    let next = applyEvent(state, event(1, "usage.request", { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 }));
    next = applyEvent(next, event(2, "usage.task", { prompt_tokens: 30, completion_tokens: 7, total_tokens: 37 }));
    expect(next.usage.request_prompt_tokens).toBe(10);
    expect(next.usage.request_total_tokens).toBe(12);
    expect(next.usage.prompt_tokens).toBe(30);
    expect(next.usage.total_tokens).toBe(37);
  });

  it("surfaces notices, checkpoint errors, and rejected commands", () => {
    let next = applyEvent(state, event(1, "system.notice", { text: "status output" }));
    next = applyEvent(next, event(2, "system.checkpoint_error", { error: "disk full" }));
    next = applyEvent(next, event(3, "command.rejected", { reason: "no longer pending" }));
    expect(next.notices.map((item) => item.text)).toEqual([
      "status output",
      "Checkpoint failed: disk full",
      "no longer pending",
    ]);
  });

  it("tracks queued commands and exposes turn failures", () => {
    let next = applyEvent(state, event(1, "command.accepted", { command: "turn.submit", command_id: "one", prompt: "queued", queued: true }));
    expect(next.queue_depth).toBe(1);
    expect(next.queued_commands).toEqual([{ command_id: "one", prompt: "queued" }]);
    next = applyEvent(next, event(2, "turn.started", { prompt: "queued", command_id: "one" }));
    expect(next.queue_depth).toBe(0);
    next = applyEvent(next, event(3, "turn.failed", { error: "provider unavailable" }));
    expect(next.history.at(-1)?.assistant).toContain("provider unavailable");
    expect(next.notices.at(-1)?.text).toContain("provider unavailable");
  });

  it("removes one queued command without cancelling the others", () => {
    let next = applyEvent(state, event(1, "command.accepted", { command: "turn.submit", command_id: "one", prompt: "first", queued: true }));
    next = applyEvent(next, event(2, "command.accepted", { command: "turn.submit", command_id: "two", prompt: "second", queued: true }));
    next = applyEvent(next, event(3, "command.accepted", { command: "turn.cancel_queued", target_command_id: "one" }));
    expect(next.queued_commands).toEqual([{ command_id: "two", prompt: "second" }]);
  });
});
