import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Inspector } from "../inspector/Inspector";
import type { ViewState } from "../../types";
import { MemoryPage, RulesPage, SchedulesPage, SearchDialog, SettingsPage } from "./panels";

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

  it("searches as the person types and offers content search", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ results: [{ kind: "content", path: "a.py", line: 2, text: "return a" }] }) }));
    vi.stubGlobal("fetch", fetchMock);
    render(<SearchDialog sessionId={null} projectId="p" close={() => undefined} openFile={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Search kind"), { target: { value: "content" } });
    fireEvent.change(screen.getByLabelText("Search query"), { target: { value: "return" } });
    expect(await screen.findByText("1 result")).toBeTruthy();
    expect(screen.getByText("return a")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("shows the current core memory section so saving does not silently replace it", async () => {
    const puts: Array<Record<string, unknown>> = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT") puts.push(JSON.parse(String(init.body)));
      return { ok: true, statusText: "OK", json: async () => ({
        core: { persona: "Persona", human_profile: "Prefers Chinese replies.", project_anchor: "Wright is a coding agent.", persona_editable: false },
        semantic: [], episodes: [],
      }) };
    }));
    render(<MemoryPage projectId="ws" />);
    expect(await screen.findByText("Prefers Chinese replies.")).toBeTruthy();
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe("/api/v1/workspaces/ws/memory");
    expect(screen.getByText("Wright is a coding agent.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit Project anchor" }));
    const editor = screen.getByRole("textbox", { name: "Project anchor" }) as HTMLTextAreaElement;
    expect(editor.value).toBe("Wright is a coding agent.");
    fireEvent.change(editor, { target: { value: "Wright is a coding agent. Uses DDD layers." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toMatchObject({ section: "project_anchor", content: "Wright is a coding agent. Uses DDD layers.", mode: "replace" });
  });

  it("explains that memory needs a workspace instead of showing an unusable form", () => {
    render(<MemoryPage projectId={null} />);
    expect(screen.getByText("No project is selected.")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("tablist")).toBeNull();
  });

  it("lists semantic memories under their tab and edits one in place", async () => {
    const patches: Array<Record<string, unknown>> = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") patches.push(JSON.parse(String(init.body)));
      return { ok: true, statusText: "OK", json: async () => ({
        core: null,
        semantic: [{ id: "m1", name: "No global pip", content: "Use uv, never pip install globally.", type: "feedback", revision: 2, status: "active" }],
        episodes: [],
      }) };
    }));
    render(<MemoryPage projectId="ws" />);
    fireEvent.click(await screen.findByRole("tab", { name: /Semantic\s*1/ }));
    expect(screen.getByText("Use uv, never pip install globally.")).toBeTruthy();
    expect(screen.getByText("rev 2")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Actions for No global pip" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Edit" }));
    const editor = screen.getByLabelText("Replace No global pip") as HTMLTextAreaElement;
    expect(editor.value).toBe("Use uv, never pip install globally.");
    fireEvent.change(editor, { target: { value: "Use uv only." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toMatchObject({ content: "Use uv only.", expected_revision: 2 });
  });

  it("requires a rule id and body before saving", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, statusText: "OK", json: async () => ({ rules: [] }) })));
    render(<RulesPage projectId="ws" />);
    expect(await screen.findByText("No rules.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New rule" }));
    fireEvent.click(screen.getByRole("button", { name: "Save rule" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Rule id and body are required.");
  });

  it("hides the rule form until a workspace is selected", () => {
    render(<RulesPage projectId={null} />);
    expect(screen.getByText("No project is selected.")).toBeTruthy();
    expect(screen.queryByLabelText("Rule id")).toBeNull();
    expect(screen.queryByRole("button", { name: "New rule" })).toBeNull();
  });

  it("describes schedule intervals in words and pauses with the switch", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      return { ok: true, statusText: "OK", json: async () => (init?.method === "POST" ? {} : [
        { id: "j1", name: "Nightly check", prompt: "Run the test suite", status: "active", trigger: { type: "interval", every_seconds: 7200 } },
      ]) };
    }));
    render(<SchedulesPage sessionId="s" projectId="p" />);
    expect(await screen.findByText("Every 2 hours")).toBeTruthy();
    const toggle = screen.getByRole("switch", { name: "Run Nightly check" });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(toggle);
    await vi.waitFor(() => expect(calls.some((call) => call.startsWith("POST") && call.endsWith("/schedules/j1/pause"))).toBe(true));
  });

  it("creates a schedule with an interval in hours", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") bodies.push(JSON.parse(String(init.body)));
      return { ok: true, statusText: "OK", json: async () => (init?.method === "POST" ? {} : []) };
    }));
    render(<SchedulesPage sessionId="s" projectId="p" />);
    fireEvent.click(await screen.findByRole("button", { name: "New schedule" }));
    fireEvent.change(screen.getByLabelText("Schedule name"), { target: { value: "Digest" } });
    fireEvent.change(screen.getByLabelText("Schedule prompt"), { target: { value: "Summarize changes" } });
    fireEvent.change(screen.getByLabelText("Run every"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Interval unit"), { target: { value: "hours" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    await vi.waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ name: "Digest", trigger: { type: "interval", every_seconds: 10800 } });
  });

  it("saves settings from segmented controls", async () => {
    const save = vi.fn(async () => undefined);
    render(<SettingsPage theme="light" inspectorOpen save={save} />);
    fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
    fireEvent.click(screen.getByRole("switch", { name: "Inspector open" }));
    await vi.waitFor(() => expect(save).toHaveBeenCalledTimes(2));
    expect(save).toHaveBeenNthCalledWith(1, { theme: "dark" });
    expect(save).toHaveBeenNthCalledWith(2, { inspector_open: false });
  });

  it("shows a server revert conflict and does not send a second overwrite", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (String(url).includes("/review/revert")) {
        return { ok: true, statusText: "OK", json: async () => ({ applied: false, results: [{ path: "a.txt", state: "conflict", result: "bytes changed outside this task" }] }) };
      }
      if (String(url).endsWith("/review")) {
        return { ok: true, statusText: "OK", json: async () => ({ revision: "session-r1", changes: [{ path: "a.txt", state: "task", origin: "task", can_revert: true }] }) };
      }
      return { ok: true, statusText: "OK", json: async () => ({ local_warning: false, baseline: "head", changes: [{ path: "a.txt", status: "M" }] }) };
    }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Inspector state={view("session-a")} sessionId="session-a" open close={() => undefined} />);
    fireEvent.click(screen.getByRole("tab", { name: /changes/i }));
    fireEvent.click(screen.getByRole("radio", { name: "This session" }));
    fireEvent.click(await screen.findByRole("button", { name: /Undo \d+ available changes/ }));
    expect(await screen.findByText(/bytes changed outside this task/)).toBeDefined();
    expect(calls.filter((item) => item.includes("/review/revert"))).toHaveLength(1);
  });
});
