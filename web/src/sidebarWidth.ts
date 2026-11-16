// Kept in step with SIDEBAR_WIDTH_RANGE in interfaces/web/routes.py.
export const SIDEBAR_WIDTH_MIN = 180;
export const SIDEBAR_WIDTH_MAX = 400;
export const SIDEBAR_WIDTH_DEFAULT = 230;

export function clampSidebarWidth(width: number, viewportWidth = typeof window !== "undefined" ? window.innerWidth : 1440): number {
  // The conversation keeps most of the window however wide the list is dragged.
  const max = Math.max(SIDEBAR_WIDTH_MIN, Math.min(SIDEBAR_WIDTH_MAX, Math.round(viewportWidth * 0.3)));
  return Math.min(max, Math.max(SIDEBAR_WIDTH_MIN, Math.round(width)));
}

export function parseSidebarWidth(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const rounded = Math.round(value);
  return rounded < SIDEBAR_WIDTH_MIN || rounded > SIDEBAR_WIDTH_MAX ? null : rounded;
}
