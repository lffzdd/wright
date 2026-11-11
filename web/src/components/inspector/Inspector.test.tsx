import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api";
import { setLocale } from "../../i18n";
import type { ViewState } from "../../types";
import { Inspector } from "./Inspector";

const view = (sessionId = "a", active = true): ViewState => ({
  stream_id: "s", last_seq: 0, seen: [], connection: "connected",
  session: { session_id: sessionId, status: "idle", active, execution_root: "/project" },
  history: [], active_turn: null, plan: {}, pending_interactions: [], notices: [], queued_commands: [], queue_depth: 0,
  usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null, request_prompt_tokens: null,
    request_completion_tokens: null, request_total_tokens: null, context_tokens: null, context_limit: null },
});
const inspector = (state: ViewState) => <Inspector state={state} sessionId={state.session.session_id} open close={() => undefined} />;
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};
const patch = (text: string) => ({ path: "a.txt", patch: `--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+${text}\n`, truncated: false, binary: false });
const pickChangeScope = (value: "git" | "session") => {
  const label = value === "session" ? "This session" : "Uncommitted changes";
  fireEvent.click(screen.getByRole("radio", { name: label }));
};

beforeEach(() => {
  setLocale("en");
  vi.spyOn(api, "changes").mockImplementation(async (_id, scope) => ({ changes: scope === "staged" ? [] : [{ path: "a.txt", status: "M" }], baseline: "HEAD", local_warning: false }));
  vi.spyOn(api, "review").mockResolvedValue({ changes: [] });
  vi.spyOn(api, "tree").mockResolvedValue({ path: "", entries: [] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); setLocale("en"); });

describe("Inspector task tab", () => {
  it("keeps the goal, the session glance and one hint while nothing has happened", () => {
    render(inspector(view()));
    const cards = [...document.querySelectorAll("#inspector-panel-task .goal-card")];
    expect(cards).toHaveLength(2);
    expect(document.querySelectorAll("#inspector-panel-task .overview-row")).toHaveLength(4);
    expect(screen.getByText("No goal set yet.").closest(".goal-card")).toBeTruthy();
    expect(document.querySelector("#inspector-panel-task .inspector-task-empty")).toBeTruthy();
    for (const text of ["No active plan for this turn.", "No files accessed in this session.", "No subagents"]) {
      expect(screen.queryByText(text)).toBeNull();
    }
  });

  it("shows the session goal when there is no plan objective", () => {
    const state = view();
    render(inspector({ ...state, session: { ...state.session, user_goal: "Ship the redesign" } }));
    expect(screen.getByText("Ship the redesign").closest(".goal-card")).toBeTruthy();
    expect(screen.queryByText("No goal set yet.")).toBeNull();
  });
});

describe("Inspector overview and tab layout", () => {
  it("opens each detail tab from its overview row", () => {
    const state = view(); state.accessed_files = [{ path: "a.ts", access: "read" }];
    render(inspector(state));
    fireEvent.click(screen.getByRole("button", { name: /Files accessed\s*1 files/ }));
    expect(screen.getByRole("tab", { name: /Files/ }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: /Overview/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Permissions/ }));
    expect(screen.getByRole("tab", { name: "Permissions" }).getAttribute("aria-selected")).toBe("true");
  });

  it("shows settings-like tabs as named icons and flags waiting approvals", () => {
    const state = view(); state.pending_interactions = [{ interaction_id: "i", kind: "permission" } as unknown as ViewState["pending_interactions"][number]];
    render(inspector(state));
    const permissions = screen.getByRole("tab", { name: "Permissions" });
    expect(permissions.textContent).toBe("");
    expect(permissions.querySelector(".tab-alert")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Context" }).classList.contains("icon-tab")).toBe(true);
    expect(screen.getByRole("button", { name: /Permissions\s*1 waiting/ })).toBeTruthy();
  });

  it("switches to a tab another part of the window asks for", () => {
    const { rerender } = render(<Inspector state={view()} sessionId="a" open close={() => undefined} />);
    rerender(<Inspector state={view()} sessionId="a" open tabRequest={{ tab: "context", request: 1 }} close={() => undefined} />);
    expect(screen.getByRole("tab", { name: "Context" }).getAttribute("aria-selected")).toBe("true");
  });
});

describe("Inspector request and content state", () => {
  it("keeps the selected patch request alive while changes refresh", async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.patch>>>();
    vi.spyOn(api, "patch").mockReturnValue(pending.promise);
    const state = view();
    const { rerender } = render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    fireEvent.click(await screen.findByRole("button", { name: /M\s*a.txt/ }));
    rerender(inspector({ ...state, session: { ...state.session, status: "running" } }));
    await act(async () => { pending.resolve(patch("selected patch")); });
    expect(await screen.findByText("selected patch")).toBeTruthy();
  });

  it("keeps task review separate from the working tree", async () => {
    vi.mocked(api.changes).mockResolvedValue({ changes: [], baseline: "HEAD", local_warning: false });
    vi.mocked(api.review).mockResolvedValue({ changes: [{ path: "a.txt", state: "task", origin: "task", can_revert: true }], revision: "session-r1" });
    render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    expect(await screen.findByText("No uncommitted changes.")).toBeTruthy();
    expect(api.review).not.toHaveBeenCalled();
    pickChangeScope("session");
    expect(await screen.findByRole("button", { name: /a.txt/ })).toBeTruthy();
    expect(screen.queryByText("No uncommitted changes.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept review" })).toBeNull();
  });

  it("keeps the Changes panel and its scope when switching away and back", async () => {
    vi.mocked(api.changes).mockResolvedValue({ changes: [], baseline: "HEAD", local_warning: false });
    render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    expect(await screen.findByText("No uncommitted changes.")).toBeTruthy();
    pickChangeScope("session");
    fireEvent.click(screen.getByRole("tab", { name: /Overview/ }));
    expect((document.getElementById("inspector-panel-changes") as HTMLElement).hidden).toBe(true);
    const fetches = vi.mocked(api.changes).mock.calls.length;
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    expect(screen.getByRole("radio", { name: "This session" }).getAttribute("aria-checked")).toBe("true");
    expect(vi.mocked(api.changes).mock.calls.length).toBe(fetches);
  });

  it("keeps the scope explanation in a tooltip and a screen-reader description", async () => {
    vi.mocked(api.changes).mockResolvedValue({ changes: [], baseline: "HEAD", local_warning: false });
    render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    const scope = await screen.findByLabelText("Change scope");
    const help = document.getElementById(scope.getAttribute("aria-describedby") ?? "");
    expect(help?.textContent).toMatch(/not committed/);
    expect(help?.classList.contains("sr-only")).toBe(true);
    expect(document.querySelector(".changes-scope-info")?.getAttribute("title")).toBe(help?.textContent);
  });

  it("switches tabs on press and draws one shared highlight", () => {
    render(inspector(view()));
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Context/ }), { button: 0 });
    expect(screen.getByRole("tab", { name: /Context/ }).getAttribute("aria-selected")).toBe("true");
    expect(document.querySelectorAll(".inspector-tab-indicator")).toHaveLength(1);
    fireEvent.mouseDown(screen.getByRole("tab", { name: /Overview/ }), { button: 2 });
    expect(screen.getByRole("tab", { name: /Context/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("only enables session undo when the server says it is safe", async () => {
    vi.mocked(api.review).mockResolvedValue({ changes: [{ path: "a.txt", state: "conflict", origin: "task", can_revert: false }], revision: "session-r1" });
    render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    pickChangeScope("session");
    const undo = await screen.findByRole("button", { name: /Undo \d+ available changes/ }) as HTMLButtonElement;
    expect(undo.disabled).toBe(true);
  });

  it("does not show a directory as empty while loading or after a failed request", async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.tree>>>();
    vi.mocked(api.tree).mockReturnValue(pending.promise);
    const { rerender } = render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    // The project tree loads as soon as the tab opens; no extra click.
    expect(screen.getByRole("status").textContent).toBe("Loading…");
    expect(screen.queryByText("This directory is empty.")).toBeNull();
    await act(async () => { pending.resolve({ path: "", entries: [] }); });
    expect(await screen.findByText("This directory is empty.")).toBeTruthy();
    vi.mocked(api.tree).mockRejectedValue(new Error("directory unavailable"));
    rerender(inspector(view("b")));
    expect((await screen.findByRole("alert")).textContent).toContain("directory unavailable");
    expect(screen.queryByText("This directory is empty.")).toBeNull();
  });

  it("normalizes absolute accessed paths for file reads and displays an empty file", async () => {
    const file = vi.spyOn(api, "file").mockResolvedValue({ path: "src/empty.ts", content: "", binary: false });
    const state = view(); state.accessed_files = [{ path: "/project/src/empty.ts", access: "read" }];
    render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    fireEvent.click(screen.getByRole("button", { name: /READ\s*src\/empty.ts/ }));
    expect(await screen.findByText("This file is empty.")).toBeTruthy();
    expect(file).toHaveBeenCalledWith("a", "src/empty.ts");
  });

  it("lists directories as LIST entries that do not open as files", () => {
    const file = vi.spyOn(api, "file");
    const state = view(); state.accessed_files = [{ path: "web/src", access: "list", tool: "ls" }];
    render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Files\s*1$/ }));
    const entry = screen.getByRole("button", { name: /LIST\s*web\/src/ }) as HTMLButtonElement;
    expect(entry.disabled).toBe(true);
    fireEvent.click(entry);
    expect(file).not.toHaveBeenCalled();
  });

  it("shows all accessed files once and counts changes as files", async () => {
    const state = view(); state.accessed_files = [
      ...Array.from({ length: 10 }, (_, index) => ({ path: `f${index}.ts`, access: "read" })),
      { path: "/project/f0.ts", access: "write" },
    ];
    render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Files\s*10$/ }));
    fireEvent.click(screen.getByRole("button", { name: "Show all (10)" }));
    expect(screen.getAllByRole("button").filter((button) => button.classList.contains("accessed-file"))).toHaveLength(10);
    expect(screen.getByRole("button", { name: /MOD\s*f0.ts/ })).toBeTruthy();
    expect(await screen.findByRole("tab", { name: /Changes\s*1$/ })).toBeTruthy();
  });

  it("does not write a review action result into a different session", async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.reviewAction>>>();
    vi.spyOn(api, "reviewAction").mockReturnValue(pending.promise);
    vi.mocked(api.review).mockResolvedValue({ changes: [{ path: "a.txt", state: "task", origin: "task", can_revert: true }], revision: "session-r1" });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { rerender } = render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    pickChangeScope("session");
    fireEvent.click(await screen.findByRole("button", { name: /Undo \d+ available changes/ }));
    await waitFor(() => expect(api.reviewAction).toHaveBeenCalled());
    rerender(inspector(view("b")));
    await act(async () => { pending.resolve({ applied: false, results: [], error: "old session failed" }); });
    expect(screen.queryByText("old session failed")).toBeNull();
  });
});

