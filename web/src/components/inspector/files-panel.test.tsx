import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api";
import { setLocale } from "../../i18n";
import type { ViewState } from "../../types";
import { Inspector } from "./Inspector";

// Each test gets its own session id: expanded folders are remembered per session.
let sid = "a";
const view = (): ViewState => ({
  stream_id: "s", last_seq: 0, seen: [], connection: "connected",
  session: { session_id: sid, status: "idle", active: true, execution_root: "/project" },
  history: [], active_turn: null, plan: {}, pending_interactions: [], notices: [], queued_commands: [], queue_depth: 0,
  usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null, request_prompt_tokens: null,
    request_completion_tokens: null, request_total_tokens: null, context_tokens: null, context_limit: null },
});
const inspector = (state: ViewState) => <Inspector state={state} sessionId={state.session.session_id} open close={() => undefined} />;
const entry = (name: string, kind = "file", path = name) => ({ name, path, kind, size: kind === "file" ? 3 : null });

beforeEach(() => {
  sid = `s-${Math.random()}`;
  setLocale("en");
  localStorage.clear();
  vi.spyOn(api, "changes").mockResolvedValue({ changes: [{ path: "src/a.ts", status: "M" }], baseline: "HEAD", local_warning: false });
  vi.spyOn(api, "review").mockResolvedValue({ changes: [] });
  vi.spyOn(api, "patch").mockResolvedValue({ path: "src/a.ts", patch: "", truncated: false, binary: false } as never);
  vi.spyOn(api, "tree").mockImplementation(async (_id, path = "") => {
    if (path === "") return { path: "", entries: [entry("src", "dir", "src"), entry(".mypy_cache", "dir", ".mypy_cache"), entry("README.md")] };
    if (path === "src") return { path: "src", entries: [entry("a.ts", "file", "src/a.ts"), entry("lib", "dir", "src/lib")] };
    if (path === "src/lib") return { path: "src/lib", entries: [entry("deep.ts", "file", "src/lib/deep.ts")] };
    return { path, entries: [] };
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); setLocale("en"); });

const openFiles = async (state = view()) => {
  render(inspector(state));
  fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
  await screen.findByRole("treeitem", { name: /README.md/ });
};

describe("Files panel", () => {
  it("hides cache folders until the user asks to show them", async () => {
    await openFiles();
    expect(screen.queryByRole("treeitem", { name: /\.mypy_cache/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show cache folders" }));
    expect(await screen.findByRole("treeitem", { name: /\.mypy_cache/ })).toBeTruthy();
    expect(localStorage.getItem("wright.files.showCache")).toBe("1");
  });

  it("shows git status letters on changed files in the tree", async () => {
    await openFiles();
    fireEvent.click(screen.getByRole("treeitem", { name: /src/ }));
    const row = await screen.findByRole("treeitem", { name: /a.ts/ });
    expect(row.querySelector(".files-status")?.textContent).toBe("M");
  });

  it("labels folders read in the session as DIR and opens them in the tree", async () => {
    const state = view(); state.accessed_files = [{ path: "src", access: "read" }, { path: "src/a.ts", access: "read" }];
    await openFiles(state);
    const dir = screen.getByRole("button", { name: /DIR\s*src/ });
    fireEvent.click(dir);
    expect(await screen.findByRole("treeitem", { name: /lib/ })).toBeTruthy();
    expect(screen.getByRole("treeitem", { name: /src/ }).getAttribute("aria-expanded")).toBe("true");
  });

  it("filters the session list by modified and read, and shows repeat counts", async () => {
    const state = view(); state.accessed_files = [
      { path: "src/a.ts", access: "write" }, { path: "README.md", access: "read" }, { path: "README.md", access: "read" },
    ];
    await openFiles(state);
    expect(screen.getByRole("button", { name: /README.md/ }).querySelector(".files-count")?.textContent).toBe("×2");
    fireEvent.click(screen.getByRole("button", { name: /Modified/ }));
    expect(screen.queryByRole("button", { name: /READ\s*README.md/ })).toBeNull();
    expect(screen.getByRole("button", { name: /MOD\s*src\/a.ts/ })).toBeTruthy();
  });

  it("moves focus through the tree with arrow keys", async () => {
    await openFiles();
    const src = screen.getByRole("treeitem", { name: /src/ });
    src.focus();
    fireEvent.keyDown(src, { key: "ArrowRight" });
    expect(await screen.findByRole("treeitem", { name: /lib/ })).toBeTruthy();
    fireEvent.keyDown(src, { key: "ArrowRight" });
    // Folders sort first, so the first child of src is lib.
    expect(document.activeElement?.textContent).toMatch(/lib/);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowLeft" });
    expect(document.activeElement).toBe(src);
  });

  it("copies the path and shows the file in the tree from its preview", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    vi.spyOn(api, "file").mockResolvedValue({ path: "src/a.ts", content: "one", binary: false });
    await openFiles();
    fireEvent.click(screen.getByRole("treeitem", { name: /src/ }));
    fireEvent.click(await screen.findByRole("treeitem", { name: /a.ts/ }));
    await screen.findByText("one");
    fireEvent.click(screen.getByRole("button", { name: "Copy path" }));
    expect(writeText).toHaveBeenCalledWith("src/a.ts");
    fireEvent.click(screen.getByRole("button", { name: "Show in tree" }));
    expect(await screen.findByRole("treeitem", { name: /a.ts/ })).toBeTruthy();
    expect(screen.queryByText("one")).toBeNull();
  });
});
