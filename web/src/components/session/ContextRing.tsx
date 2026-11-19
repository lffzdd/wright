import { useT } from "../../i18n";

export const compactTokens = (value: number | null | undefined) => typeof value === "number"
  ? value >= 1_000 ? `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}k` : String(value)
  : "unknown";

/** Circumference of restored's r=15 ring (2π·15). */
const RING = 94.25;

/**
 * Restored's hollow progress ring. Progress is an inline stroke offset — CSS
 * `stroke-dashoffset` would override an SVG attribute and freeze the ring empty.
 * Native `title` clips at the composer corner, so the usage line is a hover tip.
 */
export function ContextRing({ total, limit, onClick }: {
  total: number;
  limit: number | null | undefined;
  onClick?: () => void;
}) {
  const tr = useT();
  const percent = limit ? Math.min(100, Math.round((total / limit) * 100)) : 0;
  const tone = percent >= 90 ? "full" : percent >= 70 ? "warn" : "";
  const label = tr("web.context_usage", { used: compactTokens(total), limit: compactTokens(limit), percent });
  const title = tr("web.context_ring_title", { used: compactTokens(total), limit: compactTokens(limit) });
  const offset = RING * (1 - percent / 100);
  const body = <>
    <svg viewBox="0 0 36 36" width="18" height="18" aria-hidden="true">
      <circle className="ring-bg context-ring-track" cx="18" cy="18" r="15" />
      <circle className="ring-fg context-ring-fill" cx="18" cy="18" r="15" style={{ strokeDashoffset: offset }} />
    </svg>
    {tone ? <span className="ctx-pct" aria-hidden="true">{percent}%</span> : null}
    <span className="ctx-ring-tip" role="tooltip">{title}</span>
  </>;
  const meter = {
    role: "meter" as const,
    "aria-valuemin": 0,
    "aria-valuemax": 100,
    "aria-valuenow": percent,
    "aria-label": label,
  };
  if (!onClick) return <div className={`ctx-ring context-ring ${tone}`.trim()} {...meter}>{body}</div>;
  return <button type="button" className={`ctx-ring context-ring context-ring-button ${tone}`.trim()} aria-label={title} onClick={onClick}>
    <span className="context-ring-meter" {...meter}>{body}</span>
  </button>;
}
