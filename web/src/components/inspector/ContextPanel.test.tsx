import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api";
import { setLocale } from "../../i18n";
import type { ContextBreakdown, McpServerView, ViewState } from "../../types";
import { ContextPanel } from "./ContextPanel";

const usage = (extra: Partial<ViewState["usage"]> = {}): ViewState["usage"] => ({
  prompt_tokens: 20400, completion_tokens: 815, total_tokens: 21215,
  request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
  context_tokens: null, context_limit: null, ...extra,
});
const breakdown: ContextBreakdown = { total: 18200, limit: 200000, exact: true, categories: [
  { id: "system_prompt", tokens: 3100, share: 0.17 }, { id: "tool_schemas", tokens: 12400, share: 0.68 },
  { id: "history", tokens: 2700, share: 0.15 },
] };

beforeEach(() => { setLocale("en"); try { localStorage.clear(); } catch { /* none */ } });
afterEach(() => { cleanup(); vi.restoreAllMocks(); setLocale("en"); });

describe("context panel", () => {
  it("shows the model and what is left beside the total", () => {
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly={false} model="claude-opus-5-5" />);
    expect(document.querySelector(".ctx-sub")?.textContent).toBe("claude-opus-5-5 · 181.8K left");
    expect(document.querySelector(".ctx-big b")?.textContent).toBe("9%");
  });

  it("shows four usage cards, with a dash for figures the server has not sent", () => {
    const { rerender } = render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly={false} />);
    const cards = () => [...document.querySelectorAll(".ctx-stats > div")].map((card) => [card.querySelector("dt")?.textContent, card.querySelector("dd")?.textContent]);
    expect(cards()).toEqual([["Input", "20.4K"], ["Output", "815"], ["Cache hits", "—"], ["Cost", "—"]]);
    rerender(<ContextPanel breakdown={breakdown} usage={usage({ prompt_tokens: 400000, cache_read_tokens: 308000, cost_usd: 0.2849 })} historyOnly={false} />);
    expect(cards().slice(2)).toEqual([["Cache hits", "308K77%"], ["Cost", "$0.28"]]);
    // Totals come from the provider, so they are not called an estimate.
    expect(document.querySelector(".ctx-usage .section-count")?.textContent).toBe("provider count");
  });

  it("anchors the total on the provider's count and shows how far the estimate was off", () => {
    render(<ContextPanel breakdown={{ ...breakdown, used: 18200, calibrated_used: 20000, anchor: { actual: 19000, estimated: 17100 } }} usage={usage()} historyOnly={false} />);
    expect(document.querySelector(".ctx-num")?.textContent).toBe("20K / 200K");
    expect(document.querySelector(".ctx-anchor")?.textContent).toBe("Last request 19K by provider count · local estimate −10%");
    expect(document.querySelector(".ctx-section .section-count")?.textContent).toBe("calibrated");
  });

  it("marks where folding starts and draws the request trend", () => {
    render(<ContextPanel breakdown={{ ...breakdown, fold_at: 150000, history: [{ estimated: 9000, actual: 9500 }, { estimated: 18200 }] }} usage={usage()} historyOnly={false} />);
    expect((document.querySelector(".ctx-fold-mark") as HTMLElement).style.left).toBe("75%");
    expect(screen.getByText("Older tool results fold past 150K")).toBeTruthy();
    expect(screen.getByRole("img", { name: "Last 2 requests" })).toBeTruthy();
    expect(document.querySelector(".ctx-trend-head .ctx-legend-tokens")?.textContent).toBe("9.5K → 18.2K");
  });

  it("offers compact only when the server registers the command, and puts it in the composer", async () => {
    const onCommand = vi.fn();
    const commands = vi.spyOn(api, "commands").mockResolvedValue([{ name: "/compact", description: "", usage: "" }, { name: "/help", description: "", usage: "" }]);
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly={false} onCommand={onCommand} />);
    await waitFor(() => expect(commands).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: "Compact context" }));
    expect(onCommand).toHaveBeenCalledWith("/compact");
    expect(screen.queryByRole("button", { name: "Clear" })).toBeNull();
  });

  it("shows no command buttons when the server has none of them", async () => {
    const commands = vi.spyOn(api, "commands").mockResolvedValue([{ name: "/help", description: "", usage: "" }]);
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly={false} onCommand={vi.fn()} />);
    await waitFor(() => expect(commands).toHaveBeenCalled());
    expect(document.querySelector(".ctx-actions")).toBeNull();
  });
});

