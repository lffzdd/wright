import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api";
import { setLocale } from "../../i18n";
import type { ChangePatch, ChangesSummary, ViewState } from "../../types";
import { ChangesPanel } from "./ChangesPanel";

const view = (sessionId = "a"): ViewState => ({
  stream_id: "s", last_seq: 0, seen: [], connection: "connected",
  session: { session_id: sessionId, status: "idle", active: true, execution_root: "/project" },
  history: [], active_turn: null, plan: {}, pending_interactions: [], notices: [], queued_commands: [], queue_depth: 0,
  usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null, request_prompt_tokens: null,
    request_completion_tokens: null, request_total_tokens: null, context_tokens: null, context_limit: null },
});
const summary = (revision = "r1"): ChangesSummary => ({ git: true, revision, baseline: "HEAD", local_warning: false, changes: [{ path: "src/a.txt", status: "M", unstaged: true, additions: 2, deletions: 2, capabilities: { stage: true, unstage: false, discard: true, hunk: true } }] });
const patch = (revision = "r1"): ChangePatch => ({ path: "src/a.txt", revision, binary: false, truncated: false, patch: "--- a/src/a.txt\n+++ b/src/a.txt\n@@ -1 +1 @@\n-old\n+new\n@@ -20 +20 @@\n-later old\n+later new\n" });
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};
const panel = (state = view(), feedback?: (text: string) => void) => <ChangesPanel sessionId={state.session.session_id} state={state} onCount={() => undefined} onFeedback={feedback} />;
const scope = (value: string) => fireEvent.change(screen.getByLabelText("Change scope"), { target: { value } });
const openFile = async () => fireEvent.click(await screen.findByRole("button", { name: /M\s*src\/a.txt/ }));

