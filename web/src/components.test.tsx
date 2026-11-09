import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { marked } from "marked";
import { afterEach, describe, expect, it, vi } from "vitest";
import { t } from "./i18n";
import { InteractionCard, ToolCard } from "./components/cards/cards";
import { SessionRail } from "./components/session/SessionRail";
import { Composer, visibleModels } from "./components/composer/Composer";
import { sessionTitle } from "./components/session/session";
import { DiffViewer } from "./DiffViewer";
import { MarkdownContent, markdownChunks } from "./Markdown";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MarkdownContent", () => {
  it("renders headers and code block with language", () => {
    const markdown = "# Heading\n\n```python\nprint('hello')\n```\n\n**bold text**";
    render(<MarkdownContent content={markdown} />);
    expect(screen.getByText("python")).toBeDefined();
    expect(screen.getByText("print('hello')")).toBeDefined();
    expect(screen.getByText("Copy")).toBeDefined();
  });

  it("strips scripts and javascript urls from rendered markdown", () => {
    const { container } = render(
      <MarkdownContent content={'hello <img src=x onerror="alert(1)"> [go](javascript:alert(1))'} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(container.querySelector("a")?.textContent).toBe("go");
  });

  it("falls back to text when markdown parsing fails", () => {
    vi.spyOn(marked, "lexer").mockImplementation(() => {
      throw new Error("boom");
    });
    expect(markdownChunks("<b>raw</b>")).toEqual([{ type: "text", text: "<b>raw</b>" }]);
    const { container } = render(<MarkdownContent content="<b>raw</b>" />);
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText("<b>raw</b>")).toBeDefined();
  });
});

describe("session titles", () => {
  it("hides the interactive placeholder from recent tasks", () => {
    expect(sessionTitle({ session_id: "abcdef123", user_goal: "(interactive session)" }, t)).toBe("Session abcdef");
    expect(sessionTitle({ session_id: "abcdef123", user_goal: "Fix Redis leak" }, t)).toBe("Fix Redis leak");
  });
});

describe("visibleModels", () => {
  it("keeps the current and extra custom models in the list", () => {
    expect(visibleModels(["gpt-4o"], "local-oss", ["local-oss", "gpt-4o"])).toEqual([
      "gpt-4o",
      "local-oss",
    ]);
  });

  it("lists configured models without inventing a fallback", () => {
    expect(visibleModels(["deepseek-v4-flash", "deepseek-chat"], "deepseek-v4-flash")).toEqual([
      "deepseek-v4-flash",
      "deepseek-chat",
    ]);
  });
});

