/** How folded reasoning sits among tool calls: a quiet aside, or the lead line with tools under it. */
export type ThoughtStyle = "quiet" | "lead";

const CACHE_KEY = "wright.thought_style";

export function isThoughtStyle(value: unknown): value is ThoughtStyle {
  return value === "quiet" || value === "lead";
}

/** The style painted last time, so a reload does not flash the default while preferences load. */
export function cachedThoughtStyle(): ThoughtStyle | null {
  try {
    const value = window.localStorage.getItem(CACHE_KEY);
    return isThoughtStyle(value) ? value : null;
  } catch {
    return null;
  }
}

/** The timeline reads it from `data-thought-style` on the root, like the theme class. */
export function paintThoughtStyle(next: ThoughtStyle): void {
  document.documentElement.dataset.thoughtStyle = next;
  try {
    window.localStorage.setItem(CACHE_KEY, next);
  } catch {
    // Storage can be blocked; painting still worked.
  }
}

export function paintedThoughtStyle(): ThoughtStyle {
  const value = document.documentElement.dataset.thoughtStyle;
  return isThoughtStyle(value) ? value : "quiet";
}
