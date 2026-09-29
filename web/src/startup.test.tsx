import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import App from "./App";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.location.hash = "";
  localStorage.clear();
});

beforeAll(() => { Element.prototype.scrollTo = vi.fn(); });

describe("web startup", () => {
  it("calls APIs only after the bootstrap exchange resolves", async () => {
    window.location.hash = "#bootstrap=secret";
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      calls.push(`${init?.method ?? "GET"} ${path}`);
      if (path.includes("/auth/exchange")) {
        await gate;
        return jsonResponse({ ok: true });
      }
      if (path.includes("/preferences")) {
        return jsonResponse({
          interface_language: "en",
          supported: [],
          note: "",
          theme: "dark",
          inspector_open: true,
        });
      }
      if (path.includes("/workspaces")) return jsonResponse({ selected_project_id: "p", projects: [] });
      if (path.includes("/project")) return jsonResponse({ project_id: "p", name: "demo", git: true, project_root: "/tmp/demo" });
      if (path.includes("/sessions")) return jsonResponse([]);
      return jsonResponse({});
    }));

    render(<App />);
    await waitFor(() => expect(calls.some((call) => call.includes("/auth/exchange"))).toBe(true));
    expect(calls.filter((call) => !call.includes("/auth/exchange"))).toEqual([]);

    release();
    await waitFor(() => expect(screen.getByText("demo")).toBeTruthy());
    expect(screen.getByText("New conversation")).toBeTruthy();
    expect(document.querySelector(".welcome-pane .environment-choice")).toBeTruthy();
    expect(document.querySelector(".composer .environment-choice, .composer .environment-compact")).toBeNull();
    expect(screen.queryByText(/Offline/)).toBeNull();
    expect(screen.queryByRole("tab", { name: "Task" })).toBeNull();
    const exchangeAt = calls.findIndex((call) => call.includes("/auth/exchange"));
    const preferencesAt = calls.findIndex((call) => call.includes("/api/v1/preferences"));
    const projectAt = calls.findIndex((call) => call.includes("/api/v1/project"));
    const sessionsAt = calls.findIndex((call) => call.includes("/api/v1/sessions"));
    expect(exchangeAt).toBeGreaterThanOrEqual(0);
    expect(preferencesAt).toBeGreaterThan(exchangeAt);
    expect(projectAt).toBeGreaterThan(exchangeAt);
    expect(sessionsAt).toBeGreaterThan(exchangeAt);
  });

  it("does not poll sessions when startup is unauthorized", async () => {
    const intervals: number[] = [];
    vi.spyOn(window, "setInterval").mockImplementation(((handler: TimerHandler, timeout?: number) => {
      intervals.push(timeout ?? 0);
      return 0 as unknown as ReturnType<typeof window.setInterval>;
    }) as typeof window.setInterval);
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "authentication required" }, 401)));

    render(<App />);
    await waitFor(() => expect(screen.getByText("Wright Web could not start")).toBeTruthy());
    expect(intervals).not.toContain(5_000);
  });

  it("restores the latest session instead of a blank draft", async () => {
    window.location.hash = "#bootstrap=secret";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/auth/exchange")) return jsonResponse({ ok: true });
      if (path.includes("/preferences")) {
        return jsonResponse({
          interface_language: "en",
          supported: [],
          note: "",
          theme: "dark",
          inspector_open: true,
        });
      }
      if (path.includes("/workspaces")) return jsonResponse({
        selected_project_id: "p",
        projects: [{ project_id: "p", name: "demo", selected: true, root: "/tmp/demo" }],
      });
      if (path.includes("/project")) return jsonResponse({ project_id: "p", name: "demo", git: true, project_root: "/tmp/demo" });
      if (path.includes("/preview")) {
        return jsonResponse({
          session: { session_id: "abc123", user_goal: "Fix Redis leak", active: false, status: "closed", recoverable: true },
          history: [],
          timeline: [{ id: "u1", kind: "text", role: "user", text: "Fix Redis leak" }],
        });
      }
      if (path.includes("/sessions")) return jsonResponse([
        { session_id: "abc123", user_goal: "Fix Redis leak", active: false, status: "closed", saved_at: "2026-09-29 12:00:00" },
      ]);
      return jsonResponse({});
    }));

    render(<App />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Fix Redis leak" })).toBeTruthy());
    expect(screen.getByLabelText("Message Wright").hasAttribute("disabled")).toBe(false);
    expect(screen.queryByText(/Offline/)).toBeNull();
    expect(screen.getByRole("button", { name: "Rename" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Archive" }).length).toBeGreaterThan(0);
  });
});
