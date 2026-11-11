import { describe, expect, it } from "vitest";
import { t } from "../../i18n";
import { conversationMarkdown, markdownFileName, rewindImpact, shortPath } from "./session";

describe("shortPath", () => {
  it("keeps short paths and trims long ones to the last segments", () => {
    expect(shortPath("/Users/me")).toBe("/Users/me");
    expect(shortPath("/Users/me/Project/wright")).toBe("…/me/Project/wright");
    expect(shortPath("C:\\Users\\me\\code\\app", 2)).toBe("…/code/app");
  });
});

describe("conversationMarkdown", () => {
  it("keeps user and assistant messages in order and leaves tool calls out", () => {
    const markdown = conversationMarkdown("Fix tests", [
      { id: "1", kind: "text", role: "user", text: "run the tests" },
      { id: "2", kind: "tool", name: "shell", text: "pytest" },
      { id: "3", kind: "reasoning", text: "thinking" },
      { id: "4", kind: "text", role: "assistant", text: "All **green**." },
      { id: "5", kind: "text", role: "assistant", text: "   " },
    ], t);
    expect(markdown).toBe(`# Fix tests\n\n## ${t("web.you")}\n\nrun the tests\n\n## Wright\n\nAll **green**.\n`);
  });
});

describe("markdownFileName", () => {
  it("strips characters that file systems reject", () => {
    expect(markdownFileName('fix: a/b "c"?')).toBe("fix a b c.md");
    expect(markdownFileName("  ")).toBe("conversation.md");
  });
});

describe("rewindImpact", () => {
  it("lists files edited from the message on, and notes shell commands", () => {
    const timeline = [
      { id: "msg_1", kind: "text" as const, role: "user", text: "a" },
      { id: "c0", kind: "edit" as const, arguments: { path: "before.ts" } },
      { id: "msg_3", kind: "text" as const, role: "user", text: "b" },
      { id: "c1", kind: "edit" as const, arguments: { path: "a.ts" } },
      { id: "c2", kind: "edit" as const, arguments: { file_path: "a.ts" } },
      { id: "c3", kind: "shell" as const, arguments: { command: "make" } },
    ];
    expect(rewindImpact(timeline, "msg_3")).toEqual({ files: ["a.ts"], shell: true });
    expect(rewindImpact(timeline, "missing")).toEqual({ files: [], shell: false });
  });
});
