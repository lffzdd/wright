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
    expect(container.querySelectorAll(".reasoning-card")).toHaveLength(0);
    expect(screen.queryByText("draft")).toBeNull();
  });

  it("renders one answer when completion events use a different transport turn id", () => {
    let state = emit(fresh(), "turn.started", { prompt: "Inspect the real page" }, "run-1");
    state = emit(state, "content.final", { content: "Production answer" }, "model-step-1");
    state = emit(state, "turn.completed", {}, "completion-1");
    render(transcript(state));
    expect(screen.getAllByText("Production answer")).toHaveLength(1);
    expect(state.timeline?.filter((item) => item.role === "assistant")).toHaveLength(1);
  });

  it("shows real reasoning separately from the final answer", () => {
    let state = emit(fresh(), "turn.started", { prompt: "Check it" });
    state = emit(state, "reasoning.delta", { piece: "Inspecting the input" });
    state = emit(state, "content.final", { content: "Finished checking" });
    render(transcript(state));
    const reasoning = screen.getByText("Inspecting the input").closest(".reasoning-card");
    expect(reasoning).toBeTruthy();
    fireEvent.click(reasoning!.querySelector("button")!);
    expect(reasoning?.classList.contains("open")).toBe(true);
    expect(screen.getByText("Finished checking").closest(".msg-card")).toBeTruthy();
    expect(screen.getAllByText("Inspecting the input").length).toBeGreaterThan(0);
  });

  it.each(["turn.completed", "turn.failed", "turn.cancelled"])("keeps ordered reasoning after %s and the next turn", (outcome) => {
    let state = emit(fresh(), "turn.started", { prompt: "Check it" });
    state = emit(state, "reasoning.delta", { piece: "Before " });
    state = emit(state, "reasoning.delta", { piece: "tools" });
    state = emit(state, "content.delta", { piece: "I will inspect" });
    for (const call_id of ["a", "b"]) {
      state = emit(state, "tool.planned", { call_id, name: "read_file" });
      state = emit(state, "tool.running", { call_id });
      state = emit(state, "tool.finished", { call_id, ok: true });
    }
    state = emit(state, "session.status_changed", { session_id: "session", turn_signal: "model_turn_started" });
    state = emit(state, "reasoning.delta", { piece: "After tools" });
    state = emit(state, "content.delta", { piece: "Final draft" });
    state = emit(state, "content.final", { content: "Final answer" });
    const expected = ["Check it", "Before tools", "I will inspect", "a", "b", "After tools", "Final answer"];
    const order = () => state.timeline?.map((item) => item.text ?? item.call_id);
    expect(order()).toEqual(expected);
    const { container, rerender } = render(transcript(state));
    expect(container.querySelectorAll(".reasoning-card")).toHaveLength(2);
    const first = screen.getByText("Before tools").closest(".reasoning-card")!;
    fireEvent.click(first.querySelector("button")!);
    state = emit(state, outcome, {});
    rerender(transcript(state));
    expect(container.querySelectorAll(".reasoning-card")).toHaveLength(2);
    expect(first.classList.contains("open")).toBe(true);
    expect(order()?.slice(0, expected.length)).toEqual(expected);
    expect(screen.getAllByText("Final answer")).toHaveLength(1);
    state = emit(state, "turn.started", { prompt: "Next" }, "turn-2");
    rerender(transcript(state));
    expect(container.querySelectorAll(".reasoning-card")).toHaveLength(2);
  });

  it("continues a snapshot reasoning segment without duplicating it", () => {
    let state: ViewState = {
      ...fresh(),
      active_turn: { turn_id: "turn-1", prompt: "Check", attachments: [], reasoning: "Partial", content: "", tools: [], segment: 1 },
      timeline: [
        { id: "turn-1:user", kind: "text", role: "user", turn_id: "turn-1", text: "Check" },
        { id: "turn-1:reasoning:0", kind: "reasoning", role: "assistant", turn_id: "turn-1", text: "Before" },
        { id: "c", kind: "tool", call_id: "c", name: "read_file", phase: "succeeded" },
        { id: "turn-1:reasoning:1", kind: "reasoning", role: "assistant", turn_id: "turn-1", text: "Partial" },
      ],
    };
    state = emit(state, "reasoning.delta", { piece: " continued" });
    state = emit(state, "content.final", { content: "Done" });
    state = emit(state, "turn.completed", {});
    const { container } = render(transcript(state));
    expect(container.querySelectorAll(".reasoning-card")).toHaveLength(2);
    expect(screen.getAllByText("Partial continued")).toHaveLength(1);
    expect(screen.getAllByText("Done")).toHaveLength(1);
  });

  it("separates model retries even without tools", () => {
    let state = emit(fresh(), "turn.started", { prompt: "Check" });
    state = emit(state, "reasoning.delta", { piece: "Attempt one" });
    state = emit(state, "session.status_changed", { session_id: "session", turn_signal: "model_turn_started" });
    state = emit(state, "reasoning.delta", { piece: "Attempt two" });
    expect(state.timeline?.filter((item) => item.kind === "reasoning").map((item) => item.text)).toEqual(["Attempt one", "Attempt two"]);
  });

  it("renders failed snapshot and live tool results with the same error and exit code", () => {
    const result = { ok: false, err: "Process failed", data: { stdout: "failure details", stderr: "problem on stderr", returncode: 2 } };
    const { container, rerender } = render(toolCardFromTimeline({ id: "c", kind: "shell", name: "execute_command", phase: "failed", arguments: { command: "false" }, result }));
    const snapshotText = container.textContent;
    expect(screen.getByText("Exit 2")).toBeTruthy();
    expect(screen.getByText(/Process failed/).textContent).toContain("problem on stderr");
    expect(screen.queryByText(/Bug Confirmed/)).toBeNull();
    let state = emit(fresh(), "turn.started", { prompt: "Check failure" });
    state = emit(state, "tool.planned", { call_id: "c", name: "execute_command", arguments: { command: "false" } });
    state = emit(state, "tool.finished", { call_id: "c", name: "execute_command", ...result });
    const liveTool = state.timeline!.find((item) => item.call_id === "c")!;
    expect(liveTool.result).toEqual(result);
    rerender(toolCardFromTimeline(liveTool));
    expect(container.textContent).toBe(snapshotText);
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
    expect(screen.getByText("5 entries (24ms)")).toBeTruthy();
    rerender(toolCardFromState({ call_id: "read", name: "read_file", phase: "succeeded", data: "line one\nline two" }));
    expect(screen.queryByText("2 entries")).toBeNull();
    // The real tool returns structured entries; count those, not JSON lines.
    rerender(toolCardFromTimeline({
      id: "dir2", kind: "tool", name: "list_directory", phase: "succeeded", arguments: {},
      result: { ok: true, data: { directory: ".", entries: [{ name: "a" }, { name: "src" }], truncated: false } },
    }));
    expect(screen.getByText("2 entries")).toBeTruthy();
  });
});

describe("projectRelative", () => {
  it("keeps project paths and drops everything outside the project", async () => {
    const { projectRelative } = await import("./widgets");
    expect(projectRelative("src/a.ts", "/p")).toBe("src/a.ts");
    expect(projectRelative("/p/src/a.ts", "/p")).toBe("src/a.ts");
    expect(projectRelative("/etc/hosts", "/p")).toBeNull();
    expect(projectRelative("../other/a.ts", "/p")).toBeNull();
    expect(projectRelative("C:\\work\\p\\src\\a.ts", "C:\\work\\p")).toBe("src/a.ts");
    expect(projectRelative("D:\\elsewhere\\a.ts", "C:\\work\\p")).toBeNull();
  });
});
