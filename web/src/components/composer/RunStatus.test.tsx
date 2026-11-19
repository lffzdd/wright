import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../../i18n";
import type { ActiveTurn, Interaction, ToolState, ViewState } from "../../types";
import { tabTitle } from "../../app/browser";
import { RunStatus, formatElapsed, runActivity } from "./RunStatus";

const view = (turn: Partial<ActiveTurn> | null, pending: Interaction[] = []): ViewState => ({
  stream_id: "s", last_seq: 0, seen: [], connection: "connected",
  session: { session_id: "session", status: "running", active: true },
  history: [], timeline: [], plan: {}, pending_interactions: pending,
  active_turn: turn ? { turn_id: "turn-1", prompt: "go", attachments: [], reasoning: "", content: "", tools: [], ...turn } : null,
  notices: [], queued_commands: [], queue_depth: 0,
  usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null,
    request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
    context_tokens: null, context_limit: null },
});
const tool = (name: string, phase: ToolState["phase"], args: Record<string, unknown> = {}): ToolState => ({ call_id: name, name, phase, arguments: args });

beforeEach(() => setLocale("en"));
afterEach(() => { cleanup(); vi.useRealTimers(); setLocale("en"); });

describe("runActivity", () => {
  it("names the newest tool still at work and its target", () => {
    const state = view({ tools: [tool("read_file", "succeeded", { path: "a.ts" }), tool("edit", "running", { path: "src/b.ts" })] });
    expect(runActivity(state)).toEqual({ key: "web.run.editing", target: "src/b.ts" });
    expect(runActivity(view({ tools: [tool("shell", "running", { command: "npm test" })] }))).toEqual({ key: "web.run.running_command", target: "npm test" });
  });

  it("puts a waiting decision ahead of everything else", () => {
    const pending = [{ request_id: "r", kind: "permission" } as Interaction];
    expect(runActivity(view({ tools: [tool("shell", "awaiting_approval", { command: "rm x" })] }, pending))).toMatchObject({ key: "web.run.waiting_approval", waiting: true });
    expect(runActivity(view({}, [{ request_id: "q", kind: "ask_user" } as Interaction]))).toMatchObject({ key: "web.run.waiting_answer" });
  });

  it("falls back to the stream once no tool is running", () => {
    expect(runActivity(view({ tools: [tool("grep", "succeeded")], content: "Done" }))?.key).toBe("web.run.replying");
    expect(runActivity(view({ reasoning: "hmm" }))?.key).toBe("web.run.thinking");
    expect(runActivity(view({}))?.key).toBe("web.working");
    expect(runActivity(view(null))).toBeNull();
  });
});

describe("RunStatus", () => {
  it("shows what is running and how long the turn has run", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    render(<RunStatus state={view({ tools: [tool("edit", "running", { path: "src/b.ts" })] })} />);
    expect(screen.getByRole("status").textContent).toBe("Editing src/b.ts0s");
    act(() => { vi.advanceTimersByTime(65_000); });
    expect(screen.getByRole("status").textContent).toContain("1m 05s");
  });

  it("renders nothing when no turn is running", () => {
    const { container } = render(<RunStatus state={view(null)} />);
    expect(container.textContent).toBe("");
  });

  it("formats elapsed time compactly", () => {
    expect(formatElapsed(9_400)).toBe("9s");
    expect(formatElapsed(3_725_000)).toBe("1h 02m");
  });
});

describe("tabTitle", () => {
  it("counts sessions waiting on the person, trusting the open session's own stream", () => {
    const listed = [{ session_id: "a", pending_interactions: 1 }, { session_id: "session", pending_interactions: 1 }, { session_id: "b" }];
    expect(tabTitle(listed, null)).toBe("(2) Wright");
    // The open session already answered; the list has not caught up.
    expect(tabTitle(listed, view({}))).toBe("(1) Wright");
    expect(tabTitle([{ session_id: "b" }], view({}, [{ request_id: "r", kind: "permission" } as Interaction]))).toBe("(1) Wright");
    expect(tabTitle([], null)).toBe("Wright");
  });
});