describe("Inspector historical sessions", () => {
  it("uses only the matching registered workspace for current file previews", async () => {
    vi.spyOn(api, "workspaces").mockResolvedValue({ selected_project_id: "other", projects: [
      { project_id: "other", name: "other", root: "/other" },
      { project_id: "project", name: "project", root: "/project", exists: true },
    ] });
    const tree = vi.spyOn(api, "workspaceTree").mockResolvedValue({ path: "", entries: [{ path: "a.txt", name: "a.txt", kind: "file", size: 10 }] });
    const file = vi.spyOn(api, "workspaceFile").mockResolvedValue({ path: "a.txt", content: "current workspace bytes", binary: false });
    const grants = vi.spyOn(api, "grants");
    render(inspector(view("saved", false)));
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    fireEvent.click(await screen.findByRole("treeitem", { name: /a.txt/ }));
    expect(await screen.findByText("current workspace bytes")).toBeTruthy();
    expect(tree).toHaveBeenCalledWith("project", ""); expect(file).toHaveBeenCalledWith("project", "a.txt");
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    expect(screen.getByText(/Resume this session to inspect its working tree/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept ready" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: /Permissions/ }));
    expect(screen.getByText("Permissions are available after this session is resumed.")).toBeTruthy();
    expect(screen.queryByRole("radio")).toBeNull();
    expect(api.changes).not.toHaveBeenCalled(); expect(api.review).not.toHaveBeenCalled(); expect(api.tree).not.toHaveBeenCalled(); expect(grants).not.toHaveBeenCalled();
  });

  it("does not substitute the project directory for an unmatched worktree", async () => {
    vi.spyOn(api, "workspaces").mockResolvedValue({ selected_project_id: "project", projects: [{ project_id: "project", name: "project", root: "/project" }] });
    const file = vi.spyOn(api, "workspaceFile");
    const state = view("saved", false); state.session.execution_root = "/worktrees/saved";
    render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    await waitFor(() => expect(api.workspaces).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Filter files in this project" }).hasAttribute("disabled")).toBe(true);
    expect(screen.queryByRole("tree")).toBeNull();
    expect(screen.getByText(/Resume this session to browse/)).toBeTruthy(); expect(file).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: /Context/ }));
    expect(screen.getByText("This saved session has no context usage statistics.")).toBeTruthy();
    expect(screen.queryByText(/unknown \/ unknown/)).toBeNull();
  });
});

