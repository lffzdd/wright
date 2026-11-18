import { getLocale, t } from "../../i18n";
import type { TimelineItem } from "../../types";

export function formatAgo(iso?: string): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const delta = Date.now() - then;
  if (delta < 45_000) return t("web.just_now");
  if (delta < 3_600_000) return `${Math.max(1, Math.round(delta / 60_000))}m`;
  if (delta < 86_400_000) return `${Math.max(1, Math.round(delta / 3_600_000))}h`;
  return `${Math.max(1, Math.round(delta / 86_400_000))}d`;
}

export type DateGroup = "today" | "yesterday" | "week" | "month" | "older";
export const DATE_GROUPS: DateGroup[] = ["today", "yesterday", "week", "month", "older"];

/** Calendar days between `then` and `now` in local time (0 = same day). */
function daysBefore(then: Date, now: Date): number {
  const start = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  return Math.round((start(now) - start(then)) / 86_400_000);
}

/**
 * The sidebar bucket for a session, as the reference groups them: 今天 / 昨天 /
 * 本周 (calendar week from Monday) / 本月 / 更早. One never saved is happening now.
 */
export function dateGroup(iso: string | undefined, now = new Date()): DateGroup {
  const timestamp = iso ? Date.parse(iso) : Number.NaN;
  if (!Number.isFinite(timestamp)) return "today";
  const then = new Date(timestamp);
  const days = daysBefore(then, now);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  const sinceMonday = (now.getDay() + 6) % 7;
  if (days <= sinceMonday) return "week";
  if (then.getFullYear() === now.getFullYear() && then.getMonth() === now.getMonth()) return "month";
  return "older";
}

/** The time a sidebar row shows: relative today, then clock, weekday or date as it ages. */
export function rowTime(iso: string | undefined, now = new Date()): string {
  const timestamp = iso ? Date.parse(iso) : Number.NaN;
  if (!Number.isFinite(timestamp)) return "";
  const date = new Date(timestamp);
  const locale = getLocale() === "zh-CN" ? "zh-CN" : "en";
  const clock = () => date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
  switch (dateGroup(iso, now)) {
    case "today": {
      const delta = now.getTime() - timestamp;
      if (delta < 60_000) return t("web.just_now");
      if (delta < 3_600_000) return t("web.minutes_ago", { count: Math.floor(delta / 60_000) });
      return t("web.hours_ago", { count: Math.floor(delta / 3_600_000) });
    }
    case "yesterday": return t("web.yesterday_at", { time: clock() });
    case "week": return date.toLocaleDateString(locale, { weekday: "short" });
    default: return date.toLocaleDateString(locale, date.getFullYear() === now.getFullYear() ? { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" });
  }
}

export function isToday(iso?: string): boolean {
  if (!iso) return false;
  const timestamp = Date.parse(iso);
  if (!Number.isFinite(timestamp)) return false;
  const date = new Date(timestamp);
  const now = new Date();
  return date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
}

export function sessionTitle(session: { session_id: string; user_goal?: string }, tr: typeof t): string {
  const goal = session.user_goal?.trim();
  if (goal && goal !== "(interactive session)") return goal;
  return tr("web.session_label", { id: session.session_id.slice(0, 6) });
}

/** Keep the last few segments of a long path: `/a/b/c/d/e` -> `…/c/d/e`. */
export function shortPath(path: string, keep = 3): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  if (parts.length <= keep) return path;
  return `…/${parts.slice(-keep).join("/")}`;
}

/** The conversation's messages as Markdown; tool calls and reasoning are left out. */
export function conversationMarkdown(title: string, timeline: TimelineItem[], tr: typeof t): string {
  const blocks = [`# ${title}`];
  for (const item of timeline) {
    const text = item.text?.trim();
    if (item.kind !== "text" || !text) continue;
    const speaker = item.role === "user" ? tr("web.you") : "Wright";
    blocks.push(`## ${speaker}`, text);
  }
  return `${blocks.join("\n\n")}\n`;
}

/** Turns a title into a file name that every desktop OS accepts. */
export function markdownFileName(title: string): string {
  const base = title.replace(/[\\/:*?"<>|\u0000-\u001f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80);
  return `${base || "conversation"}.md`;
}

/** What dropping a turn leaves behind: files its edits touched, and whether shell commands ran. */
export function rewindImpact(timeline: TimelineItem[], messageId: string): { files: string[]; shell: boolean } {
  const start = timeline.findIndex((item) => item.id === messageId);
  if (start < 0) return { files: [], shell: false };
  const files = new Set<string>();
  let shell = false;
  for (const item of timeline.slice(start)) {
    if (item.kind === "shell") shell = true;
    if (item.kind !== "edit") continue;
    const args = item.arguments ?? {};
    const path = ["path", "file_path", "file"].map((key) => args[key]).find((value) => typeof value === "string" && value);
    if (typeof path === "string") files.add(path);
  }
  return { files: [...files], shell };
}
