import { useT } from "../../i18n";

export const compactTokens = (value: number | null | undefined) => typeof value === "number"
  ? value >= 1_000 ? `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}k` : String(value)
  : "unknown";

const RADIUS = 6;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** How full the model context is, as a small ring; the exact figures live in its label. */
export function ContextRing({ total, limit }: { total: number; limit: number | null | undefined }) {
  const tr = useT();
  const percent = limit ? Math.min(100, Math.round((total / limit) * 100)) : 0;
  const tone = percent >= 90 ? "full" : percent >= 70 ? "warn" : "";
  const label = tr("web.context_usage", { used: compactTokens(total), limit: compactTokens(limit), percent });
  return <div
    className={`context-ring ${tone}`.trim()}
    role="meter"
    aria-valuemin={0}
    aria-valuemax={100}
    aria-valuenow={percent}
    aria-label={label}
    title={`${label}\n${tr("web.estimate")}`}
  >
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
      <circle className="context-ring-track" cx="8" cy="8" r={RADIUS} />
      <circle
        className="context-ring-fill"
        cx="8" cy="8" r={RADIUS}
        strokeDasharray={CIRCUMFERENCE}
        strokeDashoffset={CIRCUMFERENCE * (1 - percent / 100)}
      />
    </svg>
    <span aria-hidden="true">{tr("web.context_short")} {percent}%</span>
  </div>;
}