describe("Inspector files, context and visibility", () => {
  it("filters project files by name and opens a match", async () => {
    vi.spyOn(api, "search").mockResolvedValue({ kind: "file", results: [{ path: "src/deep/app.ts", name: "app.ts", kind: "file" }] });
    vi.spyOn(api, "file").mockResolvedValue({ path: "src/deep/app.ts", content: "one\ntwo", binary: false });
    render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    fireEvent.click(screen.getByRole("button", { name: "Filter files in this project" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter files in this project" }), { target: { value: "app" } });
    fireEvent.click(await screen.findByRole("button", { name: /app.ts/ }));
    await screen.findByText("two");
    expect(api.search).toHaveBeenCalledWith("a", "app", "file");
    expect(document.querySelectorAll(".cv-row")).toHaveLength(2);
  });

  it("jumps from a changed file preview to its diff", async () => {
    vi.spyOn(api, "tree").mockResolvedValue({ path: "", entries: [{ path: "a.txt", name: "a.txt", kind: "file", size: 3 }] });
    vi.spyOn(api, "file").mockResolvedValue({ path: "a.txt", content: "new", binary: false });
    vi.spyOn(api, "patch").mockResolvedValue(patch("new"));
    render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    fireEvent.click(await screen.findByRole("treeitem", { name: /a.txt/ }));
    fireEvent.click(await screen.findByRole("button", { name: "View diff" }));
    expect(screen.getByRole("tab", { name: /Changes/ }).getAttribute("aria-selected")).toBe("true");
    expect(await screen.findByRole("region", { name: "Selected file" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /M a.txt/ }).getAttribute("aria-pressed")).toBe("true");
  });

  it("shows a preview estimate as a gauge with only the non-empty categories", () => {
    const state = view();
    state.context_breakdown = { total: 24000, limit: 128000, preview: true, categories: [
      { id: "system_prompt", tokens: 3000, share: 0.125 }, { id: "tool_schemas", tokens: 5000, share: 0.21 },
      { id: "output_reserve", tokens: 16000, share: 0.67 }, { id: "plan", tokens: 0, share: 0 },
    ] };
    render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Context/ }));
    expect(screen.getByRole("img", { name: "19% of the context window used" })).toBeTruthy();
    expect(screen.getByText("Estimate before the next request")).toBeTruthy();
    const labels = [...document.querySelectorAll(".ctx-legend-label")].map((item) => item.textContent);
    expect(labels).toEqual(["Reserved Output", "Tool Definitions", "System Prompt", "Free"]);
    expect(screen.getByText("Empty: Plan")).toBeTruthy();
  });

  it("warns near the context limit", () => {
    const state = view();
    state.context_breakdown = { total: 120000, limit: 128000, categories: [{ id: "history", tokens: 120000, share: 1 }] };
    render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Context/ }));
    expect(document.querySelector(".ctx-summary")?.classList.contains("danger")).toBe(true);
    expect(screen.getByText(/Close to the limit/)).toBeTruthy();
  });

  it("stays in the DOM when closed but out of reach, so it can animate", () => {
    render(<Inspector state={view()} sessionId="a" open={false} close={() => undefined} />);
    const panel = document.getElementById("workspace-inspector")!;
    expect(panel.classList.contains("is-closed")).toBe(true);
    expect(panel.hasAttribute("inert")).toBe(true);
    expect(screen.queryByRole("tab", { name: "Task" })).toBeNull();
  });
});
