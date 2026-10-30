import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, expect, it, vi } from "vitest";
import App from "./App";
import type { SessionSummary } from "./types";

beforeAll(() => { Element.prototype.scrollTo = vi.fn(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); });
const deferred = () => { let resolve!: () => void; const promise = new Promise<void>((done) => { resolve = done; }); return { promise, resolve }; };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function setup(options: { resumeGate?: Promise<void>; failFirstSubmit?: boolean } = {}) {
  vi.stubGlobal("WebSocket", class { static OPEN = 1; readyState = 1; send() {} close() {} });
  const sessions: SessionSummary[] = [
    { session_id: "a", user_goal: "Alpha conversation", project_id: "one", project_root: "/one", execution_root: "/one", active: false, status: "closed" },
    { session_id: "b", user_goal: "Beta conversation", project_id: "two", project_root: "/two", execution_root: "/two", active: false, status: "closed" },
    { session_id: "c", user_goal: "Gamma conversation", project_id: "one", project_root: "/one", execution_root: "/one", active: false, status: "closed" },
  ];
  const calls: Array<{ path: string; method: string; body: Record<string, unknown> }> = [];
  let submissions = 0;
  const snapshot = (session: SessionSummary) => ({ stream_id: "s", last_seq: 0, session, history: [], timeline: [{ id: `prior-${session.session_id}`, kind: "text", role: "assistant", text: `Prior context for ${session.session_id}` }], active_turn: null, plan: {}, pending_interactions: [], usage: {} });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input), method = init?.method ?? "GET", body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ path, method, body });
    if (path.includes("/preferences")) return json({ interface_language: "en", theme: "dark", inspector_open: false });
    if (path === "/api/v1/workspaces") return json({ selected_project_id: "one", projects: [{ project_id: "one", name: "One", root: "/one" }, { project_id: "two", name: "Two", root: "/two" }] });
    if (path === "/api/v1/project") return json({ project_id: "one", name: "One", project_root: "/one", git: true });
    if (path === "/api/v1/sessions?all_projects=true") return json(sessions);
    if (path.includes("/preview") || path.endsWith("/snapshot")) return json(snapshot(sessions.find((session) => path.includes(`/sessions/${session.session_id}/`))!));
    if (method === "POST" && /^\/api\/v1\/workspaces\/[^/]+\/sessions$/.test(path)) {
      if (options.resumeGate) await options.resumeGate;
      const session = sessions.find((item) => item.session_id === body.resume)!;
      session.active = true; session.status = "idle";
      return json(snapshot(session));
    }
    if (path.endsWith("/turns")) {
      ++submissions;
      if (options.failFirstSubmit && submissions === 1) return json({ detail: "Temporary transport failure" }, 503);
      return json({ accepted: true });
    }
    return json({ changes: [], rules: [], entries: [], ok: true });
  }));
  render(<App />);
  return { calls, sessions };
}

it("groups history under its project, isolates session drafts, and resumes the original owner only when sending", async () => {
  const gate = deferred();
  const { calls } = setup({ resumeGate: gate.promise });
  await screen.findByRole("heading", { name: "Alpha conversation" });
  const one = screen.getByRole("region", { name: "One" }), two = screen.getByRole("region", { name: "Two" });
  expect(within(one).queryByRole("button", { name: "Beta conversation" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Beta conversation" }));
  await screen.findByRole("heading", { name: "Beta conversation" });
  expect(document.querySelector(".crumb")?.textContent).toBe("Two");
  expect(calls.some((call) => call.path === "/api/v1/sessions/b/preview?project_id=two")).toBe(true);
  expect(calls.filter((call) => call.body.resume)).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("Message Wright"), { target: { value: "Draft for Beta" } });
  fireEvent.click(screen.getByRole("button", { name: "Alpha conversation" }));
  await screen.findByRole("heading", { name: "Alpha conversation" });
  expect((screen.getByLabelText("Message Wright") as HTMLTextAreaElement).value).toBe("");
  fireEvent.change(screen.getByLabelText("Message Wright"), { target: { value: "Draft for Alpha" } });
  fireEvent.click(screen.getByRole("button", { name: "Beta conversation" }));
  await screen.findByRole("heading", { name: "Beta conversation" });
  expect((screen.getByLabelText("Message Wright") as HTMLTextAreaElement).value).toBe("Draft for Beta");
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByText("Restoring this conversation…")).toBeTruthy();
  await act(async () => { gate.resolve(); });
  await waitFor(() => expect(calls.some((call) => call.path === "/api/v1/sessions/b/turns")).toBe(true));
  expect(calls.find((call) => call.body.resume)?.path).toBe("/api/v1/workspaces/two/sessions");
  expect(calls.find((call) => call.body.resume)?.body.resume).toBe("b");
  expect(await screen.findByText("Prior context for b")).toBeTruthy();
});

it("does not let a slow historical send take back the view after selecting another conversation", async () => {
  const gate = deferred(); const { calls } = setup({ resumeGate: gate.promise });
  await screen.findByRole("heading", { name: "Alpha conversation" });
  fireEvent.change(screen.getByLabelText("Message Wright"), { target: { value: "Continue Alpha" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  fireEvent.click(screen.getByRole("button", { name: "Gamma conversation" }));
  await screen.findByRole("heading", { name: "Gamma conversation" });
  await act(async () => { gate.resolve(); });
  await waitFor(() => expect(calls.some((call) => call.path === "/api/v1/sessions/a/turns")).toBe(true));
  expect(screen.getByRole("heading", { name: "Gamma conversation" })).toBeTruthy();
  expect((screen.getByLabelText("Message Wright") as HTMLTextAreaElement).value).toBe("");
});

it("reopens the same historical session and retries an uncertain submit with the same command id", async () => {
  const { calls } = setup({ failFirstSubmit: true });
  await screen.findByRole("heading", { name: "Alpha conversation" });
  fireEvent.change(screen.getByLabelText("Message Wright"), { target: { value: "Continue after interruption" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByText("Temporary transport failure")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(calls.filter((call) => call.path.endsWith("/turns"))).toHaveLength(2));
  const opens = calls.filter((call) => call.body.resume), submits = calls.filter((call) => call.path.endsWith("/turns"));
  expect(opens.map((call) => call.body.resume)).toEqual(["a", "a"]);
  expect(submits[0].body.command_id).toBe(submits[1].body.command_id);
  expect(opens[0].body.client_request_id).toBe(opens[1].body.client_request_id);
});

it("keeps a new conversation draft with its project and lets project history collapse", async () => {
  setup(); await screen.findByRole("heading", { name: "Alpha conversation" });
  const one = screen.getByRole("region", { name: "One" }), two = screen.getByRole("region", { name: "Two" });
  fireEvent.click(within(one).getByRole("button", { name: "One" }));
  fireEvent.change(screen.getByLabelText("Message Wright"), { target: { value: "New One draft" } });
  fireEvent.click(within(two).getByRole("button", { name: "Two" }));
  expect((screen.getByLabelText("Message Wright") as HTMLTextAreaElement).value).toBe("");
  expect(screen.getByText("/two")).toBeTruthy();
  fireEvent.click(within(one).getByRole("button", { name: "One" }));
  expect((screen.getByLabelText("Message Wright") as HTMLTextAreaElement).value).toBe("New One draft");
  const toggle = within(two).getByRole("button", { name: "Toggle conversations for Two" });
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
});
