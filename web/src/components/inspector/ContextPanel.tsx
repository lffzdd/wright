import { useT } from "../../i18n";
import type { ContextBreakdown, ViewState } from "../../types";

type Usage = ViewState["usage"];

const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const exact = new Intl.NumberFormat();
const percent = (value: number) => `${value < 0.01 && value > 0 ? "<1" : Math.round(value * 100)}%`;

/** Share of the limit at which the gauge turns amber, then red. */
const WARN_AT = 0.75;
const DANGER_AT = 0.9;

export function ContextPanel({ breakdown, usage, historyOnly }: { breakdown?: ContextBreakdown | null; usage: Usage; historyOnly: boolean }) {
  const tr = useT();
  const total = breakdown?.total ?? 0;
  const billed = usage.total_tokens;
  if (!total && billed == null) {
    return <p className="empty-small">{tr(historyOnly ? "web.inspector_history_context" : "web.inspector_empty_context")}</p>;
  }
  const label = (id: string) => {
    const key = `web.context_category.${id}`;
    const translated = tr(key);
    return translated === key ? id.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()) : translated;
  };
  const limit = breakdown?.limit ?? null;
  const used = limit ? Math.min(1, total / limit) : null;
  const level = used === null ? "" : used >= DANGER_AT ? "danger" : used >= WARN_AT ? "warn" : "ok";
  const categories = [...(breakdown?.categories ?? [])].sort((a, b) => b.tokens - a.tokens);
  const filled = categories.filter((item) => item.tokens > 0);
  const empty = categories.filter((item) => item.tokens <= 0);
  // Segments are sized against the limit, so the free space stays visible.
  const scale = limit && limit >= total ? limit : total || 1;
  const radius = 26;
  const circumference = 2 * Math.PI * radius;

  return <div className="context-panel">
    {total > 0 && <>
      <section className={`ctx-summary ${level}`} aria-label={tr("web.token_breakdown")}>
        {used !== null && <svg className="ctx-ring" viewBox="0 0 64 64" role="img" aria-label={tr("web.context.used", { pct: percent(used) })}>
          <circle cx="32" cy="32" r={radius} className="ctx-ring-track" />
          <circle cx="32" cy="32" r={radius} className="ctx-ring-value" strokeDasharray={`${used * circumference} ${circumference}`} transform="rotate(-90 32 32)" />
          <text x="32" y="36" textAnchor="middle">{percent(used)}</text>
        </svg>}
        <div className="ctx-summary-copy">
          <strong title={`${exact.format(total)} tok`}>{compact.format(total)}<small>{limit ? ` / ${compact.format(limit)}` : ""} tok</small></strong>
          <span>{tr(breakdown?.preview ? "web.context.preview" : "web.estimate")}</span>
          {level === "warn" || level === "danger" ? <span className="ctx-alert" role="status">{tr("web.context.near_limit")}</span> : null}
        </div>
      </section>
      <div className="ctx-bar" aria-hidden="true">
        {filled.map((item) => <i key={item.id} className={`ctx-c-${item.id}`} style={{ width: `${(item.tokens / scale) * 100}%` }} title={`${label(item.id)} · ${exact.format(item.tokens)} tok`} />)}
      </div>
      <ul className="ctx-legend">
        {filled.map((item) => <li key={item.id}>
          <span className={`ctx-dot ctx-c-${item.id}`} aria-hidden="true" />
          <span className="ctx-legend-label">{label(item.id)}</span>
          <span className="ctx-legend-tokens" title={`${exact.format(item.tokens)} tok`}>{compact.format(item.tokens)}</span>
          <span className="ctx-legend-share">{percent(total ? item.tokens / total : 0)}</span>
        </li>)}
        {limit !== null && limit > total && <li className="ctx-free">
          <span className="ctx-dot" aria-hidden="true" />
          <span className="ctx-legend-label">{tr("web.context.free")}</span>
          <span className="ctx-legend-tokens">{compact.format(limit - total)}</span>
          <span className="ctx-legend-share" />
        </li>}
      </ul>
      {empty.length > 0 && <p className="hint ctx-empty">{tr("web.context.empty_categories", { names: empty.map((item) => label(item.id)).join(", ") })}</p>}
    </>}
    {billed != null && <section className="ctx-billing">
      <div className="section-label"><span>{tr("web.context.billing")}</span></div>
      <dl>
        <div><dt>{tr("web.context.input")}</dt><dd>{usage.prompt_tokens == null ? "—" : exact.format(usage.prompt_tokens)}</dd></div>
        <div><dt>{tr("web.context.output")}</dt><dd>{usage.completion_tokens == null ? "—" : exact.format(usage.completion_tokens)}</dd></div>
        <div><dt>{tr("web.context.total")}</dt><dd>{exact.format(billed)}</dd></div>
      </dl>
    </section>}
    {breakdown?.note && total > 0 && <details className="perm-fold ctx-note"><summary>{tr("web.context.how")}</summary><p className="hint">{breakdown.note}</p></details>}
  </div>;
}
