import { describe, expect, it } from "vitest";
import { describeInterval, splitInterval, toSeconds } from "./interval";

const tr = (key: string, params: Record<string, string | number> = {}) => `${key}${params.count === undefined ? "" : `:${params.count}`}`;

describe("schedule intervals", () => {
  it("picks the largest unit that divides the interval exactly", () => {
    expect(splitInterval(86400)).toEqual({ count: 1, unit: "days" });
    expect(splitInterval(7200)).toEqual({ count: 2, unit: "hours" });
    expect(splitInterval(5400)).toEqual({ count: 90, unit: "minutes" });
    expect(splitInterval(45)).toEqual({ count: 45, unit: "seconds" });
  });

  it("round-trips through seconds", () => {
    expect(toSeconds(3, "hours")).toBe(10800);
    expect(toSeconds(splitInterval(5400).count, splitInterval(5400).unit)).toBe(5400);
  });

  it("uses the singular phrase for one unit", () => {
    expect(describeInterval(3600, tr)).toBe("web.panel.every_one.hours");
    expect(describeInterval(1800, tr)).toBe("web.panel.every_many.minutes:30");
  });
});
