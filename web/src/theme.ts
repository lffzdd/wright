export type Theme = "dark" | "light";

const CACHE_KEY = "wright.theme";

export function systemTheme(): Theme {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** The theme painted last time, so a reload does not flash the wrong one while preferences load. */
export function cachedTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(CACHE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    return null;
  }
}

export function paintTheme(next: Theme): void {
  document.documentElement.classList.toggle("light", next === "light");
  document.documentElement.classList.toggle("dark", next !== "light");
  try {
    window.localStorage.setItem(CACHE_KEY, next);
  } catch {
    // Storage can be blocked; painting still worked.
  }
}

export function paintedTheme(): Theme {
  return document.documentElement.classList.contains("light") ? "light" : "dark";
}
