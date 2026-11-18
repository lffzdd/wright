import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Inspector } from "./Inspector";
import type { ViewState } from "../../types";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const state = { session: { session_id: "s", active: true, status: "idle" }, plan: {}, pending_interactions: [], usage: {}, connection: "connected" } as unknown as ViewState;

it("supports arrow-key tabs and returns to the same searched file on repeated requests", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).includes("/file?") ? { content: "File preview", binary: false } : { changes: [] } })));
  const props = { state, sessionId: "s", open: true, close: () => undefined, focusPath: "a.txt" };
  const { rerender } = render(<Inspector {...props} focusRequest={1} />);
  await screen.findByText("File preview");
  const files = screen.getByRole("tab", { name: "Files" });
  files.focus(); fireEvent.keyDown(files, { key: "ArrowRight" });
  expect(screen.getByRole("tab", { name: "Context" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe("inspector-tab-context");
  rerender(<Inspector {...props} focusRequest={2} />);
  expect(screen.getByRole("tab", { name: "Files" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByText("File preview")).toBeTruthy();
});

it("keeps keyboard focus inside an open drawer", () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ changes: [] }) })));
  render(<Inspector state={state} sessionId="s" open narrow close={() => undefined} />);
  const first = screen.getByRole("tab", { name: /Overview/ });
  // The Overview's last row is the drawer's last control.
  const last = [...document.querySelectorAll<HTMLButtonElement>(".overview-row")].pop()!;
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(last);
  fireEvent.keyDown(last, { key: "Tab" });
  expect(document.activeElement).toBe(first);
});

it("expands the diff under the file row without a dialog or patch card", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).split("?")[0].endsWith("/changes/a.txt") ? { path: "a.txt", binary: false, truncated: false, revision: "r1", patch: "diff --git a/a.txt b/a.txt\nindex 111..222 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n" } : { changes: String(input).endsWith("/changes?scope=unstaged") ? [{ path: "a.txt", status: "M" }] : [] } })));
  render(<Inspector state={state} sessionId="s" open close={() => undefined} />);
  fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
  const row = await screen.findByRole("button", { name: /M\s*a.txt/ });
  fireEvent.click(row);
  await screen.findByText("new");
  expect(screen.queryByRole("button", { name: "Expand diff" })).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.queryByRole("button", { name: /Copy/ })).toBeNull();
  expect(screen.queryByText(/diff --git/)).toBeNull();
  expect(row.closest(".diff-file")?.querySelector(".diff-table")).toBeTruthy();
});
