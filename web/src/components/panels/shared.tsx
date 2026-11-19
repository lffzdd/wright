import type { ReactNode } from "react";
import type { useT } from "../../i18n";

/** Shared pieces of the capability pages and the search dialog. */

export type Notice = { tone: "idle" | "busy" | "ok" | "error"; text: string };

export function noteOf(error: unknown): Notice {
  return { tone: "error", text: error instanceof Error ? error.message : String(error) };
}

export function PanelNotice({ notice }: { notice: Notice }) {
  if (!notice.text) return null;
  return <p role={notice.tone === "error" ? "alert" : "status"} className={notice.tone === "error" ? "form-error" : "hint"}>{notice.text}</p>;
}

export function PageNotice({ notice }: { notice: Notice }) {
  if (!notice.text) return null;
  return <div className={`cap-notice ${notice.tone}`}><PanelNotice notice={notice} /></div>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="dialog-field"><span>{label}</span>{children}</label>;
}

export function statusLabel(tr: ReturnType<typeof useT>, status: string): string {
  const key = `web.status.${status}`;
  const label = tr(key);
  return label === key ? status : label;
}

/** Trimmed text, or undefined when there is none to show. */
export function preview(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

/** A positive epoch-seconds value, else null. */
export function stamp(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

/** An ISO date string as epoch seconds, else null. */
export function isoStamp(value: unknown): number | null {
  if (typeof value !== "string" || !value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed / 1000 : null;
}
