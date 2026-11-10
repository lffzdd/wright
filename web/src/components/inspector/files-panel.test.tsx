import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("merges absolute and relative spellings of one file without losing a write", async () => {
    const state = view(); state.accessed_files = [
      { path: "/project/src/a.ts", access: "write", tools: { edit_file: 1 } },
      { path: "src/a.ts", access: "read", tools: { read_file: 2 } },
    ];
    await openFiles(state);
    const rows = screen.getAllByRole("button", { name: /src\/a.ts/ }).filter((row) => row.classList.contains("accessed-file"));
    expect(rows).toHaveLength(1);
    expect(rows[0].querySelector(".access-tag")?.textContent).toBe("MOD");
    expect(rows[0].getAttribute("title")).toBe("src/a.ts\nedit_file ×1 · read_file ×2");
  });

  it("opens folders searched in the session in the tree", async () => {
    const state = view(); state.accessed_files = [{ path: "src", access: "read", tools: { grep: 1 } }, { path: "src/a.ts", access: "read" }];
    await openFiles(state);
    const dir = screen.getByRole("button", { name: /GREP\s*src$/ });
    fireEvent.click(dir);
    expect(await screen.findByRole("treeitem", { name: /lib/ })).toBeTruthy();
    expect(screen.getByRole("treeitem", { name: /src/ }).getAttribute("aria-expanded")).toBe("true");
  });

  it("filters the session list by modified and read, and shows repeat counts", async () => {
    const state = view(); state.accessed_files = [
      { path: "src/a.ts", access: "write" }, { path: "README.md", access: "read" }, { path: "README.md", access: "read" },
    ];
    await openFiles(state);
    expect(screen.getByRole("button", { name: /READ\s*README.md/ }).querySelector(".files-count")?.textContent).toBe("×2");
    fireEvent.click(screen.getByRole("button", { name: /Modified/ }));
    expect(screen.queryByRole("button", { name: /READ\s*README.md/ })).toBeNull();
    expect(screen.getByRole("button", { name: /MOD\s*src\/a.ts/ })).toBeTruthy();
  });

  it("tags session rows by the strongest tool used and lists every tool on hover", async () => {
    const state = view(); state.accessed_files = [
      { path: "src/a.ts", access: "write", tool: "read_file", tools: { read_file: 2, edit_file: 1 } },
      { path: "README.md", access: "read", tools: { grep: 1, read_file: 1 } },
      { path: "src", access: "read", tools: { grep: 1 } },
      { path: ".", access: "list", tools: { glob: 1 } },
    ];
    await openFiles(state);
    const written = screen.getByRole("button", { name: /MOD\s*src\/a.ts/ });
    expect(written.querySelector(".access-tag.modified")?.textContent).toBe("MOD");
    expect(written.querySelector(".files-status")).toBeNull();
    expect(written.getAttribute("title")).toBe("src/a.ts\nread_file ×2 · edit_file ×1");
    expect(screen.getByRole("button", { name: /READ\s*README.md/ }).querySelector(".access-tag.read")).toBeTruthy();
    expect(screen.getByRole("button", { name: /GREP\s*src$/ }).querySelector(".access-tag.search")?.textContent).toBe("GREP");
    expect(screen.getByRole("button", { name: /LIST\s*\.$/ }).querySelector(".access-tag.list")).toBeTruthy();
    const read = screen.getByRole("button", { name: /^Read/ });
    expect(read.querySelector("small")?.textContent).toBe("1");
    fireEvent.click(read);
    expect(screen.queryByRole("button", { name: /GREP\s*src$/ })).toBeNull();
    expect(screen.getByRole("treeitem", { name: /README.md/ }).querySelector(".files-size")?.textContent).toBe("3 B");
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

describe("Files panel readability", () => {
  it("treats a searched folder as a folder even when its parent was never expanded", async () => {
    const state = view(); state.accessed_files = [{ path: "src/lib", access: "read", tools: { grep: 1 } }];
    await openFiles(state);
    const row = screen.getByRole("button", { name: /GREP\s*src\/lib/ });
    await waitFor(() => expect(row.getAttribute("data-dir")).toBe("true"));
    expect(row.querySelector(".files-name > span")?.textContent).toBe("lib/");
  });

  it("shows the latest session rows first, with a show-all toggle, and remembers collapsing", async () => {
    const state = view(); state.accessed_files = Array.from({ length: 10 }, (_, index) => ({ path: `README.md${index}`, access: "read" }));
    await openFiles(state);
    const details = document.querySelector<HTMLDetailsElement>(".files-session");
    // Collapsed by default; the person opens it to see the rows.
    expect(details?.open).toBe(false);
    fireEvent.click(details!.querySelector("summary")!);
    expect(details?.open).toBe(true);
    await waitFor(() => expect(localStorage.getItem("wright.files.sessionOpen")).toBe("1"));
    expect(details!.querySelectorAll(".accessed-file")).toHaveLength(8);
    expect(screen.queryByRole("button", { name: /README\.md0$/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show all (10)" }));
    expect(details!.querySelectorAll(".accessed-file")).toHaveLength(10);
    fireEvent.click(screen.getByRole("button", { name: "Show fewer" }));
    expect(details!.querySelectorAll(".accessed-file")).toHaveLength(8);
    fireEvent.click(details!.querySelector("summary")!);
    expect(details?.open).toBe(false);
    await waitFor(() => expect(localStorage.getItem("wright.files.sessionOpen")).toBe("0"));
  });

  it("marks collapsed folders that hold changed or used files", async () => {
    const state = view(); state.accessed_files = [{ path: "src/lib/deep.ts", access: "read" }];
    vi.mocked(api.changes).mockResolvedValue({ changes: [], baseline: "HEAD", local_warning: false });
    await openFiles(state);
    const src = screen.getByRole("treeitem", { name: /src/ });
    expect(src.querySelector(".files-dir-mark.used")).toBeTruthy();
    fireEvent.click(src);
    expect(src.querySelector(".files-dir-mark")).toBeNull();
    expect((await screen.findByRole("treeitem", { name: /lib/ })).querySelector(".files-dir-mark.used")).toBeTruthy();
  });

  it("prefers the changed mark on folders holding working-tree changes", async () => {
    await openFiles();
    await waitFor(() => expect(screen.getByRole("treeitem", { name: /src/ }).querySelector(".files-dir-mark.changed")).toBeTruthy());
  });

  it("shows the call behind a row's tag in the conversation", async () => {
    const showCall = vi.fn();
    const state = view(); state.accessed_files = [
      { path: "src/a.ts", access: "write", call_id: "c9", tools: { edit_file: 1, read_file: 1 }, last_calls: { edit_file: "c4", read_file: "c9" } },
      { path: "README.md", access: "read", call_id: "c5", tools: { read_file: 1 }, last_calls: { read_file: "c5" } },
    ];
    render(<Inspector state={state} sessionId={state.session.session_id} open close={() => undefined} showCall={showCall} />);
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Show in conversation: src/a.ts" }));
    expect(showCall).toHaveBeenLastCalledWith("c4");
    fireEvent.click(screen.getByRole("button", { name: "Show in conversation: README.md" }));
    expect(showCall).toHaveBeenLastCalledWith("c5");
  });

  it("names a listing of the project root after the project", async () => {
    const state = view(); state.accessed_files = [{ path: ".", access: "list", tools: { list_directory: 1 } }];
    await openFiles(state);
    const row = screen.getByRole("button", { name: /LIST\s*\.$/ });
    expect(row.querySelector(".files-name > span")?.textContent).toBe("project/");
  });

  it("shows a session file in the tree from its row", async () => {
    const state = view(); state.accessed_files = [{ path: "src/lib/deep.ts", access: "read" }];
    await openFiles(state);
    fireEvent.click(screen.getByRole("button", { name: "Show in tree: src/lib/deep.ts" }));
    expect(await screen.findByRole("treeitem", { name: /deep.ts/ })).toBeTruthy();
  });


  it("marks tree rows that were used in this session", async () => {
    const state = view(); state.accessed_files = [{ path: "README.md", access: "read" }];
    await openFiles(state);
    expect(screen.getByRole("treeitem", { name: /README.md/ }).querySelector(".files-used-dot")).toBeTruthy();
    expect(screen.getByRole("treeitem", { name: /src/ }).querySelector(".files-used-dot")).toBeNull();
  });

  it("shows the cache toggle as a labeled text button", async () => {
    await openFiles();
    const toggle = screen.getByRole("button", { name: "Show cache folders" });
    expect(toggle.textContent).toBe("Show cache");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
  });
});
