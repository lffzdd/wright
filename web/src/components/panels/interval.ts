export type IntervalUnit = "seconds" | "minutes" | "hours" | "days";

export const INTERVAL_UNITS: IntervalUnit[] = ["minutes", "hours", "days", "seconds"];

const SECONDS: Record<IntervalUnit, number> = { seconds: 1, minutes: 60, hours: 3600, days: 86400 };

/** The largest unit that divides the interval exactly, so 7200 reads as 2 hours. */
export function splitInterval(seconds: number): { count: number; unit: IntervalUnit } {
  for (const unit of ["days", "hours", "minutes"] as const) {
    if (seconds >= SECONDS[unit] && seconds % SECONDS[unit] === 0) return { count: seconds / SECONDS[unit], unit };
  }
  return { count: seconds, unit: "seconds" };
}

export function toSeconds(count: number, unit: IntervalUnit): number {
  return count * SECONDS[unit];
}

export function describeInterval(seconds: number, tr: (key: string, params?: Record<string, string | number>) => string): string {
  const { count, unit } = splitInterval(seconds);
  return count === 1 ? tr(`web.panel.every_one.${unit}`) : tr(`web.panel.every_many.${unit}`, { count });
}
