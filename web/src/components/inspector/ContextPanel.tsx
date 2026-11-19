import { ArrowsInSimple, CaretRight } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { api } from "../../api";
import { useT } from "../../i18n";
import type { ContextBreakdown, ContextCategory, ContextGroup, ContextItem, McpServerView, ViewState } from "../../types";

type Usage = ViewState["usage"];

const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const exact = new Intl.NumberFormat();
const percent = (value: number) => `${value < 0.01 && value > 0 ? "<1" : Math.round(value * 100)}%`;
const dollars = (value: number) => `$${value < 0.01 && value > 0 ? value.toFixed(4) : value.toFixed(2)}`;

/** Share of the limit at which the gauge turns amber, then red. */
const WARN_AT = 0.75;
const DANGER_AT = 0.9;

/** Slash commands the panel offers as buttons, shown only when the server registers them. */
const PANEL_COMMANDS = ["/compact", "/clear"] as const;

function usePanelCommands(enabled: boolean) {
  const [available, setAvailable] = useState<string[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let current = true;
    api.commands().then((commands) => {
      if (current) setAvailable(commands.map((command) => command.name).filter((name) => (PANEL_COMMANDS as readonly string[]).includes(name)));
    }).catch(() => undefined);
    return () => { current = false; };
  }, [enabled]);
  return available;
}

const GROUPS: ContextGroup[] = ["fixed", "conversation", "reserve"];
/** Ids from snapshots written before categories carried a group. */
const LEGACY_GROUPS: Record<string, ContextGroup> = {
  system_prompt: "fixed", tool_schemas: "fixed", memory: "fixed", rules: "fixed",
  output_reserve: "reserve",
};
const groupOf = (category: ContextCategory): ContextGroup => category.group ?? LEGACY_GROUPS[category.id] ?? "conversation";

