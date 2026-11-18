import { describe, expect, it } from "vitest";
import { t } from "../../i18n";
import { conversationMarkdown, dateGroup, markdownFileName, rewindImpact, rowTime, shortPath } from "./session";

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

describe("dateGroup", () => {
  // Wednesday 18 November 2026, local time.
  const now = new Date(2026, 10, 18, 15, 0);
  const at = (day: number, month = 10, year = 2026) => new Date(year, month, day, 9, 30).toISOString();

  it("groups like the reference: today, yesterday, this week from Monday, this month, earlier", () => {
    expect(dateGroup(undefined, now)).toBe("today");
    expect(dateGroup(at(18), now)).toBe("today");
    expect(dateGroup(at(17), now)).toBe("yesterday");
    expect(dateGroup(at(16), now)).toBe("week");
    expect(dateGroup(at(15), now)).toBe("month");
    expect(dateGroup(at(1), now)).toBe("month");
    expect(dateGroup(at(31, 9), now)).toBe("older");
  });

  it("says how long ago today, the clock yesterday and the weekday this week", () => {
    expect(rowTime(new Date(now.getTime() - 12 * 60_000).toISOString(), now)).toBe(t("web.minutes_ago", { count: 12 }));
    expect(rowTime(new Date(now.getTime() - 2 * 3_600_000).toISOString(), now)).toBe(t("web.hours_ago", { count: 2 }));
    expect(rowTime(at(17), now)).toBe(t("web.yesterday_at", { time: "09:30" }));
    expect(rowTime(at(16), now)).toBe("Mon");
  });
});
