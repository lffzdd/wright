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
  agents: [],
  resync: false,
};

function event(seq: number, type: string, payload: Record<string, unknown>, turn_id = "abc:1"): UiEvent {
  return { version: 2, stream_id: "stream", event_id: `event-${seq}`, seq, emitted_at: "now", project_id: "project", session_id: "abc", turn_id, type, payload };
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

  it("keeps registered artifact references on completed tool events", () => {
    let next = applyEvent(state, event(1, "turn.started", { prompt: "make a report" }));
    next = applyEvent(next, event(2, "tool.finished", {
      call_id: "report", name: "write_report", ok: true,
      artifacts: [{ id: "artifact-1", name: "report.md", media_type: "text/markdown", size: 42 }],
    }));
    expect(next.active_turn?.tools[0].artifacts).toEqual([
      { id: "artifact-1", name: "report.md", media_type: "text/markdown", size: 42 },
    ]);
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

  it("shows a directory queue without treating it as a running turn", () => {
    const next = applyEvent(state, event(1, "session.status_changed", {
      session_id: "abc",
      lifecycle: "open",
      execution: "queued",
      queue_reason: "waiting for session abc to finish in this directory",
    }));
    expect(next.session.execution).toBe("queued");
    expect(next.session.status).toBe("queued");
    expect(next.session.queue_reason).toContain("this directory");
    expect(next.active_turn).toBeNull();
  });

  it("ignores a history request without mixing sessions", () => {
    const next = applyEvent(state, event(1, "session.history_requested", { max_turns: 5 }));
    expect(next.history).toEqual([]);
    expect(next.last_seq).toBe(1);
    const other = applyEvent(next, event(2, "content.delta", { piece: "no" }, "other:1"));
    const isolated = { ...event(2, "content.delta", { piece: "no" }), session_id: "other-session" };
    expect(applyEvent(next, isolated)).toBe(next);
    expect(other.last_seq).toBe(2);
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

  it("ignores a stale sequence and asks for resync on a gap", () => {
    const started = applyEvent(state, event(1, "turn.started", { prompt: "keep" }));
    const stale = applyEvent(started, event(1, "content.final", { content: "old" }));
    expect(stale).toBe(started);
    expect(stale.active_turn?.content).toBe("");
    const gapped = applyEvent(started, event(3, "content.delta", { piece: "skipped" }));
    expect(gapped.resync).toBe(true);
    expect(gapped.last_seq).toBe(1);
    expect(gapped.active_turn?.content).toBe("");
  });

  it("does not apply another session or an old event version", () => {
    const other = event(1, "content.final", { content: "nope" });
    other.session_id = "other";
    expect(applyEvent({ ...state, active_turn: { prompt: "", attachments: [], reasoning: "", content: "root", tools: [] } }, other).active_turn?.content).toBe("root");
    const old = event(1, "turn.started", { prompt: "old" });
    old.version = 1;
    expect(applyEvent(state, old).resync).toBe(true);
    expect(applyEvent(state, old).active_turn).toBeNull();
  });

  it("keeps child content, tools, and usage off the root turn", () => {
    let next = applyEvent(state, event(1, "turn.started", { prompt: "parent" }));
    next = applyEvent(next, event(2, "content.final", { content: "root answer" }));
    next = applyEvent(next, event(3, "content.final", { content: "child answer", agent_depth: 1, agent_task_id: "task-1" }));
    next = applyEvent(next, event(4, "tool.finished", { call_id: "child-call", name: "read", ok: true, agent_depth: 1, agent_task_id: "task-1" }));
    next = applyEvent(next, event(5, "usage.request", { prompt_tokens: 9, completion_tokens: 1, total_tokens: 10, agent_depth: 1, agent_task_id: "task-1" }));
    next = applyEvent(next, event(6, "interaction.requested", { request_id: "perm-1", kind: "permission", tool_name: "read", agent_task_id: "task-1", agent_depth: 1 }));
    expect(next.active_turn?.content).toBe("root answer");
    expect(next.active_turn?.tools).toEqual([]);
    expect(next.usage.request_total_tokens).toBe(0);
    expect(next.agents?.[0].content).toBe("child answer");
    expect(next.agents?.[0].tools[0].call_id).toBe("child-call");
    expect(next.agents?.[0].request_usage?.total_tokens).toBe(10);
    expect(next.pending_interactions[0].request_id).toBe("perm-1");
  });

  it("keeps tools and separate turns when the user text repeats", () => {
    let next = applyEvent(state, event(1, "turn.started", { prompt: "继续" }, "turn-a"));
    next = applyEvent(next, event(2, "tool.finished", { call_id: "call-a", name: "read", ok: true, artifacts: [{ id: "art-1", name: "a.txt", media_type: "text/plain", size: 1 }] }));
    next = applyEvent(next, event(3, "turn.completed", { run_id: "run-a" }, "turn-a"));
    next = applyEvent(next, event(4, "turn.started", { prompt: "继续" }, "turn-b"));
    next = applyEvent(next, event(5, "turn.cancelled", {}, "turn-b"));
    expect(next.history).toHaveLength(2);
    expect(next.history[0].turn_id).toBe("turn-a");
    expect(next.history[0].tools?.[0].artifacts?.[0].id).toBe("art-1");
    expect(next.history[1].status).toBe("cancelled");
    expect(next.history[1].assistant).toBe("Turn cancelled.");
  });

  it("updates a snapshot timeline when the approval resolves and the tool finishes", () => {
    const waiting: ViewState = {
      ...state,
      pending_interactions: [{ request_id: "perm-1", kind: "permission" }],
      timeline: [
        { id: "user-1", kind: "text", role: "user", text: "write the note" },
        { id: "call-1", kind: "edit", name: "write_file", phase: "pending" },
        { id: "perm-1", kind: "approval", phase: "pending", interaction: { request_id: "perm-1", kind: "permission" } },
      ],
    };
    let next = applyEvent(waiting, event(1, "interaction.resolved", { request_id: "perm-1" }));
    expect(next.pending_interactions).toEqual([]);
    expect(next.timeline?.some((item) => item.kind === "approval")).toBe(false);
    next = applyEvent(next, event(2, "turn.started", { prompt: "write the note" }));
    next = applyEvent(next, event(3, "tool.finished", { call_id: "call-1", name: "write_file", ok: true, data: { path: "note.txt" } }));
    next = applyEvent(next, event(4, "content.final", { content: "Preview turn completed." }));
    next = applyEvent(next, event(5, "turn.completed", { run_id: "run-1" }));
    expect(next.timeline?.find((item) => item.id === "call-1")?.phase).toBe("succeeded");
    expect(next.timeline?.some((item) => item.text === "Preview turn completed.")).toBe(true);
    expect(next.active_turn).toBeNull();
  });
});
