import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { api } from "../../api";
import { applyEvent } from "../../reducer";
import type { UiEvent, ViewState } from "../../types";
import { Timeline } from "./Timeline";
import { toolCardFromTimeline, toolCardFromState } from "../cards/cards";

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
  it("stays pinned when content grows before a programmatic scroll event lands", () => {
    const state = emit(fresh(), "turn.started", { prompt: "Long answer" });
    const { container } = render(transcript(state));
    const scroller = container.querySelector(".conversation-scroll") as HTMLElement;
    const metrics = { top: 0, height: 1000 };
    Object.defineProperty(scroller, "clientHeight", { configurable: true, get: () => 400 });
    Object.defineProperty(scroller, "scrollHeight", { configurable: true, get: () => metrics.height });
    Object.defineProperty(scroller, "scrollTop", { configurable: true, get: () => metrics.top, set: (value: number) => { metrics.top = value; } });

    // Scrolled to the old bottom (600), but the content grew by 50px first.
    metrics.top = 600;
    metrics.height = 1050;
    fireEvent.scroll(scroller);
    expect(screen.queryByText("Jump to latest")).toBeNull();

    // A real upward scroll still releases the pin.
    metrics.top = 300;
    fireEvent.scroll(scroller);
    expect(screen.getByText("Jump to latest")).toBeTruthy();
  });

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
    expect(container.querySelectorAll(".msg-row.assistant-msg")).toHaveLength(2);
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

  it("animates only items that arrive after the session loaded, once per item", () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1_000);
    let state = emit(fresh(), "turn.started", { prompt: "First" });
    state = emit(state, "content.final", { content: "Loaded answer" });
    state = emit(state, "turn.completed", {});
    const { container, rerender } = render(transcript(state));
    expect(container.querySelectorAll(".enter")).toHaveLength(0);

    state = emit(state, "turn.started", { prompt: "Second" }, "turn-2");
    state = emit(state, "tool.planned", { call_id: "c2", name: "list_directory", arguments: { path: "src" } }, "turn-2");
    rerender(transcript(state));
    const live = screen.getByText("list_directory").closest(".tl-indent")!;
    expect(live.classList.contains("enter")).toBe(true);
    expect(screen.getByText("Loaded answer").closest(".enter")).toBeNull();

    // Long after it appeared, the same tool moving into the record must not replay.
    now.mockReturnValue(5_000);
    state = emit(state, "tool.completed", { call_id: "c2", name: "list_directory", result: { ok: true, data: { entries: [] } } }, "turn-2");
    state = emit(state, "turn.completed", {}, "turn-2");
    rerender(transcript(state));
    expect(screen.getByText("list_directory").closest(".tl-indent")!.classList.contains("enter")).toBe(false);
  });

  it("renders replies without a header and marks tool cards open only when expanded", () => {
    let state = emit(fresh(), "turn.started", { prompt: "List it" });
    state = emit(state, "tool.planned", { call_id: "c", name: "list_directory", arguments: { path: "src" } });
    state = emit(state, "tool.completed", { call_id: "c", name: "list_directory", result: { ok: true, data: { entries: ["a", "b"] } } });
    state = emit(state, "content.final", { content: "Two files" });
    state = emit(state, "turn.completed", {});
    const { container } = render(transcript(state));
    const reply = screen.getByText("Two files").closest(".msg-card")!;
    expect(reply.querySelector(".msg-card-head")).toBeNull();
    const card = container.querySelector(".tool-card:not(.reasoning-card)")!;
    expect(card.classList.contains("open")).toBe(false);
    fireEvent.click(card.querySelector("button")!);
    expect(card.classList.contains("open")).toBe(true);
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
    // The finished run of tools folds away; the reasoning the person opened stays.
    const group = container.querySelector(".step-group")!;
    expect(group.classList.contains("open")).toBe(false);
    expect(container.querySelectorAll(".reasoning-card")).toHaveLength(1);
    expect(first.classList.contains("open")).toBe(true);
    fireEvent.click(group.querySelector(".step-group-head")!);
    expect(container.querySelectorAll(".reasoning-card")).toHaveLength(2);
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
    fireEvent.click(container.querySelector(".step-group-head")!);
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

  it("explains a sandbox denial and grants the directory read-only on request", async () => {
    vi.spyOn(api, "grants").mockResolvedValue({ version: "v1" } as never);
    const addGrant = vi.spyOn(api, "addGrant").mockResolvedValue({} as never);
    const data = { returncode: 1, output: "ls: /outside: Operation not permitted\n", sandbox_denial: { path: "/outside", grant_directory: "/outside", readable: ["/work"], writable: ["/work"] } };
    render(toolCardFromState({ call_id: "c", name: "execute_command", phase: "failed", arguments: { command: "ls /outside" }, data }, "s"));
    expect(screen.getByRole("status").textContent).toContain("/work");
    fireEvent.click(screen.getByRole("button", { name: /read-only|只读/ }));
    await waitFor(() => expect(addGrant).toHaveBeenCalledWith("s", { kind: "directory", path: "/outside", operations: ["file_read"] }, "session", "v1"));
  });

  it("grants a directory read-write when the sandbox blocked a write", async () => {
    vi.spyOn(api, "grants").mockResolvedValue({ version: "v1" } as never);
    const addGrant = vi.spyOn(api, "addGrant").mockResolvedValue({} as never);
    const data = { returncode: 1, output: "touch: /lib/x: Read-only file system\n", sandbox_denial: { path: "/lib/x", grant_directory: "", grant_write_directory: "/lib", readable: ["/work", "/lib"], writable: ["/work"] } };
    render(toolCardFromState({ call_id: "c", name: "execute_command", phase: "failed", arguments: { command: "touch /lib/x" }, data }, "s"));
    expect(screen.queryByRole("button", { name: /read-only|只读/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /read and write|可读写/ }));
    await waitFor(() => expect(addGrant).toHaveBeenCalledWith("s", { kind: "directory", path: "/lib", operations: ["file_read", "file_write"] }, "session", "v1"));
    expect(await screen.findByText(/readable and writable|已可读写/)).toBeTruthy();
  });

  it("does not invent diff line numbers or file counts from output lines", () => {
    const { container, rerender } = render(toolCardFromState({ call_id: "c", name: "edit_file", phase: "succeeded", data: "-old\n+new" }, undefined, undefined, true));
    expect(container.querySelector(".diff-line")?.textContent).toBe("");
    rerender(toolCardFromState({ call_id: "read", name: "read_file", phase: "succeeded", data: "line one\nline two" }));
    expect(screen.queryByText("2 files")).toBeNull();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/line one/)).toBeTruthy();
  });

  it("opens an edit card only while its turn is live", () => {
    const edit = { call_id: "e", name: "edit_file", phase: "succeeded", arguments: { file: "a.py" }, data: { diff: "-old\n+new", additions: 1, deletions: 1, diff_truncated: false } } as const;
    const { container, rerender } = render(toolCardFromState(edit, undefined, false, true));
    expect(container.querySelector(".tool-card")!.classList.contains("open")).toBe(true);
    rerender(toolCardFromState(edit, undefined, false, false));
    expect(container.querySelector(".tool-card")!.classList.contains("open")).toBe(false);
    fireEvent.click(container.querySelector(".tool-card-head")!);
    expect(container.querySelector(".tool-card")!.classList.contains("open")).toBe(true);
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

  it("folds a finished run of steps into a summary and keeps a live run open", () => {
    let state = emit(fresh(), "turn.started", { prompt: "Look around" });
    state = emit(state, "reasoning.delta", { piece: "Check the tree" });
    state = emit(state, "tool.planned", { call_id: "d", name: "list_directory", arguments: { path: "." } });
    state = emit(state, "tool.finished", { call_id: "d", ok: true, data: { entries: [] } });
    state = emit(state, "tool.planned", { call_id: "g", name: "grep", arguments: { pattern: "x" } });
    state = emit(state, "tool.finished", { call_id: "g", ok: true, data: { matches: [] } });
    state = emit(state, "tool.planned", { call_id: "c", name: "execute_command", arguments: { command: "false" } });
    const { container, rerender } = render(transcript(state));
    expect(container.querySelectorAll(".step-group")).toHaveLength(1);
    expect(container.querySelector(".step-group")!.classList.contains("open")).toBe(true);
    expect(screen.getByText("false")).toBeTruthy();

    state = emit(state, "tool.finished", { call_id: "c", ok: false, err: "boom", data: { returncode: 1 } });
    state = emit(state, "content.final", { content: "Done looking" });
    state = emit(state, "turn.completed", {});
    rerender(transcript(state));
    const group = container.querySelector(".step-group")!;
    expect(group.classList.contains("open")).toBe(false);
    expect(group.querySelector(".step-group-summary")?.textContent).toBe("Listed 1 folder · Searched once · Ran 1 command");
    expect(group.querySelector(".step-group-failed")?.textContent).toBe("1 failed");
    expect(container.querySelector(".tool-card")).toBeNull();
    expect(screen.getByText("Done looking")).toBeTruthy();
  });

  it("shows a lone step without a group around it", () => {
    let state = emit(fresh(), "turn.started", { prompt: "One" });
    state = emit(state, "tool.planned", { call_id: "r", name: "read_file", arguments: { path: "a.py" } });
    state = emit(state, "tool.finished", { call_id: "r", ok: true, data: { content: "1|x" } });
    state = emit(state, "turn.completed", {});
    const { container } = render(transcript(state));
    expect(container.querySelector(".step-group")).toBeNull();
    // The row shows the tool's own name; its icon is coloured by kind.
    expect(screen.getByText("read_file")).toBeTruthy();
    expect(container.querySelector(".step-icon")?.classList.contains("reads")).toBe(true);
  });

  it("lists each user prompt in the outline and ignores headings inside replies", async () => {
    let state = emit(fresh(), "turn.started", { prompt: "Please design # NotHeading and **NotHeading**" });
    state = emit(state, "tool.planned", { call_id: "c", name: "execute_command", arguments: { command: "echo '## NotHeading'" } });
    state = emit(state, "tool.output", { call_id: "c", output: "## Output Not Heading\n**Also Not Heading**" });
    state = emit(state, "tool.finished", { call_id: "c", ok: true });

    // One exchange is not a directory yet, even when the reply is full of headings.
    state = emit(state, "content.final", { content: "## Only One Heading\nSome content." });
    state = emit(state, "turn.completed", {});
    const { container, rerender } = render(transcript(state));
    expect(container.querySelector(".timeline-toc-trigger")).toBeNull();

    state = emit(state, "turn.started", { prompt: "More details" }, "turn-2");
    state = emit(state, "content.final", {
      content: "## Overview\nIntro\n\n**Key Takeaways**\n\nDetails\n\n### Implementation\nCode",
    }, "turn-2");
    rerender(transcript(state));

    const trigger = container.querySelector<HTMLButtonElement>(".timeline-toc-trigger");
    expect(trigger).not.toBeNull();
    expect(container.querySelector(".timeline-toc-trigger-count")?.textContent).toBe("2");

    const tagged = container.querySelectorAll("[data-toc-id]");
    expect(tagged.length).toBe(2);
    expect(tagged[0].classList.contains("msg-row")).toBe(true);
    expect(tagged[0].classList.contains("user-msg")).toBe(true);
    expect(tagged[0].querySelector(".avatar")).toBeNull();
    expect(tagged[0].getAttribute("data-toc-id")).toBe("turn-1:user");
    expect(tagged[1].getAttribute("data-toc-id")).toBe("turn-2:user");
    expect(container.querySelector(".markdown-body [data-toc-id]")).toBeNull();

    fireEvent.click(trigger!);
    expect(container.querySelector(".timeline-toc-panel")).not.toBeNull();
    const items = container.querySelectorAll<HTMLButtonElement>(".timeline-toc-item");
    expect(items.length).toBe(2);
    expect(items[0].textContent).toContain("Please design # NotHeading and **NotHeading**");
    expect(items[1].textContent).toContain("More details");
    expect(items[0].textContent).not.toContain("Only One Heading");
    expect(items[1].textContent).not.toContain("Overview");
    expect(items[1].textContent).not.toContain("Key Takeaways");

    const scrollToMock = vi.fn();
    const scroller = container.querySelector(".conversation-scroll") as HTMLElement;
    scroller.scrollTo = scrollToMock;
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue({
      top: 50, left: 0, bottom: 500, right: 400, width: 400, height: 450, x: 0, y: 50, toJSON: () => {},
    });
    vi.spyOn(tagged[1], "getBoundingClientRect").mockReturnValue({
      top: 250, left: 0, bottom: 270, right: 300, width: 300, height: 20, x: 0, y: 250, toJSON: () => {},
    });

    fireEvent.click(items[1]);
    expect(scrollToMock).toHaveBeenCalledWith({
      top: 156,
      behavior: "smooth",
    });
    expect(container.querySelector(".timeline-toc-panel")).toBeNull();

    fireEvent.click(container.querySelector<HTMLButtonElement>(".timeline-toc-trigger")!);
    expect(container.querySelector(".timeline-toc-panel")).not.toBeNull();

    let stateOther = emit(fresh(), "turn.started", { prompt: "Another session" });
    stateOther = { ...stateOther, session: { ...stateOther.session, session_id: "session-other" } };
    stateOther = emit(stateOther, "content.final", { content: "## Solo Heading\nJust one heading." });
    rerender(transcript(stateOther));

    expect(container.querySelector(".timeline-toc-trigger")).toBeNull();
    expect(container.querySelector(".timeline-toc-panel")).toBeNull();
  });
});