describe("context categories", () => {
  const grouped: ContextBreakdown = {
    total: 60000, used: 44000, limit: 128000, folded: { count: 4, tokens: 90000 },
    categories: [
      { id: "system_prompt", group: "fixed", tokens: 3000, share: 0.05, count: 1, items: [{ label: "System prompt", key: "system_prompt", tokens: 3000 }] },
      { id: "tool_schemas", group: "fixed", tokens: 6000, share: 0.1, count: 1, items: [{ label: "read_file", tokens: 400 }] },
      { id: "tool_output", group: "conversation", tokens: 35000, share: 0.58, count: 12, items: [
        { label: "shell", detail: "pytest -q", tokens: 30000, call_id: "call-1" },
        { label: "read_file", detail: "src/app.py", tokens: 20, folded: true, call_id: "call-2" },
      ], by_tool: [{ label: "shell", count: 9, tokens: 34000 }, { label: "read_file", count: 3, tokens: 1000 }] },
      { id: "mcp_schemas", group: "fixed", tokens: 900, share: 0.01, count: 1, items: [{ label: "demo", mcp: "demo", count: 2, tokens: 900 }] },
      { id: "output_reserve", group: "reserve", tokens: 16000, share: 0.27 },
      { id: "plan", group: "conversation", tokens: 0, share: 0 },
    ],
  };

  it("groups categories and keeps the reply reserve out of the used total", () => {
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} />);
    expect([...document.querySelectorAll(".ctx-group-head > span:first-of-type")].map((item) => item.textContent))
      .toEqual(["Sent every request", "Conversation", "Kept free"]);
    expect(document.querySelector(".ctx-num")?.textContent).toBe("44K / 128K");
    expect(screen.getByText("4 older tool results are folded for this request, saving 90K.")).toBeTruthy();
    expect(document.querySelector(".ctx-bar i.reserve")).toBeTruthy();
  });

  it("lists categories in request order, not by size", () => {
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} />);
    fireEvent.click(screen.getByRole("button", { name: /Sent every request/ }));
    expect([...document.querySelectorAll(".ctx-group.fixed .ctx-legend-label")].map((item) => item.textContent))
      .toEqual(["System prompt", "Tool definitions", "MCP tool definitions"]);
  });

  it("remembers the order picked for a category", async () => {
    const loadDetail = vi.fn().mockResolvedValue({ tokens: 3000, items: [
      { id: "a", label: "role", tokens: 10, seq: 1, content: "x" }, { id: "b", label: "rules", tokens: 900, seq: 2, content: "y" },
    ] });
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} loadDetail={loadDetail} />);
    fireEvent.click(screen.getByRole("button", { name: /Sent every request/ }));
    fireEvent.click(screen.getByRole("button", { name: /System prompt/ }));
    // The system prompt reads top to bottom, so it opens in order.
    expect(screen.getByRole("radio", { name: "In order" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("radio", { name: "Largest" }));
    cleanup();
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} loadDetail={loadDetail} />);
    fireEvent.click(screen.getByRole("button", { name: /Sent every request/ }));
    fireEvent.click(screen.getByRole("button", { name: /System prompt/ }));
    expect(screen.getByRole("radio", { name: "Largest" }).getAttribute("aria-checked")).toBe("true");
    await screen.findByText("2 items");
  });

  it("opens the whole request from the gauge and from an item", async () => {
    const items = [
      { id: "system:role", label: "role", tokens: 15, seq: 0, category: "system_prompt", content: "You are a coding assistant." },
      { id: "m1", label: "fix the bug", tokens: 8, seq: 1, turn: 0, category: "user", content: "fix the bug" },
      { id: "m2:call:c1", label: "shell", detail: "pytest -q", tokens: 30000, seq: 2, turn: 0, category: "tool_output", call_id: "c1", content: "3 passed" },
    ];
    const loadRequest = vi.fn().mockImplementation(async (source: string, raw?: boolean) => ({
      source, has_last: false, items, tokens: 30023, limit: 128000, turns: [{ index: 0, label: "fix the bug", tokens: 30008, parts: { text: 8, reasoning: 0, tool_calls: 0, tool_results: 30000 } }],
      ...(raw ? { request: { messages: [{ role: "system" }], tools: [] } } : {}),
    }));
    const loadDetail = vi.fn().mockResolvedValue({ tokens: 30000, items: [items[2]] });
    render(<ContextPanel breakdown={{ ...grouped, preview: true }} usage={usage()} historyOnly={false} loadDetail={loadDetail} loadRequest={loadRequest} />);
    fireEvent.click(screen.getByRole("button", { name: /View full request/ }));
    const dialog = await screen.findByRole("dialog", { name: "Full request" });
    expect(loadRequest).toHaveBeenCalledWith("next");
    await screen.findAllByText("Turn 1 · fix the bug");
    expect([...dialog.querySelectorAll(".ctx-request-toc-label")].map((item) => item.textContent)).toEqual(["System prompt", "Turn 1 · fix the bug"]);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search the request" }), { target: { value: "passed" } });
    expect(screen.getByText("1 matches")).toBeTruthy();
    expect(dialog.querySelectorAll(".ctx-request-main .ctx-item")).toHaveLength(1);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    fireEvent.click(screen.getByRole("button", { name: /Copy JSON/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(JSON.parse(writeText.mock.calls[0][0])).toEqual({ messages: [{ role: "system" }], tools: [] });
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();

    // From an item in a category: the dialog opens with that item expanded.
    fireEvent.click(screen.getByRole("button", { name: /Tool results/ }));
    await screen.findByText("1 item");
    fireEvent.click(screen.getByRole("button", { name: "Show in full request" }));
    await screen.findByRole("dialog");
    await waitFor(() => expect(document.querySelector('[data-anchor="m2:call:c1"] .ctx-content pre')?.textContent).toBe("3 passed"));
  });

  it("keeps what every request carries folded until asked", () => {
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} />);
    expect(screen.queryByRole("button", { name: /^Tool definitions/ })).toBeNull();
    expect(screen.getByRole("button", { name: /Tool results/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Sent every request/ }));
    expect(screen.getByRole("button", { name: /MCP tool definitions/ })).toBeTruthy();
  });

  it("opens a category in the detail pane with every item, its content, and a jump to the call", async () => {
    const onShowCall = vi.fn();
    const loadDetail = vi.fn().mockResolvedValue({ tokens: 35000, items: [
      { id: "t1", label: "shell", detail: "pytest -q", tokens: 30000, call_id: "call-1", seq: 2, content: "3 passed" },
      { id: "t2", label: "read_file", detail: "src/app.py", tokens: 20, folded: true, call_id: "call-2", seq: 1, content: "[folded]" },
    ] });
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} onShowCall={onShowCall} loadDetail={loadDetail} />);
    fireEvent.click(screen.getByRole("button", { name: /Tool results/ }));
    expect(loadDetail).toHaveBeenCalledWith({ category: "tool_output" });
    expect(screen.getByRole("heading", { name: "Tool results" })).toBeTruthy();
    expect([...document.querySelectorAll(".ctx-tool-sum")].map((chip) => chip.textContent)).toEqual(["shell ×9 · 34K", "read_file ×3 · 1K"]);
    await screen.findByText("2 items");
    const rows = () => [...document.querySelectorAll(".ctx-detail-list .ctx-item")].map((row) => row.textContent);
    expect(rows()).toEqual(["shellpytest -q30K", "read_filesrc/app.pyfolded20"]);
    fireEvent.click(screen.getByRole("radio", { name: "In order" }));
    expect(rows()[0]).toContain("read_file");
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter items" }), { target: { value: "passed" } });
    expect(rows()).toEqual(["shellpytest -q30K"]);
    fireEvent.click(screen.getByRole("button", { name: /shellpytest/ }));
    expect(document.querySelector(".ctx-content pre")?.textContent).toBe("3 passed");
    fireEvent.click(screen.getByRole("button", { name: "Show in conversation" }));
    expect(onShowCall).toHaveBeenCalledWith("call-1");
    fireEvent.click(screen.getByRole("button", { name: "Context" }));
    expect(screen.getByRole("button", { name: /Tool results/ })).toBeTruthy();
  });

  it("falls back to the largest items when the full list cannot be fetched", () => {
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly />);
    fireEvent.click(screen.getByRole("button", { name: /Tool results/ }));
    expect(screen.getByText(/Full content is available while the session is open/)).toBeTruthy();
    expect(document.querySelectorAll(".ctx-detail-list .ctx-item")).toHaveLength(2);
    expect(document.querySelector(".ctx-detail-list button.ctx-item")).toBeNull();
  });

  it("lists the conversation by turn, marks the heaviest, and opens a turn", async () => {
    const turn = (index: number, tokens: number) => ({ index, label: `task ${index + 1}`, tokens, parts: { text: tokens / 2, reasoning: 0, tool_calls: tokens / 4, tool_results: tokens / 4 } });
    const turns = [turn(0, 400), turn(1, 8000), turn(2, 1200), turn(3, 6000), turn(4, 200)];
    const loadDetail = vi.fn().mockResolvedValue({ tokens: 8000, turn: turns[1], items: [{ id: "u", label: "task 2", tokens: 4000, category: "user", seq: 1, content: "task 2" }] });
    render(<ContextPanel breakdown={{ ...grouped, turns }} usage={usage()} historyOnly={false} loadDetail={loadDetail} />);
    // By type is the default view.
    expect(document.querySelector(".ctx-turn-list")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "By turn" }));
    expect([...document.querySelectorAll(".ctx-turn-label")].map((item) => item.textContent)).toEqual(["task 1", "task 2", "task 3", "task 4", "task 5"]);
    expect([...document.querySelectorAll(".ctx-turn-list li.heavy .ctx-turn-label")].map((item) => item.textContent)).toEqual(["task 2", "task 3", "task 4"]);
    fireEvent.click(screen.getByRole("button", { name: /task 2/ }));
    expect(loadDetail).toHaveBeenCalledWith({ turn: 1 });
    expect(screen.getByRole("heading", { name: "Turn 2" })).toBeTruthy();
    await screen.findByText("1 item");
  });

  it("offers the turn view only when the breakdown has turns", () => {
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} />);
    expect(screen.queryByRole("radio", { name: "By turn" })).toBeNull();
  });

  it("shows only recent turns until asked for all of them", () => {
    const turns = Array.from({ length: 20 }, (_, index) => ({ index, label: `t${index}`, tokens: 100, parts: { text: 100, reasoning: 0, tool_calls: 0, tool_results: 0 } }));
    render(<ContextPanel breakdown={{ ...grouped, turns }} usage={usage()} historyOnly={false} />);
    fireEvent.click(screen.getByRole("radio", { name: "By turn" }));
    expect(document.querySelectorAll(".ctx-turn-list li")).toHaveLength(12);
    fireEvent.click(screen.getByRole("button", { name: "Show all 20 turns" }));
    expect(document.querySelectorAll(".ctx-turn-list li")).toHaveLength(20);
  });

  it("hides empty categories instead of listing them", () => {
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} />);
    expect(screen.queryByText(/Plan/)).toBeNull();
    expect(screen.queryByText(/Empty:/)).toBeNull();
  });

  it("reads breakdowns saved before categories had groups", () => {
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly={false} />);
    expect([...document.querySelectorAll(".ctx-group-head > span:first-of-type")].map((item) => item.textContent))
      .toEqual(["Sent every request", "Conversation"]);
  });
});