describe("draft environment", () => {
  it("offers the current checkout by default and an explicit isolated option", () => {
    render(<Composer
      sessionId="draft:project"
      connection="connected"
      draft={{ prompt: "", attachments: [], commandId: null, phase: "idle", reason: "", environment: "local" }}
      updateDraft={() => undefined}
      submit={async () => "accepted"}
      cancel={async () => undefined}
      running={false}
      chooseEnvironment
      git
      workLabel="/tmp/project"
    />);
    expect(screen.getByRole("button", { name: "This checkout" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Isolated execution" }).hasAttribute("disabled")).toBe(false);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("InteractionCard", () => {
  it("shows a specific reason when the server gives one", () => {
    render(<InteractionCard sessionId="s" interaction={{
      request_id: "r0", kind: "permission", tool_name: "execute_command", command: "pip install x",
      reason_code: "permission.network_requested", reason: "network", choices: [],
    }} respond={() => true} />);
    expect(screen.getByText("This command requests network access")).toBeDefined();
  });

  const interaction = {
    request_id: "r1",
    kind: "permission" as const,
    tool_name: "write_file",
    subject: "nested/a.txt",
    reason: "needs approval",
    operation: "file_write",
    grant_summary: "Adds file root /tmp/project/nested",
    preview: "hello <script>alert(1)</script>",
    shell_note: "",
    choices: [
      { id: "allow_once", label: "Allow once", scope: "This invocation only", persistence: "No save" },
      { id: "deny", label: "Deny", scope: "No execution", persistence: "No save" },
    ],
  };

  it("shows the reason, grant and preview as text and ignores a second click", () => {
    const respond = vi.fn(() => new Promise<boolean>(() => undefined));
    const { container } = render(<InteractionCard sessionId="s" interaction={interaction} respond={respond} />);
    // A reason that only says "needs approval" is noise; it is not shown.
    expect(screen.queryByText(/needs approval/)).toBeNull();
    expect(screen.getByText("hello <script>alert(1)</script>")).toBeDefined();
    expect(container.querySelector("script")).toBeNull();
    const allow = screen.getByRole("button", { name: /Allow once/i });
    fireEvent.click(allow);
    fireEvent.click(allow);
    expect(respond).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Submitting…")).toBeDefined();
  });

  it("keeps the selected choice when the decision is not accepted", async () => {
    const respond = vi.fn(async () => false);
    render(<InteractionCard sessionId="s" interaction={interaction} respond={respond} />);
    fireEvent.click(screen.getByRole("button", { name: /Allow once/i }));
    expect(await screen.findByText(/Kept choice: Allow once/i)).toBeDefined();
    expect(screen.getByRole("button", { name: /Allow once/i })).toBeDefined();
  });

  it("does not dump grant scopes into a truncated summary row", () => {
    render(<InteractionCard sessionId="s" interaction={{
      ...interaction,
      risk_flags: ["recursive_delete"],
      grant_summary: "./build, ./dist",
      choices: [
        { id: "allow_once", label: "Allow once", scope: "This invocation only", persistence: "No save" },
        { id: "deny", label: "Deny", scope: "No execution", persistence: "No save" },
      ],
    }} respond={() => true} />);
    expect(screen.queryByText(/Allow once: This invocation only/)).toBeNull();
    expect(screen.getByText("Recursive deletion in workspace")).toBeDefined();
    expect(screen.getByText("Directory purge outside git tracking")).toBeDefined();
    expect(screen.queryByText("recursive_delete")).toBeNull();
  });
});

describe("ToolCard", () => {
  it("marks a failed edit as failed and hides its empty line counts", () => {
    render(<ToolCard name="edit_file" phase="failed" args={{ file: "src/calc.py" }} output="InputValidationError" />);
    expect(screen.getByText("failed")).toBeTruthy();
    expect(screen.queryByText("+0")).toBeNull();
  });

  it("does not add a failed label beside a shell exit code", () => {
    render(<ToolCard name="execute_command" phase="failed" args={{ command: "false" }} data={{ exit_code: 1 }} />);
    expect(screen.getByText("Exit 1")).toBeTruthy();
    expect(screen.queryByText("failed")).toBeNull();
  });
});

describe("ToolCard states", () => {
  it("labels a denied call without expanding it", () => {
    render(<ToolCard name="execute_command" phase="failed" args={{ command: "rm a" }} data={{ permission: { decision: "deny" } }} output="Permission denied" />);
    expect(screen.getByText("Denied")).toBeTruthy();
    expect(screen.queryByText("failed")).toBeNull();
    expect(screen.queryByText("Permission denied")).toBeNull();
  });

  it("collapses a finished call like a reload would, unless the person toggled it", () => {
    const card = (phase: string) => <ToolCard name="read_file" phase={phase} output="file body" />;
    const { rerender } = render(card("running"));
    expect(screen.getByText("file body")).toBeTruthy();
    rerender(card("succeeded"));
    expect(screen.queryByText("file body")).toBeNull();
    fireEvent.click(screen.getByRole("button"));
    rerender(card("succeeded"));
    expect(screen.getByText("file body")).toBeTruthy();
  });

  it("lists directory entries instead of tree-prefixing the raw JSON", () => {
    const data = { directory: ".", truncated: false, entries: [
      { name: "src", path: "src", type: "directory", size: null },
      { name: "README.md", path: "README.md", type: "file", size: 2048 },
    ] };
    render(<ToolCard name="list_directory" phase="succeeded" data={data} output={JSON.stringify(data, null, 2)} />);
    fireEvent.click(screen.getByRole("button"));
    const entries = [...document.querySelectorAll(".dir-entry")].map((entry) => entry.textContent);
    expect(entries).toEqual(["src/", "README.md2.0 KB"]);
    expect(document.querySelector(".term-body")).toBeNull();
  });

  it("groups search hits by file and marks the pattern", () => {
    const data = { matches: [
      { path: "a.py", line: 3, column: 1, text: "raise HostBusy()" },
      { path: "a.py", line: 9, column: 5, text: "except HostBusy:" },
      { path: "b.py", line: 1, column: 1, text: "from a import HostBusy" },
    ], truncated: false };
    render(<ToolCard name="grep" phase="succeeded" args={{ pattern: "Host[A-Z]\\w+" }} data={data} />);
    expect(screen.getByText("3 matches in 2 files")).toBeTruthy();
    fireEvent.click(screen.getByRole("button"));
    expect([...document.querySelectorAll(".search-path")].map((node) => node.textContent)).toEqual(["a.py", "b.py"]);
    expect([...document.querySelectorAll(".search-results mark")].map((node) => node.textContent)).toEqual(["HostBusy", "HostBusy", "HostBusy"]);
  });

  it("shows read file content with a line-number gutter", () => {
    render(<ToolCard name="read_file" phase="succeeded" args={{ path: "a.py" }} data={{ content: "10|def f():\n11|    return 1\n" }} />);
    fireEvent.click(screen.getByRole("button"));
    expect([...document.querySelectorAll(".code-num")].map((node) => node.textContent)).toEqual(["10", "11"]);
    expect(screen.getByText("def f():")).toBeTruthy();
  });

  it("puts the shell command above its output", () => {
    render(<ToolCard name="execute_command" phase="running" args={{ command: "pytest -q" }} output="collecting" />);
    expect(document.querySelector(".tool-card-body .term-cmd")?.textContent).toBe("$ pytest -q");
  });

  it("shows what a search looked for and a quiet success for exit 0", () => {
    render(<>
      <ToolCard name="grep" phase="succeeded" args={{ pattern: "host_busy", path: "src/wright" }} output="hit" />
      <ToolCard name="execute_command" phase="succeeded" args={{ command: "true" }} data={{ exit_code: 0 }} />
    </>);
    expect(screen.getByText("host_busy")).toBeTruthy();
    // Success is the default; only failures earn a label.
    expect(screen.queryByText("Exit 0")).toBeNull();
    expect(document.querySelectorAll(".step-icon.failed")).toHaveLength(0);
  });

  it("hides line counts until an edit has been applied", () => {
    render(<ToolCard name="edit_file" phase="awaiting_approval" args={{ file: "a.py" }} />);
    expect(screen.queryByText("+0")).toBeNull();
  });
});

describe("SessionRail", () => {
  const rail = (workspaces: { project_id: string; name: string; selected?: boolean }[]) => <SessionRail
    sessions={[
      { session_id: "a", user_goal: "Listed task", project_id: "one", active: true, status: "idle", agent_status: "completed" },
      { session_id: "b", user_goal: "Other project task", project_id: "gone", active: true, status: "idle", agent_status: "completed" },
    ]}
    selected={null} onSelect={vi.fn()} onCreate={vi.fn()} open workspaces={workspaces}
  />;

  it("lists an open session once and keeps sessions without a workspace row", () => {
    render(rail([{ project_id: "one", name: "One", selected: true }]));
    expect(screen.getAllByRole("button", { name: /Listed task/ })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /Other project task/ })).toHaveLength(1);
  });

  it("shows no live dot when no open session is working", () => {
    const { container } = render(rail([]));
    expect(screen.getByText("Open Tasks")).toBeTruthy();
    expect(container.querySelector(".live-dot")).toBeNull();
  });
});

describe("DiffViewer", () => {
  it("renders diff additions, deletions, and stats", () => {
    const patch = `--- a/file.py
+++ b/file.py
@@ -1,3 +1,3 @@
 line 1
-deleted line
+added line
 line 3`;
    render(<DiffViewer patch={patch} filename="file.py" />);
    expect(screen.getByText("file.py")).toBeDefined();
    expect(screen.getByText("+1")).toBeDefined();
    expect(screen.getByText("-1")).toBeDefined();
    expect(screen.getByText("added line")).toBeDefined();
    expect(screen.getByText("deleted line")).toBeDefined();
  });
});