export function ContextPanel({ breakdown, usage, historyOnly, model, mcpServers, onCommand, onToggleMcp, onShowCall }: {
  breakdown?: ContextBreakdown | null;
  usage: Usage;
  historyOnly: boolean;
  model?: string;
  mcpServers?: McpServerView[];
  /** Puts a slash command in the composer; the person still sends it. */
  onCommand?: (command: string) => void;
  onToggleMcp?: (name: string, enabled: boolean) => Promise<unknown>;
  /** Scrolls the conversation to a tool call. */
  onShowCall?: (callId: string) => void;
}) {
  const tr = useT();
  const commands = usePanelCommands(Boolean(onCommand) && !historyOnly);
  const total = breakdown?.total ?? 0;
  const billed = usage.total_tokens;
  const servers = mcpServers ?? [];
  if (!total && billed == null && !servers.length) {
    return <p className="empty-small">{tr(historyOnly ? "web.inspector_history_context" : "web.inspector_empty_context")}</p>;
  }
  const label = (id: string) => {
    const key = `web.context_category.${id}`;
    const translated = tr(key);
    return translated === key ? id.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()) : translated;
  };
  const categories = breakdown?.categories ?? [];
  const reserve = categories.filter((item) => groupOf(item) === "reserve").reduce((sum, item) => sum + item.tokens, 0);
  // The reply reserve is room kept free, not context in use.
  const usedTokens = breakdown?.used ?? total - reserve;
  const limit = breakdown?.limit ?? null;
  const used = limit ? Math.min(1, usedTokens / limit) : null;
  const level = used === null ? "" : used >= DANGER_AT ? "danger" : used >= WARN_AT ? "warn" : "ok";
  const grouped = GROUPS.map((group) => ({
    group,
    items: categories.filter((item) => groupOf(item) === group && item.tokens > 0).sort((a, b) => b.tokens - a.tokens),
  }));
  const filled = grouped.flatMap((entry) => entry.items);
  const empty = categories.filter((item) => item.tokens <= 0 && groupOf(item) !== "reserve");
  // Segments are sized against the limit, so the free space stays visible.
  const scale = limit && limit >= total ? limit : total || 1;
  const radius = 28;
  const circumference = 2 * Math.PI * radius;
  const sub = [model, limit !== null && limit > usedTokens ? tr("web.context.remaining", { tokens: compact.format(limit - usedTokens) }) : ""].filter(Boolean).join(" · ");
  const tokens = (value: number | null | undefined) => value == null ? "—" : compact.format(value);
  const folded = breakdown?.folded;

  return <div className="context-panel">
    {total > 0 && <section className="ctx-section" aria-label={tr("web.token_breakdown")}>
      <div className="section-label"><span>{tr("web.context.window")}</span>{breakdown?.preview || !breakdown?.exact ? <span className="section-count" title={tr(breakdown?.preview ? "web.context.preview" : "web.estimate")}>{tr("web.context.estimated")}</span> : null}</div>
      <div className={`ctx-hero ${level}`}>
        {used !== null && <div className="ctx-big">
          <svg viewBox="0 0 64 64" role="img" aria-label={tr("web.context.used", { pct: percent(used) })}>
            <circle cx="32" cy="32" r={radius} className="ctx-ring-track" />
            <circle cx="32" cy="32" r={radius} className="ctx-ring-value" strokeDasharray={`${used * circumference} ${circumference}`} />
          </svg>
          <b aria-hidden="true">{percent(used)}</b>
        </div>}
        <div className="ctx-hero-copy">
          <div className="ctx-num" title={`${exact.format(usedTokens)} tok`}>{compact.format(usedTokens)}{limit ? <small> / {compact.format(limit)}</small> : null}</div>
          {sub ? <div className="ctx-sub">{sub}</div> : null}
          {level === "warn" || level === "danger" ? <div className="ctx-alert" role="status">{tr("web.context.near_limit")}</div> : null}
        </div>
      </div>
      <div className="ctx-bar" aria-hidden="true">
        {filled.map((item) => <i key={item.id} className={`ctx-c-${item.id}${groupOf(item) === "reserve" ? " reserve" : ""}`} style={{ width: `${(item.tokens / scale) * 100}%` }} title={`${label(item.id)} · ${exact.format(item.tokens)} tok`} />)}
      </div>
      {folded && folded.count > 0 ? <p className="hint ctx-folded">{tr("web.context.folded", { count: folded.count, tokens: compact.format(folded.tokens) })}</p> : null}
      {grouped.filter((entry) => entry.items.length > 0).map(({ group, items }) => <div key={group} className={`ctx-group ${group}`}>
        <div className="ctx-group-head"><span>{tr(`web.context_group.${group}`)}</span><span className="ctx-legend-tokens">{compact.format(items.reduce((sum, item) => sum + item.tokens, 0))}</span></div>
        <ul className="ctx-legend">
          {items.map((item) => <CategoryRow key={item.id} category={item} label={label(item.id)} total={total} onShowCall={onShowCall} />)}
        </ul>
      </div>)}
      {empty.length > 0 && <p className="hint ctx-empty">{tr("web.context.empty_categories", { names: empty.map((item) => label(item.id)).join(", ") })}</p>}
      {commands.length > 0 && <div className="ctx-actions">
        {commands.includes("/compact") && <button type="button" className="button ctx-action" onClick={() => onCommand?.("/compact")}><ArrowsInSimple size={13} aria-hidden="true" />{tr("web.context.compact")}</button>}
        {commands.includes("/clear") && <button type="button" className="button ctx-action ghost" onClick={() => onCommand?.("/clear")}>{tr("web.context.clear")}</button>}
      </div>}
    </section>}
    {billed != null && <section className="ctx-section ctx-usage">
      <div className="section-label"><span>{tr("web.context.billing")}</span><span className="section-count" title={tr("web.estimate")}>{tr("web.context.estimated")}</span></div>
      <dl className="ctx-stats">
        <div><dt>{tr("web.context.input")}</dt><dd title={usage.prompt_tokens == null ? undefined : exact.format(usage.prompt_tokens)}>{tokens(usage.prompt_tokens)}</dd></div>
        <div><dt>{tr("web.context.output")}</dt><dd title={usage.completion_tokens == null ? undefined : exact.format(usage.completion_tokens)}>{tokens(usage.completion_tokens)}</dd></div>
        <div><dt>{tr("web.context.cache_read")}</dt><dd>{tokens(usage.cache_read_tokens)}</dd></div>
        <div><dt>{tr("web.context.cost")}</dt><dd>{usage.cost_usd == null ? "—" : dollars(usage.cost_usd)}</dd></div>
      </dl>
    </section>}
    {servers.length > 0 && <McpSection servers={servers} onToggle={historyOnly ? undefined : onToggleMcp} />}
    {breakdown?.note && total > 0 && <details className="perm-fold ctx-note"><summary>{tr("web.context.how")}</summary><p className="hint">{breakdown.note}</p></details>}
  </div>;
}

