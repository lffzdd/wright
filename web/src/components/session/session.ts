import { t } from "../../i18n";

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
