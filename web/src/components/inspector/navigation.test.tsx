import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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
  expect(screen.getByRole("tab", { name: "Changes" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe("inspector-tab-changes");
  rerender(<Inspector {...props} focusRequest={2} />);
  expect(screen.getByRole("tab", { name: "Files" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByText("File preview")).toBeTruthy();
});

it("keeps keyboard focus inside an open drawer", () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ changes: [] }) })));
  render(<Inspector state={state} sessionId="s" open narrow close={() => undefined} />);
  const first = screen.getByRole("tab", { name: "Task" });
  const close = screen.getByRole("button", { name: "Close inspector" });
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(close);
  fireEvent.keyDown(close, { key: "Tab" });
  expect(document.activeElement).toBe(first);
});

it("keeps expanded diff focus inside its dialog and returns it on Escape", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).split("?")[0].endsWith("/changes/a.txt") ? { path: "a.txt", binary: false, truncated: false, revision: "r1", patch: "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n" } : { changes: String(input).endsWith("/changes?scope=unstaged") ? [{ path: "a.txt", status: "M" }] : [] } })));
  render(<Inspector state={state} sessionId="s" open close={() => undefined} />);
  fireEvent.click(screen.getByRole("tab", { name: /Changes/ }));
  fireEvent.click(await screen.findByRole("button", { name: /M\s*a.txt/ }));
  await screen.findByText("new");
  const expand = screen.getByRole("button", { name: "Expand diff" });
  fireEvent.click(expand);
  const dialog = screen.getByRole("dialog", { name: "a.txt" });
  const close = within(dialog).getByRole("button", { name: "Close" });
  const copy = within(dialog).getByRole("button", { name: /Copy/ });
  expect(document.activeElement).toBe(close);
  fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(copy);
  fireEvent.keyDown(copy, { key: "Tab" });
  expect(document.activeElement).toBe(close);
  const escape = vi.fn(); window.addEventListener("keydown", escape);
  fireEvent.keyDown(close, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(expand);
  expect(escape).not.toHaveBeenCalled();
  window.removeEventListener("keydown", escape);
});
