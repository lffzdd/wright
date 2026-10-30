import { describe, expect, it } from "vitest";
import { clampInspectorWidth, defaultInspectorWidth, parseInspectorWidth } from "./inspectorWidth";

describe("inspectorWidth", () => {
  it("picks responsive defaults", () => {
    expect(defaultInspectorWidth(1440)).toBe(380);
    expect(defaultInspectorWidth(1280)).toBe(300);
  });

  it("clamps drag widths against viewport", () => {
    expect(clampInspectorWidth(200, 1200)).toBe(280);
    expect(clampInspectorWidth(900, 1200)).toBe(540);
    expect(clampInspectorWidth(420, 1200)).toBe(420);
  });

  it("parses stored preferences", () => {
    expect(parseInspectorWidth(360)).toBe(360);
    expect(parseInspectorWidth(200)).toBeNull();
    expect(parseInspectorWidth("360")).toBeNull();
  });
});
