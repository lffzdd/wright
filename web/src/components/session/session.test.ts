import { describe, expect, it } from "vitest";
import { taskCode } from "./session";

describe("taskCode", () => {
  it("uses one format for the header, the selected card and compact rows", () => {
    expect(taskCode("5a3312aa9f")).toBe("#5a3312");
    expect(taskCode("task-12")).toBe("#12");
  });
});