describe("skills", () => {
  it("lists skills with a switch and saves the change", async () => {
    const loadSkills = vi.fn().mockResolvedValue([
      { id: "release-check", name: "release-check", description: "before a release", enabled: true },
      { id: "triage", name: "triage", description: "", enabled: false },
    ]);
    const onToggleSkill = vi.fn().mockResolvedValue([
      { id: "release-check", name: "release-check", description: "before a release", enabled: false },
      { id: "triage", name: "triage", description: "", enabled: false },
    ]);
    const loaded: ContextBreakdown = { ...breakdown, categories: [...breakdown.categories!, { id: "loaded_skills", group: "conversation", tokens: 500, share: 0.01, items: [{ label: "triage", tokens: 500, loaded: true }] }] };
    render(<ContextPanel breakdown={loaded} usage={usage()} historyOnly={false} loadSkills={loadSkills} onToggleSkill={onToggleSkill} />);
    const toggle = await screen.findByRole("switch", { name: "Turn release-check on or off" });
    expect(document.querySelector(".ctx-skills .section-count")?.textContent).toBe("1/2");
    // A skill turned off after it was loaded still has its body in the conversation.
    expect(screen.getByText("off · body still in context")).toBeTruthy();
    fireEvent.click(toggle);
    expect(onToggleSkill).toHaveBeenCalledWith("release-check", false);
    await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("false"));
  });

  it("is absent for a saved session", () => {
    const loadSkills = vi.fn();
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly loadSkills={loadSkills} />);
    expect(loadSkills).not.toHaveBeenCalled();
  });
});

