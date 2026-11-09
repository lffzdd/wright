import { describe, expect, it } from "vitest";
import type { TimelineItem } from "../../types";
import { segmentTimeline, stepCategory, summarizeSteps, summaryCategories } from "./steps";

const item = (id: string, kind: TimelineItem["kind"], extra: Partial<TimelineItem> = {}): TimelineItem => ({ id, kind, ...extra });

describe("segmentTimeline", () => {
  it("groups consecutive steps and lets messages split them", () => {
    const segments = segmentTimeline([
      item("u", "text", { role: "user", text: "go" }),
      item("r", "reasoning", { text: "think" }),
      item("a", "approval"),
      item("t1", "tool", { name: "grep", call_id: "t1" }),
      item("m", "text", { role: "assistant", text: "so far" }),
      item("t2", "shell", { name: "execute_command", call_id: "t2" }),
    ]);
    expect(segments.map((segment) => segment.type === "item" ? segment.item.id : segment.steps.map((step) => step.key))).toEqual([
      "u", ["r", "tool:t1"], "m", ["tool:t2"],
    ]);
  });
});

describe("summarizeSteps", () => {
  it("counts each kind of tool call, failures and time", () => {
    const [segment] = segmentTimeline([
      item("r", "reasoning", { duration_ms: 500 }),
      item("d", "tool", { name: "list_directory", phase: "succeeded", duration_ms: 20 }),
      item("f1", "tool", { name: "read_file", phase: "succeeded" }),
      item("f2", "tool", { name: "read_file", phase: "succeeded" }),
      item("g", "tool", { name: "glob_files", phase: "succeeded" }),
      item("s", "shell", { name: "execute_command", phase: "failed", duration_ms: 1000 }),
      item("e", "edit", { name: "edit_file", phase: "running" }),
      item("p", "tool", { name: "update_plan", phase: "succeeded" }),
    ]);
    if (segment.type !== "steps") throw new Error("expected steps");
    const summary = summarizeSteps(segment.steps);
    expect(summaryCategories(summary)).toEqual([["directories", 1], ["reads", 2], ["searches", 1], ["commands", 1], ["edits", 1], ["plans", 1]]);
    expect(summary).toMatchObject({ reasoning: 1, failed: 1, running: true, durationMs: 1520 });
  });
});

describe("stepCategory", () => {
  it("keeps web, memory and plan tools out of code search", () => {
    const names = ["grep", "glob", "web_search", "http_request", "search_memory", "search_episodes", "knowledge_search", "update_core_memory", "update_plan", "replan", "ask_user", "manage_loop", "mcp_tool"];
    const [segment] = segmentTimeline(names.map((name) => item(name, "tool", { name })));
    if (segment.type !== "steps") throw new Error("expected steps");
    expect(segment.steps.map(stepCategory)).toEqual(["searches", "searches", "web", "web", "memory", "memory", "memory", "memory", "plans", "plans", "tools", "tools", "tools"]);
  });
});

