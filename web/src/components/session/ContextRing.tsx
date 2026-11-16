import { useT } from "../../i18n";

export const compactTokens = (value: number | null | undefined) => typeof value === "number"
  ? value >= 1_000 ? `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1)}k` : String(value)
  : "unknown";

// The wedge sits inside the outline with a small gap, like a pie in a dish.
const OUTLINE = 7;
const RADIUS = 5;

/** The filled wedge of the pie, clockwise from twelve o'clock. */
function wedge(percent: number): string {
  if (percent <= 0) return "";
  const angle = (Math.min(percent, 99.99) / 100) * 2 * Math.PI;
  const x = 8 + RADIUS * Math.sin(angle);
  const y = 8 - RADIUS * Math.cos(angle);
  return `M8 8 L8 ${8 - RADIUS} A${RADIUS} ${RADIUS} 0 ${angle > Math.PI ? 1 : 0} 1 ${x.toFixed(2)} ${y.toFixed(2)} Z`;
}

/**
 * How full the model context is, as a small pie; the exact figures live in its label.
 * The percentage only shows once the context is filling up, and the pie is a
 * filled wedge rather than an arc so it never reads as a busy indicator.
 */
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
      <circle className="context-ring-track" cx="8" cy="8" r={OUTLINE} />
      {percent >= 100
        ? <circle className="context-ring-fill" cx="8" cy="8" r={RADIUS} />
        : percent > 0 && <path className="context-ring-fill" d={wedge(percent)} />}
    </svg>
    {tone && <span aria-hidden="true">{percent}%</span>}
  </div>;
}
