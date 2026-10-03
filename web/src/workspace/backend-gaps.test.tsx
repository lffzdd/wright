import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { parseDiff } from "../diff";
import { setLocale } from "../i18n";
import { applyEvent } from "../reducer";
import type { EffectivePolicy, Grants, UiEvent, ViewState } from "../types";
import { InteractionCard, toolCardFromState, toolCardFromTimeline } from "./cards";
import { Inspector } from "./widgets";

const view = (): ViewState => ({
  stream_id: "stream", last_seq: 0, seen: [], connection: "connected",
  session: { session_id: "session", status: "running", execution: "running", active: true },
  history: [], active_turn: null, plan: {}, pending_interactions: [], notices: [],
  queued_commands: [], queue_depth: 0,
  usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null,
    request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
    context_tokens: null, context_limit: null },
});
const inspector = (state: ViewState) => <Inspector state={state} sessionId="session" open close={() => undefined} />;

beforeEach(() => {
  setLocale("en");
  vi.spyOn(api, "changes").mockResolvedValue({ changes: [], baseline: "HEAD", local_warning: false });
  vi.spyOn(api, "review").mockResolvedValue({ changes: [] });
});
afterEach(() => {
  cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); setLocale("en");
});

describe("backend data in workspace panels", () => {
  it("renders the same structured diff from live and snapshot tool envelopes", () => {
    const data = { file: "file.txt", diff: "--- a/file.txt\n+++ b/file.txt\n@@ -5,2 +7,3 @@\n context\n-old\n+new\n+\n\\ No newline at end of file\n", additions: 2, deletions: 1, diff_truncated: true };
    const { container, rerender } = render(toolCardFromState({ call_id: "c", name: "edit_file", phase: "succeeded", data, arguments: { file: "file.txt" } }));
    expect(container.querySelector(".diff-del .diff-line-old")?.textContent).toBe("6");
    expect(container.querySelector(".diff-add .diff-line-new")?.textContent).toBe("8");
    expect(container.querySelectorAll(".diff-add")).toHaveLength(2);
    expect(screen.getByRole("status").textContent).toContain("64 KiB");
    const liveText = container.textContent;
    rerender(toolCardFromTimeline({ id: "c", kind: "edit", name: "edit_file", phase: "succeeded", arguments: { file: "file.txt" }, result: { ok: true, data } }));
    expect(container.textContent).toBe(liveText);
  });

  it("does not mistake changed text beginning with --- or +++ for file headers", () => {
    const parsed = parseDiff("--- a/f\n+++ b/f\n@@ -1 +1,2 @@\n---old\n+++new\n+\n\\ No newline at end of file\n");
    expect([parsed.additions, parsed.deletions]).toEqual([2, 1]);
    expect(parsed.lines.filter((line) => line.type === "add").map((line) => line.newNum)).toEqual([1, 2]);
    expect(parsed.lines.at(-1)?.type).toBe("meta");
    expect(parsed.lines).toHaveLength(7);
  });

  it("renders explicit zero-change diffs and approval audit text", () => {
    render(toolCardFromState({ call_id: "c", name: "edit_file", phase: "succeeded", data: { diff: "", additions: 0, deletions: 0, diff_truncated: false } }));
    expect(screen.getByText("No textual diff.")).toBeTruthy();
    render(<InteractionCard sessionId="s" interaction={{ request_id: "permission", kind: "permission", choices: [{ id: "deny", label: "Deny", scope: "invocation", persistence: "none" }] }} respond={async () => true} />);
    expect(screen.getByText(/Permission requests and decisions are recorded/)).toBeTruthy();
  });

  it("updates elapsed time while live and freezes a history preview", async () => {
    vi.useFakeTimers(); vi.setSystemTime(105_000);
    const state = view();
    state.plan = { observed_at: 105, steps: [{ id: "first", title: "Edit file", status: "in_progress", started_at: 100, ended_at: null, elapsed_ms: 5000, wait_reason: { kind: "permission", request_ids: ["p"] } }] };
    const { rerender } = render(inspector(state));
    expect(screen.getByText("Elapsed 5.0s (including waits)")).toBeTruthy();
    expect(screen.getByText("Awaiting permission approval")).toBeTruthy();
    await act(async () => { vi.advanceTimersByTime(2000); });
    expect(screen.getByText("Elapsed 7.0s (including waits)")).toBeTruthy();
    const history = { ...state, session: { ...state.session, active: false, status: "closed", execution: "idle" as const } };
    rerender(inspector(history));
    expect(screen.getByText("Elapsed 5.0s (including waits)")).toBeTruthy();
    await act(async () => { vi.advanceTimersByTime(10_000); });
    expect(screen.getByText("Elapsed 5.0s (including waits)")).toBeTruthy();
  });

  it("shows completed time and no elapsed value for an unstarted step", () => {
    const state = view();
    state.plan = { steps: [
      { id: "done", title: "Done", status: "completed", started_at: 100, ended_at: 102, elapsed_ms: 2000 },
      { id: "pending", title: "Next", status: "pending", started_at: null, ended_at: null },
    ] };
    const { container } = render(inspector(state));
    expect(screen.getByText("Elapsed 2.0s (including waits)")).toBeTruthy();
    expect(screen.getByText(/^Ended /)).toBeTruthy();
    expect(container.querySelector(".step-card.pending")?.textContent).not.toContain("Elapsed");
  });

  it("clears wait details on authoritative updates without changing step state", () => {
    let state = view();
    const event = (seq: number, plan: ViewState["plan"]): UiEvent => ({ version: 2, stream_id: "stream", event_id: `e${seq}`, seq, emitted_at: "now", project_id: "p", session_id: "session", type: "plan.updated", payload: { plan } });
    const step = { id: "first", title: "Edit file", status: "in_progress", started_at: 100, ended_at: null };
    state = applyEvent(state, event(1, { steps: [{ ...step, wait_reason: { kind: "permission", request_ids: ["p"] } }] }));
    const { rerender } = render(inspector(state));
    expect(screen.getByText("Awaiting permission approval")).toBeTruthy();
    state = applyEvent(state, event(2, { steps: [step] }));
    rerender(inspector(state));
    expect(screen.queryByText("Awaiting permission approval")).toBeNull();
    expect(state.plan.steps?.[0].status).toBe("in_progress");
  });

  it("shows policy defaults, conditional overrides and refreshes on policy and reconnect changes", async () => {
    const base = (decision: "allow" | "ask" | "deny") => ({ decision, source: "default", reason_code: "", reason_params: {} });
    const policy: EffectivePolicy = { operation: "file_write", defaults: { in_scope: base("ask"), outside_scope: base("ask") }, directories: ["/project"], read_only: ["/external-ro"], rules: [{ effect: "deny", scope: "project", rule: { tool_name: "edit_file", pattern: "secrets/*" }, target: "/project/secrets/*", resource_kind: "file", operations: ["file_write"], tool: "edit_file", description: "edit_file secrets/*", conditional: true }], constraints: ["protected_permission_files"], precedence: [] };
    const grants: Grants = { version: "test", grants: [], permission_mode: "default", effective_policy: [policy] };
    const request = vi.spyOn(api, "grants").mockResolvedValue(grants);
    const state = view();
    const { rerender } = render(inspector(state));
    fireEvent.click(screen.getByRole("tab", { name: /Permissions/ }));
    expect(await screen.findByText("File Write")).toBeTruthy();
    expect(screen.getByText(/Conditional rule/)).toBeTruthy();
    expect(screen.getByText(/Conditional rule/).textContent).toContain("This project long term");
    expect(screen.getByText(/Read-only directory: \/external-ro/)).toBeTruthy();
    expect(screen.getByText("Only through edit_file")).toBeTruthy();
    expect(screen.getByText("Protected permission files remain blocked.")).toBeTruthy();
    expect(screen.queryByText(/unrestricted/i)).toBeNull();
    request.mockResolvedValue({ ...grants, permission_mode: "bypass", effective_policy: [{ ...policy, defaults: { in_scope: base("allow"), outside_scope: base("allow") } }] });
    const bypass = { ...state, session: { ...state.session, permission_mode: "bypass" } };
    rerender(inspector(bypass));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Fewer confirmations")).toBeTruthy();
    rerender(inspector({ ...bypass, connection: "reconnecting" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
    rerender(inspector({ ...bypass, connection: "connected" }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(4));
    rerender(inspector({ ...bypass, pending_interactions: [{ request_id: "p", kind: "permission" }] }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(5));
    const committed = applyEvent(bypass, { version: 2, stream_id: "stream", event_id: "policy", seq: 1, emitted_at: "now", project_id: "p", session_id: "session", type: "session.policy_updated", payload: { interaction_mode: "plan", permission_mode: "bypass" } });
    rerender(inspector(committed));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(6));
    expect(committed.session.interaction_mode).toBe("plan");
    expect(committed.permissions_revision).toBe(1);
  });
});
