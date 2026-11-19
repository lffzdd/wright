import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { setLocale } from "../../i18n";
import { MarkdownContent } from "../code/Markdown";
import { languageForFence } from "../code/highlight";
import { ToolCard } from "./ToolCard";
import { parseJsonOutput, StructuredOutput } from "./StructuredOutput";

beforeEach(() => setLocale("en"));
afterEach(() => { cleanup(); setLocale("en"); });

const memory = {
  id: "mem-a1fbc4ef51a84462",
  schema_version: 1,
  name: "Keep global Python clean",
  description: "Install third-party packages into an isolated env",
  type: "feedback",
  scope: "project",
  project_id: "wright-8a6903b8ca5a52d6",
  content: "Only install into an isolated env.\n\n**Why:** the user asked for it.",
};

it("maps Markdown fence tags to bundled grammars", () => {
  expect(languageForFence("python")).toBe("python");
  expect(languageForFence("py")).toBe("python");
  expect(languageForFence("bash")).toBe("shellscript");
  expect(languageForFence("TS")).toBe("typescript");
  expect(languageForFence("json title=x")).toBe("json");
  expect(languageForFence("brainfuck")).toBeUndefined();
  expect(languageForFence(undefined)).toBeUndefined();
});

it("labels code fences and copies from an icon button", () => {
  const { container } = render(<MarkdownContent content={"```python\nprint(1)\n```"} />);
  expect(container.querySelector(".code-lang")?.textContent).toBe("python");
  expect(screen.getByRole("button", { name: "Copy code" })).toBeTruthy();
  expect(container.querySelector("pre code")?.textContent).toBe("print(1)");
});

it("only treats JSON objects and arrays as structured output", () => {
  expect(parseJsonOutput('{"a": 1}')).toEqual({ a: 1 });
  expect(parseJsonOutput(" [1, 2] ")).toEqual([1, 2]);
  expect(parseJsonOutput("{not json")).toBeUndefined();
  expect(parseJsonOutput("plain text")).toBeUndefined();
});

it("lays out a record with its title, fields and unescaped text", () => {
  const { container } = render(<StructuredOutput value={memory} />);
  expect(container.querySelector(".so-title")?.textContent).toBe("Keep global Python clean");
  expect(container.querySelector(".so-summary")?.textContent).toBe("Install third-party packages into an isolated env");
  const fields = [...container.querySelectorAll(".so-field dt")].map((node) => node.textContent);
  expect(fields).toEqual(["type", "scope"]);
  // The text field reads as Markdown, not an escaped JSON string.
  const prose = container.querySelector(".so-prose .markdown-body");
  expect(prose?.querySelector("strong")?.textContent).toBe("Why:");
  expect(prose?.textContent).not.toContain("\\n");
  // Bookkeeping fields only appear in the folded raw JSON.
  const raw = container.querySelector(".so-raw");
  expect(raw?.hasAttribute("open")).toBe(false);
  expect(raw?.textContent).toContain("mem-a1fbc4ef51a84462");
  expect(container.querySelector(".so-fields")?.textContent).not.toContain("mem-a1fbc4ef51a84462");
});

it("keeps file text literal instead of rendering it as Markdown", () => {
  const { container } = render(<StructuredOutput value={{ path: "notes.md", content: "# Title\nline two" }} />);
  expect(container.querySelector(".so-prose .markdown-body")).toBeNull();
  expect(container.querySelector(".so-text")?.textContent).toBe("# Title\nline two");
});

it("shows short records without a title as highlighted JSON", () => {
  const { container } = render(<StructuredOutput value={{ ok: true, count: 2 }} />);
  expect(container.querySelector(".structured-output")).toBeNull();
  expect(container.querySelector(".cv")?.textContent).toContain('"count": 2');
});

it("renders a JSON tool result through the structured view", () => {
  const view = render(<ToolCard name="get_memory" phase="succeeded" output={JSON.stringify(memory)} />);
  fireEvent.click(view.getByRole("button", { expanded: false }));
  expect(view.container.querySelector(".so-title")?.textContent).toBe("Keep global Python clean");
});
