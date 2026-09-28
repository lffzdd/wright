import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Composer, Inspector, type Draft } from "./App";
import type { ViewState } from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
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
    prompt_tokens: null,
    completion_tokens: null,
    total_tokens: null,
    request_prompt_tokens: null,
    request_completion_tokens: null,
    request_total_tokens: null,
    context_tokens: null,
    context_limit: null,
  },
  seen: [],
  connection: "connected",
});

function jsonResponse(body: unknown, ok = true) {
  return { ok, statusText: ok ? "OK" : "error", json: async () => body };
}

describe("session isolation", () => {
  it("does not show a failed changes request as a clean worktree", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "diff failed" }, false)));
    render(<Inspector state={view("session-a")} sessionId="session-a" open close={() => undefined} />);
    expect((await screen.findByRole("alert")).textContent).toContain("diff failed");
    expect(screen.queryByText(/Working tree is clean/)).toBeNull();
  });

  it("drops a late changes response after switching sessions", async () => {
    let releaseA: (value: ReturnType<typeof jsonResponse>) => void = () => undefined;
    const pendingA = new Promise<ReturnType<typeof jsonResponse>>((resolve) => { releaseA = resolve; });
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (String(url).includes("session-a")) return pendingA;
      return Promise.resolve(jsonResponse({ local_warning: false, baseline: "head", changes: [{ path: "b.txt", status: "M" }] }));
    }));
    const { rerender } = render(<Inspector state={view("session-a")} sessionId="session-a" open close={() => undefined} />);
    rerender(<Inspector state={view("session-b")} sessionId="session-b" open close={() => undefined} />);
    expect(await screen.findByText("b.txt")).toBeDefined();
    releaseA(jsonResponse({ local_warning: false, baseline: "head", changes: [{ path: "a.txt", status: "M" }] }));
    await Promise.resolve();
    expect(screen.queryByText("a.txt")).toBeNull();
    expect(screen.getByText("b.txt")).toBeDefined();
  });

  it("keeps an upload on the session that started it", async () => {
    let release: (value: ReturnType<typeof jsonResponse>) => void = () => undefined;
    const pending = new Promise<ReturnType<typeof jsonResponse>>((resolve) => { release = resolve; });
    vi.stubGlobal("fetch", vi.fn(() => pending));
    function Harness({ sessionId }: { sessionId: string }) {
      const [drafts, setDrafts] = useState<Record<string, Draft>>({});
      const draft = drafts[sessionId] ?? { prompt: "", attachments: [], commandId: null, phase: "idle" as const, reason: "" };
      return <Composer
        sessionId={sessionId}
        connection="connected"
        draft={draft}
        running={false}
        updateDraft={(owner, patch) => setDrafts((current) => ({ ...current, [owner]: patch(current[owner] ?? draft) }))}
        submit={async () => "unknown"}
        cancel={async () => undefined}
      />;
    }
    const { rerender } = render(<Harness sessionId="session-a" />);
    const input = document.querySelector("input[type=file]") as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], "a.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [file] } });
    rerender(<Harness sessionId="session-b" />);
    release(jsonResponse({ id: "att-a", filename: "a.png", media_type: "image/png", size: 3, width: 1, height: 1 }));
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByText("a.png")).toBeNull();
    rerender(<Harness sessionId="session-a" />);
    expect(await screen.findByText("a.png")).toBeDefined();
  });

  it("keeps the files that uploaded when another file fails", async () => {
    let calls = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      calls += 1;
      if (calls === 1) {
        return jsonResponse({ id: "att-ok", filename: "ok.png", media_type: "image/png", size: 1, width: 1, height: 1 });
      }
      return jsonResponse({ detail: "bad image" }, false);
    }));
    function Harness() {
      const [drafts, setDrafts] = useState<Record<string, Draft>>({});
      const draft = drafts["session-a"] ?? { prompt: "", attachments: [], commandId: null, phase: "idle" as const, reason: "" };
      return <Composer
        sessionId="session-a"
        connection="connected"
        draft={draft}
        running={false}
        updateDraft={(owner, patch) => setDrafts((current) => ({ ...current, [owner]: patch(current[owner] ?? draft) }))}
        submit={async () => "unknown"}
        cancel={async () => undefined}
      />;
    }
    render(<Harness />);
    const input = document.querySelector("input[type=file]") as HTMLInputElement;
    const files = [
      new File([new Uint8Array([1])], "ok.png", { type: "image/png" }),
      new File([new Uint8Array([2])], "bad.png", { type: "image/png" }),
    ];
    fireEvent.change(input, { target: { files } });
    expect(await screen.findByText("ok.png")).toBeDefined();
    expect(await screen.findByText(/bad image/)).toBeDefined();
    expect(screen.queryByText("bad.png")).toBeNull();
  });
});
