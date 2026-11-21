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
    if (path === "") return { path: "", entries: [entry("src", "dir", "src"), entry(".mypy_cache", "dir", ".mypy_cache"), entry("README.md"), entry(".env"), entry(".gitignore")] };
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

describe("Files used in this session", () => {
  const used = () => {
    const state = view(); state.accessed_files = [
      { path: ".", access: "list", tools: { ls: 1 }, last_calls: { ls: "call-ls" } },
      { path: "src", access: "read", tools: { grep: 1 }, last_calls: { grep: "call-grep" } },
      { path: "README.md", access: "read", tools: { read_file: 2 }, call_id: "call-read", last_calls: { read_file: "call-read" } },
      { path: "src/a.ts", access: "write", tools: { read_file: 1, edit_file: 1 }, last_calls: { edit_file: "call-edit", read_file: "call-read-late" } },
    ];
    return state;
  };
  const sessionView = () => fireEvent.click(screen.getByRole("radio", { name: "This session 3" }));
  const sessionRows = () => [...screen.getByRole("region", { name: "Used in this session" }).querySelectorAll<HTMLElement>(".lr")];
  const pathButton = (text: string) => sessionRows().map((row) => row.querySelector<HTMLButtonElement>(".files-session-path")!).find((button) => button.textContent === text)!;

  it("keeps the tree first and switches to the used files from the header", async () => {
    await openFiles(used());
    expect(screen.queryByRole("region", { name: "Used in this session" })).toBeNull();
    expect(screen.getByRole("radio", { name: "project" }).getAttribute("aria-checked")).toBe("true");
    sessionView();
    expect(screen.queryByRole("tree")).toBeNull();
    const rows = sessionRows();
    // A bare listing of the project root is not work worth a row.
    expect(rows.map((row) => row.querySelector(".files-session-path")?.textContent)).toEqual(["src/a.ts", "README.md", "src/"]);
    expect(rows[0].querySelector(".badge.M")).toBeTruthy();
    expect(rows[0].querySelector(".muted")?.textContent).toBe("read_file · edit_file");
    expect(rows[1].querySelector(".muted")?.textContent).toBe("read_file ×2");
    fireEvent.click(screen.getByRole("radio", { name: "project" }));
    expect(screen.getByRole("tree")).toBeTruthy();
  });

  it("counts only real work on the Files tab", async () => {
    render(inspector(used()));
    expect(screen.getByRole("tab", { name: /Files\s*3$/ })).toBeTruthy();
  });

  it("jumps to the call that changed the file, not a later read", async () => {
    const showCall = vi.fn();
    render(<Inspector state={used()} sessionId={sid} open close={() => undefined} showCall={showCall} />);
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    await screen.findByRole("treeitem", { name: /README.md/ });
    sessionView();
    const jump = screen.getByRole("button", { name: "Jump to this call: src/a.ts" });
    expect(jump.textContent).toBe("read_file · edit_file");
    fireEvent.click(jump);
    expect(showCall).toHaveBeenCalledWith("call-edit");
  });

  it("opens a used file and offers the conversation jump from its preview", async () => {
    const showCall = vi.fn();
    vi.spyOn(api, "file").mockResolvedValue({ path: "README.md", content: "hello", binary: false });
    render(<Inspector state={used()} sessionId={sid} open close={() => undefined} showCall={showCall} />);
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    await screen.findByRole("treeitem", { name: /README.md/ });
    sessionView();
    fireEvent.click(pathButton("README.md"));
    expect(await screen.findByText("hello")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Jump to this call" }));
    expect(showCall).toHaveBeenCalledWith("call-read");
  });

  it("opens a used folder in the tree instead of previewing it", async () => {
    await openFiles(used());
    sessionView();
    fireEvent.click(pathButton("src/"));
    expect(await screen.findByRole("treeitem", { name: /a.ts/ })).toBeTruthy();
    expect(screen.getByRole("radio", { name: "project" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.queryByRole("region", { name: "Content preview" })).toBeNull();
  });

  it("highlights the tree row after reveal in tree", async () => {
    await openFiles(used());
    sessionView();
    fireEvent.click(screen.getByRole("button", { name: "Reveal in tree: README.md" }));
    const row = await screen.findByRole("treeitem", { name: /README.md/ });
    expect(row.classList.contains("files-reveal")).toBe(true);
  });
});

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
    expect(row.classList.contains("mod")).toBe(true);
    expect(row.querySelector(".st")?.textContent).toBe("M");
  });

  it("marks a used file with the in-context dot after merging absolute and relative paths", async () => {
    const state = view(); state.accessed_files = [
      { path: "/project/src/a.ts", access: "write", tools: { edit_file: 1 } },
      { path: "src/a.ts", access: "read", tools: { read_file: 2 } },
    ];
    await openFiles(state);
    fireEvent.click(screen.getByRole("treeitem", { name: /src/ }));
    const row = await screen.findByRole("treeitem", { name: /a.ts/ });
    expect(row.querySelector(".incontext")).toBeTruthy();
    expect(screen.getAllByRole("treeitem", { name: /a.ts/ })).toHaveLength(1);
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

  it("returns to the tree from the preview", async () => {
    vi.spyOn(api, "file").mockResolvedValue({ path: "src/a.ts", content: "one", binary: false });
    await openFiles();
    fireEvent.click(screen.getByRole("treeitem", { name: /src/ }));
    fireEvent.click(await screen.findByRole("treeitem", { name: /a.ts/ }));
    await screen.findByText("one");
    fireEvent.click(screen.getByRole("button", { name: "Back to files" }));
    expect(await screen.findByRole("treeitem", { name: /a.ts/ })).toBeTruthy();
    expect(screen.queryByText("one")).toBeNull();
  });

  it("cites the open file into the composer", async () => {
    const onReference = vi.fn();
    vi.spyOn(api, "file").mockResolvedValue({ path: "src/a.ts", content: "one", binary: false });
    render(<Inspector state={view()} sessionId={sid} open close={() => undefined} onReference={onReference} />);
    fireEvent.click(screen.getByRole("tab", { name: /Files/ }));
    fireEvent.click(await screen.findByRole("treeitem", { name: /src/ }));
    fireEvent.click(await screen.findByRole("treeitem", { name: /a.ts/ }));
    await screen.findByText("one");
    expect(document.querySelector(".crumb-path")?.textContent).toBe("src/a.ts");
    expect(document.querySelector(".files-preview-head .badge")?.textContent).toBe("M");
    const cite = screen.getByRole("button", { name: "Add this file to your message" });
    expect(cite).toHaveProperty("title", "Add this file to your message");
    fireEvent.click(cite);
    expect(onReference).toHaveBeenCalledWith("src/a.ts");
  });
});

describe("Files panel readability", () => {
  it("counts the working-tree changes inside a collapsed folder", async () => {
    vi.spyOn(api, "changes").mockResolvedValue({ changes: [
      { path: "src/a.ts", status: "M" }, { path: "src/lib/deep.ts", status: "M" },
    ], baseline: "HEAD", local_warning: false });
    await openFiles();
    const src = screen.getByRole("treeitem", { name: /src/ });
    await waitFor(() => expect(src.querySelector(".files-dir-count")?.textContent).toBe("2"));
    expect(src.classList.contains("has-changes")).toBe(true);
  });

  it("fades entries git ignores", async () => {
    vi.spyOn(api, "tree").mockResolvedValue({ path: "", entries: [
      { ...entry("README.md") }, { ...entry(".env"), ignored: true }, { ...entry("log", "dir", "log"), ignored: true },
    ] });
    await openFiles();
    expect(screen.getByRole("treeitem", { name: /^\.env/ }).classList.contains("files-ignored")).toBe(true);
    expect(screen.getByRole("treeitem", { name: /log/ }).classList.contains("files-ignored")).toBe(true);
    expect(screen.getByRole("treeitem", { name: /README.md/ }).classList.contains("files-ignored")).toBe(false);
  });

  it("tells a read apart from an edit in the session mark", async () => {
    const state = view(); state.accessed_files = [
      { path: "README.md", access: "read", tools: { read_file: 1 } },
      { path: ".env", access: "write", tools: { edit_file: 1 } },
    ];
    await openFiles(state);
    const read = screen.getByRole("treeitem", { name: /README.md/ }).querySelector(".incontext");
    const edit = screen.getByRole("treeitem", { name: /^\.env/ }).querySelector(".incontext");
    expect(read?.classList.contains("read")).toBe(true);
    expect(read?.getAttribute("title")).toBe("Read in this session");
    expect(edit?.classList.contains("modified")).toBe(true);
    expect(edit?.getAttribute("title")).toBe("Edited in this session");
  });

  it("counts the working-tree changes in the header and opens them", async () => {
    vi.spyOn(api, "changes").mockResolvedValue({ changes: [
      { path: "src/a.ts", status: "M" }, { path: "README.md", status: "M" },
    ], baseline: "HEAD", local_warning: false });
    await openFiles();
    const summary = await waitFor(() => {
      const found = document.querySelector<HTMLButtonElement>(".files-change-summary");
      expect(found).toBeTruthy();
      return found!;
    });
    expect(summary.textContent).toBe("2 changed");
    fireEvent.click(summary);
    expect(await screen.findByRole("tab", { name: /Changes/, selected: true })).toBeTruthy();
  });

  it("leaves the header without a summary when nothing changed", async () => {
    vi.spyOn(api, "changes").mockResolvedValue({ changes: [], baseline: "HEAD", local_warning: false });
    await openFiles();
    expect(document.querySelector(".files-change-summary")).toBeNull();
    expect(document.querySelector(".files-count-label")).toBeNull();
  });

  it("marks tree rows that were used in this session", async () => {
    const state = view(); state.accessed_files = [{ path: "README.md", access: "read" }];
    await openFiles(state);
    expect(screen.getByRole("treeitem", { name: /README.md/ }).querySelector(".incontext")).toBeTruthy();
    expect(screen.getByRole("treeitem", { name: /src/ }).querySelector(".incontext")).toBeNull();
  });

  it("shows the cache toggle as an icon button that keeps its label", async () => {
    await openFiles();
    const toggle = screen.getByRole("button", { name: "Show cache folders" });
    expect(toggle.textContent).toBe("");
    expect(toggle.querySelector("svg")).toBeTruthy();
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
  });

  it("puts the used mark after the file name", async () => {
    const state = view(); state.accessed_files = [{ path: "README.md", access: "read" }];
    await openFiles(state);
    const row = screen.getByRole("treeitem", { name: /README.md/ });
    const dot = row.querySelector(".incontext");
    expect(dot?.previousElementSibling?.classList.contains("name")).toBe(true);
  });

  it("tints config dotfiles like other config files", async () => {
    await openFiles();
    expect(screen.getByRole("treeitem", { name: /^\.env/ }).querySelector(".ext-config")).toBeTruthy();
    expect(screen.getByRole("treeitem", { name: /\.gitignore/ }).querySelector(".ext-config")).toBeTruthy();
    expect(screen.getByRole("treeitem", { name: /README.md/ }).querySelector(".ext-doc")).toBeTruthy();
  });

  it("does not show a history disclaimer when the workspace is still readable", async () => {
    vi.spyOn(api, "workspaces").mockResolvedValue({
      selected_project_id: "project",
      projects: [{ project_id: "project", name: "project", root: "/project", exists: true }],
    });
    vi.spyOn(api, "workspaceTree").mockImplementation(async (_id, path = "") => {
      if (path === "") return { path: "", entries: [entry("README.md")] };
      return { path, entries: [] };
    });
    const state = view();
    state.session.active = false;
    await openFiles(state);
    expect(screen.queryByText(/may have changed/i)).toBeNull();
    expect(screen.queryByText(/Resume this session to browse/)).toBeNull();
  });

  it("keeps the tree on screen and spins the refresh icon while it reloads", async () => {
    await openFiles();
    let release: () => void = () => undefined;
    vi.mocked(api.tree).mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve({ path: "", entries: [entry("README.md")] }); }));
    const button = screen.getByRole("button", { name: "Refresh file tree" });
    fireEvent.click(button);
    expect(screen.getByRole("treeitem", { name: /README.md/ })).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    expect(button.getAttribute("aria-busy")).toBe("true");
    release();
    await waitFor(() => expect(button.getAttribute("aria-busy")).toBe("false"));
  });

  it("keeps the filter behind a button until asked, and Escape puts it away", async () => {
    await openFiles();
    expect(screen.queryByRole("searchbox")).toBeNull();
    fireEvent.keyDown(screen.getByRole("treeitem", { name: /README.md/ }), { key: "/" });
    const box = screen.getByRole("searchbox", { name: "Filter files in this project" });
    expect(document.activeElement).toBe(box);
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByRole("searchbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Filter files in this project" }));
    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "f", metaKey: true });
    expect(screen.getByRole("searchbox")).toBeTruthy();
  });

  it("shows matches under the project header and clears back to the tree", async () => {
    vi.spyOn(api, "search").mockResolvedValue({ kind: "file", results: [{ path: "src/a.ts", name: "a.ts", kind: "file" }] });
    const state = view(); state.accessed_files = [{ path: "README.md", access: "read" }];
    await openFiles(state);
    fireEvent.click(screen.getByRole("button", { name: "Filter files in this project" }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "a.ts" } });
    expect(await screen.findByRole("region", { name: "Matching files" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh file tree" })).toBeTruthy();
    fireEvent.blur(screen.getByRole("searchbox"));
    expect(screen.getByRole("searchbox")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(await screen.findByRole("treeitem", { name: /README.md/ })).toBeTruthy();
  });

  it("shows the full path on hover instead of repeating a root file's name", async () => {
    await openFiles();
    expect(screen.getByRole("treeitem", { name: /README.md/ }).getAttribute("title")).toBe("/project/README.md");
    fireEvent.click(screen.getByRole("treeitem", { name: /src/ }));
    expect(screen.getByRole("treeitem", { name: /src/ }).getAttribute("title")).toBe("/project/src");
    expect((await screen.findByRole("treeitem", { name: /a.ts/ })).getAttribute("title")).toBe("/project/src/a.ts");
  });

  it("names the project and shows the change count without a status legend in the pane head", async () => {
    await openFiles();
    const head = document.querySelector(".files-project-head")!;
    expect(head.querySelector("b")?.textContent).toBe("project");
    await waitFor(() => expect(head.textContent).toMatch(/1 changed/));
    // Tree rows already color M and A; the legend only took header room.
    expect(head.querySelector(".c-amber, .c-green")).toBeNull();
  });
});
