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

beforeEach(() => {
  setLocale("en");
  vi.spyOn(api, "changes").mockResolvedValue({ changes: [{ path: "a.txt", status: "M" }], baseline: "HEAD", local_warning: false });
  vi.spyOn(api, "review").mockResolvedValue({ changes: [] });
  vi.spyOn(api, "tree").mockResolvedValue({ path: "", entries: [] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); setLocale("en"); });

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

  it("does not discard the review response when a patch opens", async () => {
    const review = deferred<Awaited<ReturnType<typeof api.review>>>();
    vi.mocked(api.review).mockReturnValue(review.promise);
    vi.spyOn(api, "patch").mockResolvedValue(patch("new text"));
    render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    fireEvent.click(await screen.findByRole("button", { name: /M\s*a.txt/ }));
    await act(async () => { review.resolve({ changes: [{ path: "a.txt", state: "task" }] }); });
    expect(await screen.findByText("a.txt · task")).toBeTruthy();
  });

  it("does not show a directory as empty while loading or after a failed request", async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.tree>>>();
    vi.mocked(api.tree).mockReturnValue(pending.promise);
    const { rerender } = render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    fireEvent.click(screen.getByRole("button", { name: "Browse workspace" }));
    expect(screen.getByRole("status").textContent).toBe("Loading…");
    expect(screen.queryByText("This directory is empty.")).toBeNull();
    await act(async () => { pending.resolve({ path: "", entries: [] }); });
    expect(await screen.findByText("This directory is empty.")).toBeTruthy();
    vi.mocked(api.tree).mockRejectedValue(new Error("directory unavailable"));
    rerender(inspector(view("b")));
    fireEvent.click(screen.getByRole("button", { name: "Browse workspace" }));
    expect((await screen.findByRole("alert")).textContent).toContain("directory unavailable");
    expect(screen.queryByText("This directory is empty.")).toBeNull();
  });

  it("normalizes absolute accessed paths for file reads and displays an empty file", async () => {
    const file = vi.spyOn(api, "file").mockResolvedValue({ path: "src/empty.ts", content: "", binary: false });
    const state = view(); state.accessed_files = [{ path: "/project/src/empty.ts", access: "read" }];
    render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    fireEvent.click(screen.getByRole("button", { name: /READ\s*\/project\/src\/empty.ts/ }));
    expect(await screen.findByText("This file is empty.")).toBeTruthy();
    expect(file).toHaveBeenCalledWith("a", "src/empty.ts");
  });

  it("shows all accessed files once and counts changes as files", async () => {
    const state = view(); state.accessed_files = [
      ...Array.from({ length: 10 }, (_, index) => ({ path: `f${index}.ts`, access: "read" })),
      { path: "/project/f0.ts", access: "write" },
    ];
    render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Files\s*\(10\)/ }));
    expect(screen.getAllByRole("button").filter((button) => button.classList.contains("accessed-file"))).toHaveLength(10);
    expect(screen.getByRole("button", { name: /MOD\s*\/project\/f0.ts/ })).toBeTruthy();
    expect(await screen.findByRole("tab", { name: /Changes\s*\(1\)/ })).toBeTruthy();
  });

  it("does not write a review action result into a different session", async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.reviewAction>>>();
    vi.spyOn(api, "reviewAction").mockReturnValue(pending.promise);
    vi.mocked(api.review).mockResolvedValue({ changes: [{ path: "a.txt", state: "task" }] });
    const { rerender } = render(inspector(view()));
    fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Accept review" }));
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
    await waitFor(() => expect(screen.getByRole("button", { name: "Browse workspace" }).hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Browse workspace" }));
    fireEvent.click(await screen.findByRole("button", { name: /FILE\s*a.txt/ }));
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
    expect(screen.getByRole("button", { name: "Browse workspace" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/Resume this session to browse/)).toBeTruthy(); expect(file).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: /Context/ }));
    expect(screen.getByText("This saved session has no context usage statistics.")).toBeTruthy();
    expect(screen.queryByText(/unknown \/ unknown/)).toBeNull();
  });
});
