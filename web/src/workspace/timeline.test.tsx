import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { applyEvent } from "../reducer";
import type { UiEvent, ViewState } from "../types";
import { Timeline } from "./widgets";
import { toolCardFromTimeline, toolCardFromState } from "./cards";

beforeAll(() => { Element.prototype.scrollTo = vi.fn(); });
afterEach(cleanup);
const fresh = (): ViewState => ({
  stream_id: "s", last_seq: 0, seen: [], connection: "connected",
  session: { session_id: "session", status: "idle", active: true },
  history: [], timeline: [], active_turn: null, plan: {}, pending_interactions: [],
  notices: [], queued_commands: [], queue_depth: 0,
  usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null,
    request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
    context_tokens: null, context_limit: null },
});
function emit(state: ViewState, type: string, payload: Record<string, unknown>, turn_id = "turn-1") {
  const seq = state.last_seq + 1;
  const event: UiEvent = { version: 2, stream_id: "s", seq, event_id: `e${seq}`, emitted_at: "now", project_id: "p", session_id: "session", turn_id, type, payload };
  return applyEvent(state, event);
}
const transcript = (state: ViewState) => <Timeline state={state} respond={async () => true} cancelQueued={() => undefined} />;

describe("real timeline projection", () => {
  it("keeps the user prompt, shell arguments and incremental output as tools run", () => {
    let state = emit(fresh(), "turn.started", { prompt: "Run the tests" });
    state = emit(state, "tool.planned", { call_id: "c", name: "execute_command", arguments: { command: "pytest -q" } });
    state = emit(state, "tool.running", { call_id: "c", name: "execute_command" });
    state = emit(state, "tool.output", { call_id: "c", output: "Collecting tests\n" });
    state = emit(state, "tool.output", { call_id: "c", output: "Running tests" });
    expect(state.active_turn?.tools[0].phase).toBe("running");
    render(transcript(state));
    expect(screen.getByText("Run the tests")).toBeTruthy();
    expect(screen.getByText("$ pytest -q")).toBeTruthy();
    expect(screen.getByText(/Collecting tests/).textContent).toContain("Running tests");
  });

  it("keeps identical answers in distinct turns in the original summary cards", () => {
    let state = fresh();
    for (const id of ["turn-1", "turn-2"]) {
      state = emit(state, "turn.started", { prompt: `Request ${id}` }, id);
      state = emit(state, "content.delta", { piece: "draft" }, id);
      state = emit(state, "content.final", { content: "## Result\n\n```python\nprint(42)\n```" }, id);
      state = emit(state, "turn.completed", {}, id);
    }
    const { container } = render(transcript(state));
    expect(screen.getAllByText(/print\(42\)/)).toHaveLength(2);
    expect(container.querySelectorAll(".avatar.assistant")).toHaveLength(2);
    expect(container.querySelectorAll(".summary-row")).toHaveLength(0);
    expect(screen.queryByText("draft")).toBeNull();
  });

  it("shows real reasoning separately from the final answer", () => {
    let state = emit(fresh(), "turn.started", { prompt: "Check it" });
    state = emit(state, "reasoning.delta", { piece: "Inspecting the input" });
    state = emit(state, "content.final", { content: "Finished checking" });
    render(transcript(state));
    expect(screen.getByText("Inspecting the input").closest(".summary-row")).toBeTruthy();
    fireEvent.click(screen.getByText("Inspecting the input").closest(".summary-row")!.querySelector("button")!);
    expect(screen.getByText("Inspecting the input").closest(".summary-row")?.classList.contains("open")).toBe(true);
    expect(screen.getByText("Finished checking").closest(".msg-card")).toBeTruthy();
    expect(screen.queryByText("Finished checking")?.closest(".summary-row")).toBeNull();
  });

  it("renders failed snapshot and live tool results with the same error and exit code", () => {
    const result = { ok: false, err: "Process failed", data: { stdout: "failure details", stderr: "problem on stderr", returncode: 2 } };
    const { container, rerender } = render(toolCardFromTimeline({ id: "c", kind: "shell", name: "execute_command", phase: "failed", arguments: { command: "false" }, result }));
    const snapshotText = container.textContent;
    expect(screen.getByText("Exit 2")).toBeTruthy();
    expect(screen.getByText(/Process failed/).textContent).toContain("problem on stderr");
    expect(screen.queryByText(/Bug Confirmed/)).toBeNull();
    rerender(toolCardFromState({ call_id: "c", name: "execute_command", phase: "failed", arguments: { command: "false" }, ...result }));
    expect(container.textContent).toBe(snapshotText);
  });

  it("does not invent diff line numbers or file counts from output lines", () => {
    const { container, rerender } = render(toolCardFromState({ call_id: "c", name: "edit_file", phase: "succeeded", data: "-old\n+new" }));
    expect(container.querySelector(".diff-line")?.textContent).toBe("");
    rerender(toolCardFromState({ call_id: "read", name: "read_file", phase: "succeeded", data: "line one\nline two" }));
    expect(screen.queryByText("2 files")).toBeNull();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/line one/)).toBeTruthy();
  });

  it("shows directory entry counts without inventing them for ordinary file reads", () => {
    const { rerender } = render(toolCardFromTimeline({
      id: "dir", kind: "tool", name: "read_directory", phase: "succeeded",
      arguments: { path: "src/middleware/" }, duration_ms: 24,
      result: { output: "auth.ts\nrate-limiter.ts\nsession.ts\ntelemetry.ts\nconfig.ts" },
    }));
    expect(screen.getByText("5 files (24ms)")).toBeTruthy();
    rerender(toolCardFromState({ call_id: "read", name: "read_file", phase: "succeeded", data: "line one\nline two" }));
    expect(screen.queryByText("2 files")).toBeNull();
  });
});
