import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { marked } from "marked";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NewSessionDialog, visibleModels } from "./App";
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

describe("visibleModels", () => {
  it("keeps the current and extra custom models in the list", () => {
    expect(visibleModels(["gpt-4o"], "local-oss", ["local-oss", "gpt-4o"])).toEqual([
      "gpt-4o",
      "local-oss",
    ]);
  });
});

describe("NewSessionDialog", () => {
  it("keeps keyboard focus inside the modal and closes on Escape", () => {
    const close = vi.fn();
    render(<><button>Outside session</button><NewSessionDialog
      project={{ git: true, default_environment: "local", models: ["model"], default_model: "model" }}
      close={close}
      create={async () => undefined}
    /></>);
    const create = screen.getByRole("button", { name: "Create session" });
    const closeButton = screen.getByRole("button", { name: "Close" });
    create.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(closeButton);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(close).toHaveBeenCalledOnce();
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
