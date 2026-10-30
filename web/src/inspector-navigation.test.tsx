import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, expect, it, vi } from "vitest";
import App from "./App";

beforeAll(() => { Element.prototype.scrollTo = vi.fn(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); });

function boot(compact: boolean, permission = false) {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: compact, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  const session = { session_id: "history", user_goal: "Review task", active: permission, status: permission ? "waiting_for_input" : "closed", execution_root: "/tmp/demo" };
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input); calls.push({ path, init });
    const body = path.includes("/preferences") ? { interface_language: "en", theme: "dark", inspector_open: init?.body ? JSON.parse(String(init.body)).inspector_open : true }
      : path.includes("/workspaces") ? { selected_project_id: "p", projects: [{ project_id: "p", root: "/tmp/demo", name: "demo" }] }
        : path === "/api/v1/project" ? { project_id: "p", name: "demo" }
          : path.includes("/preview") ? { session, history: [], timeline: [], usage: {} }
            : path.endsWith("/snapshot") ? { stream_id: "s", last_seq: 0, session, history: [], timeline: [], active_turn: null, plan: {}, usage: {}, pending_interactions: [{ request_id: "approval", kind: "permission", choices: [{ id: "deny", label: "Deny" }] }] }
            : path.startsWith("/api/v1/sessions?") ? [session] : { changes: [], rules: [], entries: [] };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  render(<App />);
  return calls;
}

it("opens a compact inspector with the first shortcut and closes its backdrop on Escape", async () => {
  boot(true);
  await screen.findByRole("heading", { name: "Review task" });
  expect(document.querySelector(".inspector.is-closed")).toBeTruthy();
  const opener = screen.getByRole("button", { name: /^Open inspector/ });
  opener.focus();
  fireEvent.keyDown(window, { key: "j", ctrlKey: true });
  expect(opener.getAttribute("aria-expanded")).toBe("true");
  expect(document.querySelector(".inspector-backdrop")).toBeTruthy();
  expect(document.querySelector(".inspector.narrow-open.is-closed")).toBeNull();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(document.querySelector(".inspector-backdrop")).toBeNull();
  expect(opener.getAttribute("aria-expanded")).toBe("false");
  expect(document.activeElement).toBe(opener);
});

it.each(["settings", "search"])("closes %s with Escape without denying the background permission", async (panel) => {
  const send = vi.fn();
  vi.stubGlobal("WebSocket", class { static OPEN = 1; readyState = 1; send = send; close() {} });
  boot(false, true);
  await screen.findByRole("heading", { name: "Review task" });
  if (panel === "settings") fireEvent.click(screen.getByRole("button", { name: "Settings" }));
  else fireEvent.keyDown(window, { key: "k", ctrlKey: true });
  const dialog = await screen.findByRole("dialog");
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(send).not.toHaveBeenCalled();
  fireEvent.keyDown(window, { key: "Escape" });
  expect(send).toHaveBeenCalledOnce();
  expect(JSON.parse(send.mock.calls[0][0])).toMatchObject({ type: "interaction.respond", request_id: "approval", answer: "deny" });
});

it("toggles the desktop inspector from the header and saves both directions", async () => {
  const calls = boot(false);
  await screen.findByRole("heading", { name: "Review task" });
  const toggle = document.querySelector<HTMLButtonElement>("button[aria-controls=workspace-inspector]")!;
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  await waitFor(() => expect(calls.filter((call) => call.init?.method === "PUT").map((call) => JSON.parse(String(call.init?.body)))).toEqual([{ inspector_open: false }, { inspector_open: true }]));
});

it("shows one compact drawer at a time without changing the desktop visibility preference", async () => {
  const calls = boot(true);
  await screen.findByRole("heading", { name: "Review task" });
  fireEvent.click(screen.getByRole("button", { name: /^Open inspector/ }));
  fireEvent.click(screen.getByRole("button", { name: "Open sessions" }));
  expect(document.querySelector(".inspector-backdrop")).toBeNull();
  expect(document.querySelector(".sidebar-backdrop")).toBeTruthy();
  fireEvent.keyDown(window, { key: "j", ctrlKey: true });
  expect(document.querySelector(".sidebar-backdrop")).toBeNull();
  fireEvent.keyDown(window, { key: "b", ctrlKey: true });
  expect(document.querySelector(".inspector-backdrop")).toBeNull();
  expect(document.querySelector(".sidebar-backdrop")).toBeTruthy();
  expect(calls.filter((call) => call.init?.method === "PUT").every((call) => JSON.parse(String(call.init?.body)).inspector_open === true)).toBe(true);
});
