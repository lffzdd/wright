import { describe, expect, it } from "vitest";
import { shortTime } from "./Inspector";

describe("shortTime", () => {
  it("drops the date for today and keeps it for earlier days", () => {
    const now = new Date(2026, 9, 31, 12, 0);
    const today = new Date(2026, 9, 31, 9, 48, 46).getTime() / 1000;
    const earlier = new Date(2026, 9, 30, 9, 48, 46).getTime() / 1000;
    expect(shortTime(today, now)).not.toMatch(/2026|30|31/);
    expect(shortTime(earlier, now)).toMatch(/30/);
    expect(shortTime(today, now)).not.toMatch(/:46/);
  });
});
