import { describe, expect, it } from "vitest";
import { browserLocale } from "./i18n";

describe("browserLocale", () => {
  it("picks the first supported language the browser prefers", () => {
    expect(browserLocale(["zh-TW", "en-US"])).toBe("zh-CN");
    expect(browserLocale(["en-GB", "zh-CN"])).toBe("en");
    expect(browserLocale(["fr-FR", "zh"])).toBe("zh-CN");
  });

  it("falls back to English", () => {
    expect(browserLocale(["fr-FR", "de"])).toBe("en");
    expect(browserLocale([])).toBe("en");
  });
});
