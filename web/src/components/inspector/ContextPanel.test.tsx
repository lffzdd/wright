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

beforeEach(() => { setLocale("en"); });
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
    rerender(<ContextPanel breakdown={breakdown} usage={usage({ cache_read_tokens: 308000, cost_usd: 0.2849 })} historyOnly={false} />);
    expect(cards().slice(2)).toEqual([["Cache hits", "308K"], ["Cost", "$0.28"]]);
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
      { id: "tool_schemas", group: "fixed", tokens: 6000, share: 0.1, count: 2, items: [
        { label: "demo", mcp: "demo", count: 2, tokens: 900 }, { label: "read_file", tokens: 400 },
      ] },
      { id: "tool_output", group: "conversation", tokens: 35000, share: 0.58, count: 12, items: [
        { label: "shell", detail: "pytest -q", tokens: 30000, call_id: "call-1" },
        { label: "read_file", detail: "src/app.py", tokens: 20, folded: true, call_id: "call-2" },
      ] },
      { id: "output_reserve", group: "reserve", tokens: 16000, share: 0.27 },
      { id: "plan", group: "conversation", tokens: 0, share: 0 },
    ],
  };

  it("groups categories and keeps the reply reserve out of the used total", () => {
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} />);
    expect([...document.querySelectorAll(".ctx-group-head > span:first-child")].map((item) => item.textContent))
      .toEqual(["Sent every request", "Conversation", "Kept free"]);
    expect(document.querySelector(".ctx-num")?.textContent).toBe("44K / 128K");
    expect(screen.getByText("4 older tool results are folded for this request, saving 90K.")).toBeTruthy();
    expect(document.querySelector(".ctx-bar i.reserve")).toBeTruthy();
  });

  it("opens a category to its largest items and jumps to a tool call", () => {
    const onShowCall = vi.fn();
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} onShowCall={onShowCall} />);
    expect(document.querySelector(".ctx-items")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Tool results/ }));
    const rows = [...document.querySelectorAll(".ctx-items .ctx-item")].map((row) => row.textContent);
    expect(rows).toEqual(["shellpytest -q30K", "read_filesrc/app.pyfolded20"]);
    expect(screen.getByText("10 smaller not listed")).toBeTruthy();
    fireEvent.click(screen.getAllByTitle("Show in conversation")[0]);
    expect(onShowCall).toHaveBeenCalledWith("call-1");
  });

  it("names MCP servers among tool definitions", () => {
    render(<ContextPanel breakdown={grouped} usage={usage()} historyOnly={false} />);
    fireEvent.click(screen.getByRole("button", { name: /Tool definitions/ }));
    expect(screen.getByText("2 tools")).toBeTruthy();
  });

  it("reads breakdowns saved before categories had groups", () => {
    render(<ContextPanel breakdown={breakdown} usage={usage()} historyOnly={false} />);
    expect([...document.querySelectorAll(".ctx-group-head > span:first-child")].map((item) => item.textContent))
      .toEqual(["Sent every request", "Conversation"]);
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