describe("MCP servers", () => {
  const servers: McpServerView[] = [
    { name: "github", status: "connected", tool_count: 14, tokens: 3200, toggleable: true },
    { name: "postgres", status: "connected", tool_count: 6 },
    { name: "browser", status: "auth_required", tool_count: 9, error: "token expired" },
  ];

  it("is absent until the server sends the list", () => {
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly={false} />);
    expect(screen.queryByText("MCP servers")).toBeNull();
  });

  it("lists each server with its tools, context cost and state", () => {
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly={false} mcpServers={servers} onToggleMcp={vi.fn()} />);
    const rows = [...document.querySelectorAll(".mcp-row")];
    expect(rows.map((row) => row.textContent)).toEqual(["github 14 tools · ~3.2K", "postgres 6 tools", "browser 9 tools · needs sign-in again"]);
    expect(rows[2].classList.contains("auth_required")).toBe(true);
    expect(rows[2].getAttribute("title")).toBe("token expired");
    // A server that needs signing in again is not counted as in use.
    expect(document.querySelector(".ctx-mcp .section-count")?.textContent).toBe("2/3");
  });

  it("offers a switch only for servers that accept one, and reports a failed change", async () => {
    const onToggle = vi.fn().mockRejectedValue(new Error("not supported"));
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly={false} mcpServers={servers} onToggleMcp={onToggle} />);
    expect(screen.getAllByRole("switch")).toHaveLength(1);
    fireEvent.click(screen.getByRole("switch", { name: "Enable github" }));
    expect(onToggle).toHaveBeenCalledWith("github", false);
    expect((await screen.findByRole("alert")).textContent).toBe("not supported");
  });

  it("has no switches for a session that is only being browsed", () => {
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly mcpServers={servers} onToggleMcp={vi.fn()} />);
    expect(screen.queryByRole("switch")).toBeNull();
  });
});