/** One category; opens to its largest items when the server listed them. */
function CategoryRow({ category, label, total, onShowCall }: {
  category: ContextCategory;
  label: string;
  total: number;
  onShowCall?: (callId: string) => void;
}) {
  const tr = useT();
  const [open, setOpen] = useState(false);
  const items = category.items ?? [];
  const more = Math.max(0, (category.count ?? items.length) - items.length);
  const head = <>
    <span className={`ctx-dot ctx-c-${category.id}`} aria-hidden="true" />
    <span className="ctx-legend-label">{label}</span>
    {category.count && category.count > 1 ? <span className="ctx-count">{category.count}</span> : null}
    <span className="ctx-legend-tokens">{compact.format(category.tokens)}</span>
  </>;
  const title = `${exact.format(category.tokens)} tok · ${percent(total ? category.tokens / total : 0)}`;
  if (!items.length) return <li className="ctx-cat" title={title}><div className="ctx-cat-head">{head}</div></li>;
  return <li className={`ctx-cat${open ? " open" : ""}`}>
    <button type="button" className="ctx-cat-head" aria-expanded={open} title={title} onClick={() => setOpen(!open)}>
      <CaretRight size={11} className="ctx-caret" aria-hidden="true" />{head}
    </button>
    {open && <ul className="ctx-items">
      {items.map((item, index) => <ItemRow key={`${item.call_id ?? item.label}-${index}`} item={item} onShowCall={onShowCall} />)}
      {more > 0 && <li className="ctx-item-more">{tr("web.context.items_more", { count: more })}</li>}
    </ul>}
  </li>;
}

function ItemRow({ item, onShowCall }: { item: ContextItem; onShowCall?: (callId: string) => void }) {
  const tr = useT();
  const keyed = item.key ? tr(`web.context_item.${item.key}`) : "";
  const name = keyed && keyed !== `web.context_item.${item.key}` ? keyed : item.label;
  const count = item.count ?? 0;
  const detail = item.mcp ? tr(count === 1 ? "web.mcp.tools_one" : "web.mcp.tools", { count })
    : item.key === "skill_catalog" ? tr(count === 1 ? "web.context.skill_count_one" : "web.context.skill_count", { count })
    : item.loaded ? tr("web.context.loaded")
    : item.detail ?? "";
  const body = <>
    <span className="ctx-item-text">
      <span className={item.key ? "ctx-item-name" : "ctx-item-name mono"}>{name}</span>
      {detail ? <span className="ctx-item-detail mono">{detail}</span> : null}
    </span>
    {item.folded ? <span className="ctx-badge">{tr("web.context.folded_badge")}</span> : null}
    <span className="ctx-legend-tokens">{compact.format(item.tokens)}</span>
  </>;
  if (item.call_id && onShowCall) {
    return <li><button type="button" className="ctx-item" title={tr("web.context.show_call")} onClick={() => onShowCall(item.call_id!)}>{body}</button></li>;
  }
  return <li><div className="ctx-item">{body}</div></li>;
}

/** Configured MCP servers and what each one's tool definitions cost in context. */
function McpSection({ servers, onToggle }: { servers: McpServerView[]; onToggle?: (name: string, enabled: boolean) => Promise<unknown> }) {
  const tr = useT();
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  // Servers in use: connected and not turned off for this conversation.
  const active = servers.filter((server) => server.status === "connected").length;
  const toggle = async (server: McpServerView) => {
    if (!onToggle || busy) return;
    setBusy(server.name); setError("");
    try { await onToggle(server.name, server.status === "disabled"); }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(""); }
  };
  return <section className="ctx-section ctx-mcp">
    <div className="section-label"><span>{tr("web.mcp.title")}</span><span className="section-count">{active}/{servers.length}</span></div>
    {error && <p className="form-error" role="alert">{error}</p>}
    <ul className="mcp-list">
      {servers.map((server) => {
        const meta = [tr(server.tool_count === 1 ? "web.mcp.tools_one" : "web.mcp.tools", { count: server.tool_count }), server.tokens ? `~${compact.format(server.tokens)}` : "", server.status === "auth_required" ? tr("web.mcp.auth_required") : server.status === "failed" ? tr("web.mcp.failed") : ""].filter(Boolean).join(" · ");
        return <li key={server.name} className={`mcp-row ${server.status}`} title={server.error ?? undefined}>
          <span className="status-dot" aria-hidden="true" />
          <span className="mcp-row-body"><span className="mono">{server.name}</span> <span className="muted">{meta}</span></span>
          {server.toggleable && onToggle ? <button type="button" role="switch" className="switch" aria-checked={server.status !== "disabled"}
            aria-label={tr("web.mcp.toggle", { name: server.name })} disabled={Boolean(busy)} title={tr("web.mcp.toggle_hint")}
            onClick={() => void toggle(server)}><span /></button> : null}
        </li>;
      })}
    </ul>
  </section>;
}
