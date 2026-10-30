export const INSPECTOR_WIDTH_MIN = 280;
export const INSPECTOR_WIDTH_MAX = 560;
export const INSPECTOR_WIDTH_DEFAULT_WIDE = 380;
export const INSPECTOR_WIDTH_DEFAULT_COMPACT = 300;
export const INSPECTOR_WIDTH_BREAKPOINT = 1280;

export function defaultInspectorWidth(viewportWidth = typeof window !== "undefined" ? window.innerWidth : INSPECTOR_WIDTH_BREAKPOINT + 1): number {
  return viewportWidth <= INSPECTOR_WIDTH_BREAKPOINT ? INSPECTOR_WIDTH_DEFAULT_COMPACT : INSPECTOR_WIDTH_DEFAULT_WIDE;
}

export function clampInspectorWidth(width: number, viewportWidth = typeof window !== "undefined" ? window.innerWidth : INSPECTOR_WIDTH_BREAKPOINT + 1): number {
  const max = Math.min(INSPECTOR_WIDTH_MAX, Math.round(viewportWidth * 0.45));
  return Math.min(max, Math.max(INSPECTOR_WIDTH_MIN, Math.round(width)));
}

export function parseInspectorWidth(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const rounded = Math.round(value);
  if (rounded < INSPECTOR_WIDTH_MIN || rounded > INSPECTOR_WIDTH_MAX) return null;
  return rounded;
}