describe("projectRelative", () => {
  it("keeps project paths and drops everything outside the project", async () => {
    const { projectRelative } = await import("../inspector/Inspector");
    expect(projectRelative("src/a.ts", "/p")).toBe("src/a.ts");
    expect(projectRelative("/p/src/a.ts", "/p")).toBe("src/a.ts");
    expect(projectRelative("/etc/hosts", "/p")).toBeNull();
    expect(projectRelative("../other/a.ts", "/p")).toBeNull();
    expect(projectRelative("C:\\work\\p\\src\\a.ts", "C:\\work\\p")).toBe("src/a.ts");
    expect(projectRelative("D:\\elsewhere\\a.ts", "C:\\work\\p")).toBeNull();
  });
});

describe("revealing a tool call", () => {
  const folded = (): ViewState => ({
    ...fresh(),
    timeline: [
      { id: "turn-1:user", kind: "text", role: "user", turn_id: "turn-1", text: "Check" },
      { id: "c1", kind: "tool", call_id: "c1", name: "read_file", phase: "succeeded", arguments: { file: "a.py" } },
      { id: "c2", kind: "tool", call_id: "c2", name: "edit_file", phase: "succeeded", arguments: { file: "a.py" } },
      { id: "turn-1:reply", kind: "text", role: "assistant", turn_id: "turn-1", text: "Done" },
    ],
  });
  const view = (state: ViewState, revealCall: { callId: string; request: number } | null) =>
    <Timeline state={state} respond={async () => true} cancelQueued={() => undefined} revealCall={revealCall} />;

  it("opens the folded group, scrolls to the call and highlights it once per request", async () => {
    const state = folded();
    const { container, rerender } = render(view(state, null));
    expect(container.querySelector(".step-group")!.classList.contains("open")).toBe(false);
    const scroller = container.querySelector(".conversation-scroll") as HTMLElement;
    const scrollTo = vi.fn();
    scroller.scrollTo = scrollTo;
    rerender(view(state, { callId: "c2", request: 1 }));
    await waitFor(() => expect(container.querySelector('[data-call-id="c2"]')?.classList.contains("reveal-flash")).toBe(true));
    expect(container.querySelector(".step-group")!.classList.contains("open")).toBe(true);
    expect(scrollTo).toHaveBeenCalled();
    // The person can fold it again; the same request does not reopen it.
    fireEvent.click(container.querySelector(".step-group-head")!);
    rerender(view(state, { callId: "c2", request: 1 }));
    expect(container.querySelector(".step-group")!.classList.contains("open")).toBe(false);
  });

  it("says so when the call is not in the conversation", async () => {
    const { rerender } = render(view(folded(), null));
    rerender(view(folded(), { callId: "elsewhere", request: 1 }));
    expect(await screen.findByText("That tool call is not in this conversation view.")).toBeTruthy();
  });
});