beforeEach(() => {
  setLocale("en");
  vi.spyOn(api, "changes").mockResolvedValue(summary());
  vi.spyOn(api, "patch").mockResolvedValue(patch());
  vi.spyOn(api, "review").mockResolvedValue({ changes: [], revision: "session-r1" });
  vi.spyOn(api, "reviewPatch").mockResolvedValue(patch("session-r1"));
  vi.spyOn(api, "changeAction").mockResolvedValue({ applied: true });
  vi.spyOn(api, "reviewAction").mockResolvedValue({ applied: true });
  vi.spyOn(window, "confirm").mockReturnValue(true);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("Changes user workflow", () => {
  it("shows only the selected scope and keeps old baseline files out of session edits", async () => {
    vi.mocked(api.changes).mockResolvedValue({ ...summary(), changes: [] });
    vi.mocked(api.review).mockResolvedValue({ revision: "session-r1", changes: [
      { path: "existing.txt", state: "preexisting", origin: "preexisting" },
      { path: "outside.txt", state: "external", origin: "external" },
      { path: "agent.txt", state: "conflict", origin: "task", can_revert: false, reason: "Edited elsewhere" },
    ] });
    render(panel());
    expect(await screen.findByText("No uncommitted changes.")).toBeTruthy();
    expect(api.review).not.toHaveBeenCalled();
    scope("session");
    expect(await screen.findByRole("button", { name: /agent.txt/ })).toBeTruthy();
    expect(screen.queryByText("existing.txt")).toBeNull();
    expect(screen.queryByText("outside.txt")).toBeNull();
    expect(screen.queryByText("No uncommitted changes.")).toBeNull();
    expect((screen.getByRole("button", { name: /Undo \d+ available changes/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows failed requests as failures instead of clean workspaces", async () => {
    vi.mocked(api.changes).mockRejectedValue(new Error("Cannot read this worktree"));
    render(panel());
    expect((await screen.findByRole("alert")).textContent).toBe("Cannot read this worktree");
    expect(screen.queryByText("No uncommitted changes.")).toBeNull();
  });

  it("keeps a late diff from a previous scope out of the selected scope", async () => {
    const pending = deferred<ChangePatch>();
    vi.mocked(api.patch).mockReturnValueOnce(pending.promise);
    render(panel());
    await openFile();
    scope("staged");
    await screen.findByText("Commit message");
    await act(async () => { pending.resolve(patch()); });
    expect(screen.queryByText("later new")).toBeNull();
    expect(api.patch).toHaveBeenCalledWith("a", "src/a.txt", "working");
  });

  it("stages an exact server hunk with the displayed revision and scope", async () => {
    render(panel());
    scope("unstaged");
    await openFile();
    const hunks = await screen.findAllByRole("button", { name: "Stage hunk" });
    fireEvent.click(hunks[1]);
    await waitFor(() => expect(api.changeAction).toHaveBeenCalledWith("a", expect.objectContaining({ action: "stage", scope: "unstaged", paths: ["src/a.txt"], expected_revision: "r1", hunk: 1 })));
    expect(api.reviewAction).not.toHaveBeenCalled();
  });

  it("requires a discard confirmation that identifies the disk and index impact", async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    render(panel());
    await openFile();
    await screen.findByText("later new");
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("staging state will return to HEAD"));
    expect(api.changeAction).not.toHaveBeenCalled();
  });

  it("disables file actions when the selected diff has a newer revision", async () => {
    vi.mocked(api.patch).mockResolvedValue(patch("r2"));
    render(panel());
    await openFile();
    expect(await screen.findByText("The diff changed after the list loaded. Refresh before acting.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Stage" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("undoes a rename pair from a session revision without sending a Git discard", async () => {
    vi.mocked(api.review).mockResolvedValue({ revision: "session-r1", changes: [
      { path: "src/a.txt", state: "task", origin: "task", kind: "add", display_kind: "rename", rename_with: "src/old.txt", can_revert: true },
      { path: "src/old.txt", state: "task", origin: "task", kind: "delete", display_kind: "rename", rename_with: "src/a.txt", can_revert: true },
    ] });
    render(panel());
    scope("session");
    fireEvent.click(await screen.findByRole("button", { name: /Undo \d+ available changes/ }));
    await waitFor(() => expect(api.reviewAction).toHaveBeenCalledWith("a", "revert", ["src/a.txt", "src/old.txt"], "session-r1"));
    expect(api.changeAction).not.toHaveBeenCalled();
  });

  it("reports binary data explicitly without pretending it has no diff", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...patch(), patch: "", binary: true });
    render(panel());
    await openFile();
    expect(await screen.findByText("Binary change. A text diff is unavailable.")).toBeTruthy();
    expect(screen.queryByText("No diff available.")).toBeNull();
  });

  it("adds a line-specific feedback draft without applying or submitting anything", async () => {
    const feedback = vi.fn();
    render(panel(view(), feedback));
    await openFile();
    await screen.findByText("later new");
    fireEvent.click(screen.getByRole("button", { name: "Expand diff" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Comment on New line 20" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Give feedback" }), { target: { value: "Keep the original wording here" } });
    fireEvent.click(screen.getByRole("button", { name: "Add to message" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(feedback).toHaveBeenCalledWith(expect.stringContaining("src/a.txt:20"));
    expect(feedback).toHaveBeenCalledWith(expect.stringContaining("Keep the original wording here"));
    expect(api.changeAction).not.toHaveBeenCalled();
    expect(api.reviewAction).not.toHaveBeenCalled();
  });

  it("commits the displayed staged snapshot with a message and does not stage other files", async () => {
    render(panel());
    scope("staged");
    await screen.findByText("Commit message");
    await screen.findByRole("button", { name: /src\/a.txt/ });
    const commit = screen.getByRole("button", { name: "Commit staged changes" }) as HTMLButtonElement;
    expect(commit.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Commit message"), { target: { value: "Fix parser" } });
    fireEvent.click(commit);
    await waitFor(() => expect(api.changeAction).toHaveBeenCalledWith("a", expect.objectContaining({ action: "commit", scope: "staged", message: "Fix parser", expected_revision: "r1", confirm: true })));
    expect(vi.mocked(api.changeAction).mock.calls.every((call) => call[1].action === "commit")).toBe(true);
  });

  it("does not let a previous session's action report overwrite the new session", async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.changeAction>>>();
    vi.mocked(api.changeAction).mockReturnValue(pending.promise);
    const { rerender } = render(panel());
    fireEvent.click(await screen.findByRole("button", { name: "Stage all" }));
    rerender(panel(view("b")));
    await act(async () => { pending.resolve({ applied: false, error: "old session failure" }); });
    expect(screen.queryByText("old session failure")).toBeNull();
  });

  it("blocks actions while the Agent is writing files", async () => {
    const state = view(); state.session.execution = "running";
    render(panel(state));
    const stage = await screen.findByRole("button", { name: "Stage all" }) as HTMLButtonElement;
    expect(stage.disabled).toBe(true);
    expect(screen.getByText("File actions are available when the session is idle and connected.")).toBeTruthy();
  });

  it("does not silently skip unsupported files in a bulk discard", async () => {
    const result = summary();
    result.changes.push({ path: "linked.txt", status: "M", unstaged: true, capabilities: { stage: false, discard: false, hunk: false }, reason: "Linked file cannot be changed here" });
    vi.mocked(api.changes).mockResolvedValue(result);
    render(panel());
    expect((await screen.findByRole("button", { name: "Discard all" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Some files do not support this operation. Inspect them individually; the bulk action is disabled.")).toBeTruthy();
    expect(api.changeAction).not.toHaveBeenCalled();
  });

  it("requires the server's hunk capability before offering partial mutations", async () => {
    const result = summary(); result.changes[0].capabilities = undefined;
    vi.mocked(api.changes).mockResolvedValue(result);
    render(panel()); scope("unstaged");
    await openFile(); await screen.findByText("later new");
    expect(screen.queryByRole("button", { name: "Stage hunk" })).toBeNull();
  });

  it("does not display missing snapshots as an empty diff", async () => {
    vi.mocked(api.patch).mockResolvedValue({ ...patch(), patch: "", unavailable: true, reason: "Stored snapshot is missing" });
    render(panel()); await openFile();
    expect(await screen.findByText("Stored snapshot is missing")).toBeTruthy();
    expect(screen.getByText("Unavailable")).toBeTruthy();
    expect(screen.queryByText("No diff available.")).toBeNull();
  });
});
