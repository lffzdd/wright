import type { useT } from "../../../i18n";
import type { Space } from "./types";

type Tr = ReturnType<typeof useT>;

/** A label that falls back to the raw value when no translation exists. */
export function label(tr: Tr, prefix: string, value: string): string {
  const key = `${prefix}.${value}`;
  const text = tr(key);
  return text === key ? value : text;
}

export function spaceName(tr: Tr, spaceId: string, spaces: Space[] = []): string {
  if (spaceId === "global") return tr("web.memory.space.global");
  if (spaceId.startsWith("repo:")) return tr("web.memory.space.project");
  const found = spaces.find((item) => item.id === spaceId);
  return found?.name || spaceId.replace(/^personal:/, "");
}

const DAY = 86_400_000;

/** "just now", "3 hours ago", "2 days ago", else a short date. */
export function relativeTime(tr: Tr, iso: string, now = Date.now()): string {
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return "";
  const diff = now - at;
  if (diff < 60_000) return tr("web.memory.time.now");
  if (diff < 3_600_000) return tr("web.memory.time.minutes", { count: Math.floor(diff / 60_000) });
  if (diff < DAY) return tr("web.memory.time.hours", { count: Math.floor(diff / 3_600_000) });
  if (diff < 7 * DAY) return tr("web.memory.time.days", { count: Math.floor(diff / DAY) });
  return new Date(at).toLocaleDateString(undefined, { year: new Date(at).getFullYear() === new Date(now).getFullYear() ? undefined : "numeric", month: "short", day: "numeric" });
}

/** The day heading an episode or change is grouped under. */
export function dayHeading(tr: Tr, iso: string, now = new Date()): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const start = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((start(now) - start(at)) / DAY);
  if (days === 0) return tr("web.memory.time.today");
  if (days === 1) return tr("web.memory.time.yesterday");
  return at.toLocaleDateString(undefined, { year: at.getFullYear() === now.getFullYear() ? undefined : "numeric", month: "long", day: "numeric", weekday: "short" });
}

export type DueState = { tone: "danger" | "warning" | "neutral"; text: string; days: number };

/** How far a due date (YYYY-MM-DD or YYYY-MM-DDTHH:MM) is from today. */
export function dueState(tr: Tr, due: string, now = new Date()): DueState | null {
  if (!due) return null;
  const day = due.slice(0, 10);
  const [year, month, date] = day.split("-").map(Number);
  if (!year || !month || !date) return { tone: "neutral", text: due, days: Infinity };
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const days = Math.round((new Date(year, month - 1, date).getTime() - today) / DAY);
  const time = due.length > 10 ? ` ${due.slice(11, 16)}` : "";
  if (days < 0) return { tone: "danger", text: tr("web.memory.due.overdue", { count: -days }), days };
  if (days === 0) return { tone: "warning", text: tr("web.memory.due.today") + time, days };
  if (days === 1) return { tone: "warning", text: tr("web.memory.due.tomorrow") + time, days };
  if (days <= 7) return { tone: "warning", text: tr("web.memory.due.in_days", { count: days }), days };
  return { tone: "neutral", text: tr("web.memory.due.on", { date: day }), days };
}

/** Case-insensitive match of every whitespace-separated word. */
export function matches(query: string, ...texts: Array<string | undefined>): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const haystack = texts.filter(Boolean).join("\n").toLowerCase();
  return words.every((word) => haystack.includes(word));
}

/** A commit sha or PR URL, shortened for a chip. */
export function linkLabel(kind: "commits" | "prs", value: string): string {
  if (kind === "commits") return value.slice(0, 7);
  const match = /\/([^/]+)\/pull\/(\d+)/.exec(value);
  return match ? `${match[1]}#${match[2]}` : value;
}
