import { describe, expect, it } from "vitest";
import { shortPath } from "./session";

describe("shortPath", () => {
  it("keeps short paths and trims long ones to the last segments", () => {
    expect(shortPath("/Users/me")).toBe("/Users/me");
    expect(shortPath("/Users/me/Project/wright")).toBe("…/me/Project/wright");
    expect(shortPath("C:\\Users\\me\\code\\app", 2)).toBe("…/code/app");
  });
});
