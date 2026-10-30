import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Inspector } from "../inspector/Inspector";
import type { ViewState } from "../../types";
import { RulesDialog, SearchDialog } from "./panels";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const view = (sessionId: string): ViewState => ({
  stream_id: "stream",
  last_seq: 0,
  session: { session_id: sessionId, status: "idle", active: true },
  history: [],
  active_turn: null,
  plan: {},
  pending_interactions: [],
  notices: [],
  queued_commands: [],
  queue_depth: 0,
  usage: {
    prompt_tokens: null, completion_tokens: null, total_tokens: null,
    request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
    context_tokens: null, context_limit: null,
  },
  seen: [],
  connection: "connected",
});

describe("workspace panels", () => {
  it("drops old search responses after changing workspace and contains Escape inside search", async () => {
    let release: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => { release = resolve; });
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => pending })));
    const close = vi.fn();
    const escaped = vi.fn();
    window.addEventListener("keydown", escaped);
    const props = { sessionId: null, close, openFile: () => undefined };
    const { rerender } = render(<SearchDialog {...props} projectId="a" />);
    fireEvent.change(screen.getByLabelText("Search query"), { target: { value: "readme" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    rerender(<SearchDialog {...props} projectId="b" />);
    release({ results: [{ kind: "file", path: "old-workspace.md" }] });
    await Promise.resolve(); await Promise.resolve();
    expect(screen.queryByText("file old-workspace.md")).toBeNull();
    fireEvent.keyDown(screen.getByLabelText("Search query"), { key: "Escape" });
    expect(close).toHaveBeenCalledOnce();
    expect(escaped).not.toHaveBeenCalled();
    window.removeEventListener("keydown", escaped);
  });

  it("opens a matching task from search results", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ results: [{ kind: "task", session_id: "saved", title: "Fix inspector" }] }) })));
    const openSession = vi.fn();
    render(<SearchDialog sessionId={null} projectId="p" close={() => undefined} openFile={() => undefined} openSession={openSession} />);
    fireEvent.change(screen.getByLabelText("Search query"), { target: { value: "inspector" } });
    fireEvent.change(screen.getByLabelText("Search kind"), { target: { value: "task" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: "task Fix inspector" }));
    expect(openSession).toHaveBeenCalledWith("saved");
  });

  it("previews a workspace file before any session exists", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).includes("/file?") ? { path: "README.md", content: "Workspace file content", binary: false } : { results: [{ kind: "file", path: "README.md" }] } })));
    const openFile = vi.fn();
    render(<SearchDialog sessionId={null} projectId="p" close={() => undefined} openFile={openFile} previewFiles />);
    fireEvent.change(screen.getByLabelText("Search query"), { target: { value: "readme" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: "file README.md" }));
    expect(await screen.findByText("Workspace file content")).toBeTruthy();
    expect(openFile).not.toHaveBeenCalled();
  });

  it("rejects an empty search before calling the server", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<SearchDialog sessionId="s" projectId="p" close={() => undefined} openFile={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Enter a search query.");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("requires a rule id and body before saving", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, statusText: "OK", json: async () => ({ rules: [] }) })));
    render(<RulesDialog sessionId="s" close={() => undefined} />);
    fireEvent.click(await screen.findByRole("button", { name: "Save rule" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Rule id and body are required.");
  });

  it("shows a server revert conflict and does not send a second overwrite", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (String(url).includes("/review/revert")) {
        return { ok: true, statusText: "OK", json: async () => ({ applied: false, results: [{ path: "a.txt", state: "conflict", result: "bytes changed outside this task" }] }) };
      }
      if (String(url).endsWith("/review")) {
        return { ok: true, statusText: "OK", json: async () => ({ changes: [{ path: "a.txt", state: "task" }] }) };
      }
      return { ok: true, statusText: "OK", json: async () => ({ local_warning: false, baseline: "head", changes: [{ path: "a.txt", status: "M" }] }) };
    }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Inspector state={view("session-a")} sessionId="session-a" open close={() => undefined} />);
    fireEvent.click(screen.getByRole("tab", { name: /changes/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Revert" }));
    expect(await screen.findByText(/bytes changed outside this task/)).toBeDefined();
    expect(calls.filter((item) => item.includes("/review/revert"))).toHaveLength(1);
  });
});
