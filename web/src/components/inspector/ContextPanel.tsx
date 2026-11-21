import { ArrowsInSimple, ArrowUpRight, CaretRight, ListMagnifyingGlass } from "@phosphor-icons/react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { api } from "../../api";
import { useT } from "../../i18n";
import type { ContextBreakdown, ContextCategory, ContextDetail, ContextDetailQuery, ContextGroup, ContextItem, ContextPoint, ContextRequest, ContextTurn, McpServerView, MessageTarget, SkillView, ViewState } from "../../types";
import { SegmentedControl } from "../ui/SegmentedControl";
import { compact, ContextDetailView, exact, Shade, TURN_PARTS, TurnLegend } from "./ContextDetail";
import { ContextRequestDialog } from "./ContextRequest";

type Usage = ViewState["usage"];
type ConversationView = "type" | "turn";

/** Turns listed before "show all"; the heaviest few are marked. */
// The turn list shows as many whole rows as By type has, so switching views moves nothing below;
// never fewer than this.
const MIN_TURN_ROWS = 4;
// A turn shade at or below this share is a sliver, so it is not drawn.
const TURN_SHADE_MIN = 0.04;
const percent = (value: number) => `${value < 0.01 && value > 0 ? "<1" : Math.round(value * 100)}%`;
const signed = (value: number) => `${value > 0 ? "+" : value < 0 ? "−" : "±"}${Math.abs(Math.round(value * 100))}%`;
const dollars = (value: number) => `$${value < 0.01 && value > 0 ? value.toFixed(4) : value.toFixed(2)}`;

/** Share of the limit at which the gauge turns amber, then red. */
const WARN_AT = 0.75;
const DANGER_AT = 0.9;
// Below this the trend is two flat lines, so it stays one line until opened.
const TREND_OPEN_AT = 0.3;
// The fold line shows once the largest recent request reaches this share of it.
const FOLD_LINE_FROM = 0.5;

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

export function ContextPanel({ breakdown, usage, historyOnly, model, mcpServers, onCommand, onToggleMcp, onShowCall, onShowMessage, loadDetail, loadRequest, sessionId, loadSkills, onToggleSkill }: {
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
  /** Scrolls the conversation to a reply or reasoning, else to the turn's prompt. */
  onShowMessage?: (target: MessageTarget) => void;
  /** Fetches every item of a category or turn with its content; absent for saved sessions. */
  loadDetail?: (query: ContextDetailQuery) => Promise<ContextDetail>;
  /** Fetches the next request whole and in order; absent for saved sessions. */
  loadRequest?: (raw?: boolean) => Promise<ContextRequest>;
  /** Keys what the full-request dialog remembers between openings. */
  sessionId?: string;
  loadSkills?: () => Promise<SkillView[]>;
  onToggleSkill?: (id: string, enabled: boolean) => Promise<SkillView[]>;
}) {
  const tr = useT();
  const commands = usePanelCommands(Boolean(onCommand) && !historyOnly);
  const [selected, setSelected] = useState<ContextDetailQuery | null>(null);
  const [openGroups, setOpenGroups] = useState<Record<ContextGroup, boolean>>({ fixed: false, conversation: true, reserve: false });
  const [view, setView] = useState<ConversationView>("type");
  const [allTurns, setAllTurns] = useState(false);
  /** The full-request dialog, optionally scrolled to one item. */
  const [request, setRequest] = useState<{ focus?: string; section?: string } | null>(null);
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
  const turns = breakdown?.turns ?? [];
  // A tool call has its own card; a reply or reasoning its own row, with the
  // turn's prompt as the fallback when that row is not on screen.
  const turnRecords = new Map(turns.map((turn) => [turn.index, turn.record_id]));
  const conversation = (item: ContextItem) => {
    if (item.call_id && onShowCall) return () => onShowCall(item.call_id!);
    const record = item.turn != null ? turnRecords.get(item.turn) : undefined;
    return record && onShowMessage ? () => onShowMessage({ contextId: item.id, tocId: record }) : undefined;
  };
  const dialog = request && loadRequest ? <ContextRequestDialog load={loadRequest} memoryKey={sessionId}
    focus={request.focus} section={request.section} label={label} onClose={() => setRequest(null)} conversation={conversation} /> : null;
  const openRequest = loadRequest ? () => setRequest({}) : undefined;
  // What every request carries is one section; conversation categories spread over turns.
  const openSection = (query: ContextDetailQuery) => {
    if ("turn" in query) return () => setRequest({ section: `turn:${query.turn}` });
    const category = categories.find((item) => item.id === query.category);
    return category && groupOf(category) === "fixed" ? () => setRequest({ section: `cat:${query.category}` }) : undefined;
  };
  if (selected) {
    return <>
      <ContextDetailView query={selected} label={label} load={loadDetail} onBack={() => setSelected(null)} conversation={conversation}
        onLocate={loadRequest ? (focus) => setRequest({ focus }) : undefined}
        onOpenRequest={loadRequest ? openSection(selected) : undefined}
        category={"category" in selected ? categories.find((item) => item.id === selected.category) : undefined}
        turn={"turn" in selected ? turns.find((item) => item.index === selected.turn) : undefined} />
      {dialog}
    </>;
  }
  const reserve = categories.filter((item) => groupOf(item) === "reserve").reduce((sum, item) => sum + item.tokens, 0);
  // The reply reserve is room kept free, not context in use.
  const estimatedTokens = breakdown?.used ?? total - reserve;
  // Anchored on what the provider counted for the last request, when it has.
  const usedTokens = breakdown?.calibrated_used ?? estimatedTokens;
  const anchor = breakdown?.anchor;
  const limit = breakdown?.limit ?? null;
  const used = limit ? Math.min(1, usedTokens / limit) : null;
  const level = used === null ? "" : used >= DANGER_AT ? "danger" : used >= WARN_AT ? "warn" : "ok";
  const grouped = GROUPS.map((group) => ({
    group,
    // Kept in the order the server lists them, which is the order of the request.
    items: categories.filter((item) => groupOf(item) === group && item.tokens > 0),
  }));
  const filled = grouped.flatMap((entry) => entry.items);
  // Segments are sized against the limit, so the free space stays visible.
  const scale = limit && limit >= total ? limit : total || 1;
  const foldAt = breakdown?.fold_at;
  const history = breakdown?.history ?? [];
  const radius = 28;
  const circumference = 2 * Math.PI * radius;
  const sub = [model, limit !== null && limit > usedTokens ? tr("web.context.remaining", { tokens: compact.format(limit - usedTokens) }) : ""].filter(Boolean).join(" · ");
  const tokens = (value: number | null | undefined) => value == null ? "—" : compact.format(value);
  const folded = breakdown?.folded;

  return <div className="context-panel">
    {total > 0 && <section className="ctx-section" aria-label={tr("web.token_breakdown")}>
      <div className="section-label"><span>{tr("web.context.window")}</span><span className="ctx-label-end">{!anchor && (breakdown?.preview || !breakdown?.exact) ? <span className="section-count" title={tr(breakdown?.preview ? "web.context.preview" : "web.estimate")}>{tr("web.context.estimated")}</span> : null}
        {openRequest ? <button type="button" className="icon-button ctx-request-open" title={tr("web.context.open_request")} aria-label={tr("web.context.open_request")} onClick={openRequest}><ListMagnifyingGlass size={14} aria-hidden="true" /></button> : null}</span></div>
      <div className={`ctx-hero ${level}${openRequest ? " clickable" : ""}`} role={openRequest ? "button" : undefined} tabIndex={openRequest ? 0 : undefined}
        aria-label={openRequest ? tr("web.context.open_request_hint") : undefined} title={openRequest ? tr("web.context.open_request_hint") : undefined}
        onClick={openRequest} onKeyDown={openRequest ? (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openRequest(); } } : undefined}>
        {used !== null && <div className="ctx-big">
          <svg viewBox="0 0 64 64" role="img" aria-label={tr("web.context.used", { pct: percent(used) })}>
            <circle cx="32" cy="32" r={radius} className="ctx-ring-track" />
            <circle cx="32" cy="32" r={radius} className="ctx-ring-value" strokeDasharray={`${used * circumference} ${circumference}`} />
          </svg>
          <b aria-hidden="true">{percent(used)}</b>
        </div>}
        <div className="ctx-hero-copy">
          <div className="ctx-num" title={[`${exact.format(usedTokens)} tok`, anchor ? tr("web.context.anchor", { actual: compact.format(anchor.actual), drift: signed((anchor.estimated - anchor.actual) / (anchor.actual || 1)) }) : ""].filter(Boolean).join("\n")}>{compact.format(usedTokens)}{limit ? <small> / {compact.format(limit)}</small> : null}</div>
          {sub ? <div className="ctx-sub">{sub}</div> : null}
          {level === "warn" || level === "danger" ? <div className="ctx-alert" role="status">{tr("web.context.near_limit")}</div> : null}
        </div>
        {openRequest ? <ArrowUpRight size={13} className="ctx-hero-hint" aria-hidden="true" /> : null}
      </div>
      <div className="ctx-bar" aria-hidden="true">
        {filled.map((item) => <i key={item.id} className={`ctx-c-${item.id}${groupOf(item) === "reserve" ? " reserve" : ""}`} style={{ width: `${(item.tokens / scale) * 100}%` }} title={`${label(item.id)} · ${exact.format(item.tokens)} tok`} />)}
        {foldAt != null && foldAt > 0 && foldAt < scale ? <span className="ctx-fold-mark" style={{ left: `${(foldAt / scale) * 100}%` }} title={tr("web.context.fold_at", { tokens: compact.format(foldAt) })} /> : null}
      </div>
      {history.length >= 2 ? <Trend points={history} foldAt={foldAt} initiallyOpen={used !== null && used >= TREND_OPEN_AT} /> : null}
      {folded && folded.count > 0 ? <p className="hint ctx-folded">{tr("web.context.folded", { count: folded.count, tokens: compact.format(folded.tokens) })}</p> : null}
      {grouped.filter((entry) => entry.items.length > 0).map(({ group, items }) => {
        const sum = items.reduce((acc, item) => acc + item.tokens, 0);
        const single = group === "reserve";
        const open = !single && openGroups[group];
        return <div key={group} className={`ctx-group ${group}${open ? " open" : ""}`}>
          {single ? <div className="ctx-group-head" title={tr("web.context_group.reserve_hint")}><span>{tr(`web.context_group.${group}`)}</span><span className="ctx-group-fill" /><span className="ctx-legend-tokens">{compact.format(sum)}</span></div>
            : <button type="button" className="ctx-group-head" aria-expanded={open} onClick={() => setOpenGroups({ ...openGroups, [group]: !open })}>
              <CaretRight size={11} className="ctx-caret" aria-hidden="true" /><span>{tr(`web.context_group.${group}`)}</span><GroupBar items={items} sum={sum} /><span className="ctx-legend-tokens">{compact.format(sum)}</span>
            </button>}
          {/* The turn legend shares the switch's line, so both views' rows start at the same height. */}
          {open && group === "conversation" && breakdown?.turns && <div className="ctx-view-row">
            <SegmentedControl className="ctx-view" value={view} ariaLabel={tr("web.context.view")} onChange={setView}
              options={[{ value: "type", label: tr("web.context.by_type") }, { value: "turn", label: tr("web.context.by_turn") }]} />
            {view === "turn" && turns.length ? <TurnLegend /> : null}
            {view === "turn" && turns.length > Math.max(items.length, MIN_TURN_ROWS) ? <button type="button" className="text-button ctx-turns-toggle" onClick={() => setAllTurns(!allTurns)}>
              {allTurns ? tr("web.context.turns_less") : tr("web.context.turns_all", { count: turns.length })}
            </button> : null}
          </div>}
          {open && (group === "conversation" && view === "turn" && breakdown?.turns
            ? <TurnList turns={turns} omitted={breakdown?.turns_omitted ?? 0} all={allTurns} rows={Math.max(items.length, MIN_TURN_ROWS)} onOpen={(turn) => setSelected({ turn })} />
            : <ul className="ctx-legend">
              {items.map((item) => <CategoryRow key={item.id} category={item} label={label(item.id)} total={total} max={Math.max(...items.map((entry) => entry.tokens))} onOpen={() => setSelected({ category: item.id })} />)}
            </ul>)}
        </div>;
      })}
      {commands.length > 0 && <div className="ctx-actions">
        {commands.includes("/compact") && <button type="button" className="button ctx-action" onClick={() => onCommand?.("/compact")}><ArrowsInSimple size={13} aria-hidden="true" />{tr("web.context.compact")}</button>}
        {commands.includes("/clear") && <button type="button" className="button ctx-action ghost" onClick={() => onCommand?.("/clear")}>{tr("web.context.clear")}</button>}
      </div>}
    </section>}
    {billed != null && <section className="ctx-section ctx-usage">
      <div className="section-label"><span title={tr("web.context.billing_hint")}>{tr("web.context.billing")}</span></div>
      <dl className="ctx-stats" title={tr("web.context.provider_count")}>
        <div><dt>{tr("web.context.input")}</dt><dd title={usage.prompt_tokens == null ? undefined : exact.format(usage.prompt_tokens)}>{tokens(usage.prompt_tokens)}</dd></div>
        <div><dt>{tr("web.context.output")}</dt><dd title={usage.completion_tokens == null ? undefined : exact.format(usage.completion_tokens)}>{tokens(usage.completion_tokens)}</dd></div>
        <div><dt>{tr("web.context.cache_read")}</dt><dd>{tokens(usage.cache_read_tokens)}{usage.cache_read_tokens != null && usage.prompt_tokens ? <small className="ctx-rate">{percent(usage.cache_read_tokens / usage.prompt_tokens)}</small> : null}</dd></div>
        <div><dt>{tr("web.context.cost")}</dt><dd>{usage.cost_usd == null ? "—" : dollars(usage.cost_usd)}</dd></div>
      </dl>
    </section>}
    {servers.length > 0 && <McpSection servers={servers} onToggle={historyOnly ? undefined : onToggleMcp}
      onShow={openRequest && categories.some((item) => item.id === "mcp_schemas") ? (name) => setRequest({ focus: `mcp:${name}` }) : undefined} />}
    {loadSkills && !historyOnly ? <SkillsSection load={loadSkills} onToggle={onToggleSkill}
      loaded={new Set((categories.find((item) => item.id === "loaded_skills")?.items ?? []).map((item) => item.label))} /> : null}
    {breakdown?.note && total > 0 && <details className="perm-fold ctx-note"><summary>{tr("web.context.how")}</summary><p className="hint">{breakdown.note}</p></details>}
    {dialog}
  </div>;
}

/** One category; opens the detail pane with all of its items. */
function CategoryRow({ category, label, total, max, onOpen }: {
  category: ContextCategory;
  label: string;
  total: number;
  /** The heaviest category in the same group, which fills the row. */
  max: number;
  onOpen: () => void;
}) {
  const head = <>
    <Shade value={category.tokens} max={max} />
    <span className={`ctx-dot ctx-c-${category.id}`} aria-hidden="true" />
    <span className="ctx-legend-label">{label}</span>
    {category.count && category.count > 1 ? <span className="ctx-count">{category.count}</span> : null}
    <span className="ctx-legend-tokens">{compact.format(category.tokens)}</span>
  </>;
  const title = `${exact.format(category.tokens)} tok · ${percent(total ? category.tokens / total : 0)}`;
  // Only the reply reserve has nothing to list.
  if (!category.items?.length && !category.count) return <li className="ctx-cat" title={title}><div className="ctx-cat-head">{head}</div></li>;
  return <li className="ctx-cat">
    <button type="button" className="ctx-cat-head" title={title} onClick={onOpen}>{head}<CaretRight size={11} className="ctx-caret" aria-hidden="true" /></button>
  </li>;
}

/** A group's categories as one thin stacked bar. */
function GroupBar({ items, sum }: { items: ContextCategory[]; sum: number }) {
  return <span className="ctx-group-bar" aria-hidden="true">
    {items.map((item) => <i key={item.id} className={`ctx-c-${item.id}`} style={{ width: `${(item.tokens / (sum || 1)) * 100}%` }} />)}
  </span>;
}

/** The conversation one turn per row, newest last, on the same grid as the category rows. */
function TurnList({ turns, omitted, all, rows, onOpen }: { turns: ContextTurn[]; omitted: number; all: boolean; rows: number; onOpen: (turn: number) => void }) {
  const tr = useT();
  const list = useRef<HTMLOListElement>(null);
  const [height, setHeight] = useState<number | undefined>(undefined);
  const capped = !all && turns.length > rows;
  // Exactly `rows` whole rows tall, measured from the rows themselves, opened at the newest turn.
  useLayoutEffect(() => {
    const node = list.current;
    if (!capped || !node) { setHeight(undefined); return; }
    const items = node.children;
    const first = items[0] as HTMLElement | undefined;
    const last = items[rows - 1] as HTMLElement | undefined;
    if (first && last) setHeight(last.offsetTop + last.offsetHeight - first.offsetTop);
    node.scrollTop = node.scrollHeight;
  }, [capped, rows, turns.length]);
  if (!turns.length) return <p className="empty-small">{tr("web.context.no_turns")}</p>;
  const max = Math.max(...turns.map((turn) => turn.tokens), 1);
  return <div className="ctx-turns">
    {omitted > 0 ? <p className="ctx-item-more">{tr("web.context.turns_omitted", { count: omitted })}</p> : null}
    <ol className={`ctx-turn-list${capped ? " capped" : ""}`} ref={list} style={capped && height ? { maxHeight: height } : undefined}>
      {turns.map((turn) => <li key={turn.index}>
        <button type="button" className="ctx-turn" title={`${turn.label} · ${exact.format(turn.tokens)} tok`} onClick={() => onOpen(turn.index)}>
          <TurnShade turn={turn} max={max} />
          <span className="ctx-turn-n">{turn.index + 1}</span>
          <span className="ctx-turn-label">{turn.label}</span>
          <span className="ctx-legend-tokens">{compact.format(turn.tokens)}</span>
          <CaretRight size={11} className="ctx-caret" aria-hidden="true" />
        </button>
      </li>)}
    </ol>
  </div>;
}

/** A turn's size as a fill behind its row, split into the legend's colours. */
function TurnShade({ turn, max }: { turn: ContextTurn; max: number }) {
  const share = turn.tokens / max;
  if (share < TURN_SHADE_MIN) return null;
  return <span className="ctx-turn-shade" style={{ width: `${Math.min(1, share) * 100}%` }} aria-hidden="true">
    {TURN_PARTS.map((part) => turn.parts[part] > 0 ? <i key={part} className={`ctx-p-${part}`} style={{ flexGrow: turn.parts[part] }} /> : null)}
  </span>;
}

/** Size of each recent request, so growth and folding show over time; one line until opened. */
function Trend({ points, foldAt, initiallyOpen }: { points: ContextPoint[]; foldAt?: number; initiallyOpen: boolean }) {
  const tr = useT();
  const [open, setOpen] = useState(initiallyOpen);
  const [hover, setHover] = useState<number | null>(null);
  const fill = `ctx-trend-fill-${useId().replace(/:/g, "")}`;
  const values = points.map((point) => point.actual ?? point.estimated);
  const first = values[0];
  const last = values[values.length - 1];
  // The fold line only once requests come near it; drawn from far away it
  // squashes the requests into a flat line at the bottom.
  const showFold = foldAt != null && foldAt > 0 && Math.max(...values) >= foldAt * FOLD_LINE_FROM;
  const low = Math.min(...values);
  const high = Math.max(...values, showFold ? foldAt! : 0);
  const width = 100;
  const height = 36;
  // Room above and below so the line and the dot never touch the edge.
  const pad = 4;
  const step = width / (values.length - 1);
  const x = (index: number) => index * step;
  // Scaled to the requests themselves, so growth and the drop after folding show.
  const y = (value: number) => high === low ? height / 2 : height - pad - ((value - low) / (high - low)) * (height - 2 * pad);
  const line = values.map((value, index) => `${x(index).toFixed(2)},${y(value).toFixed(2)}`).join(" ");
  const area = `0,${height} ${line} ${width},${height}`;
  const title = tr("web.context.trend", { count: values.length });
  // The latest request, or the one under the pointer.
  const marked = hover ?? values.length - 1;
  return <div className={`ctx-trend${open ? " open" : ""}`}>
    <button type="button" className="ctx-trend-head" aria-expanded={open} onClick={() => setOpen(!open)}>
      <CaretRight size={11} className="ctx-caret" aria-hidden="true" />
      <span>{title}</span><span className="ctx-legend-tokens">{compact.format(first)} → {compact.format(last)}</span>
    </button>
    {open ? <div className="ctx-trend-chart" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={title}>
        <defs>
          <linearGradient id={fill} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0" className="ctx-trend-fill-top" />
            <stop offset="1" className="ctx-trend-fill-bottom" />
          </linearGradient>
        </defs>
        <polygon points={area} fill={`url(#${fill})`} />
        {showFold ? <line className="ctx-trend-fold" x1="0" x2={width} y1={y(foldAt!)} y2={y(foldAt!)}><title>{tr("web.context.fold_at", { tokens: compact.format(foldAt!) })}</title></line> : null}
        {/* One hover band per request, centred on its point; under the line so it stays visible. */}
        {values.map((value, index) => <rect key={index} className="ctx-trend-hit" x={Math.max(0, x(index) - step / 2)} y="0" width={index === 0 || index === values.length - 1 ? step / 2 : step} height={height}
          onMouseEnter={() => setHover(index)}>
          <title>{tr("web.context.trend_point", { n: index + 1, tokens: exact.format(value) })}</title>
        </rect>)}
        <polyline className="ctx-trend-line" points={line} />
      </svg>
      {/* A dot in HTML: in the stretched SVG a circle would turn into an ellipse. */}
      <i className="ctx-trend-dot" style={{ left: `${(x(marked) / width) * 100}%`, top: `${(y(values[marked]) / height) * 100}%` }} aria-hidden="true" />
    </div> : null}
  </div>;
}

/** Configured MCP servers and what each one's tool definitions cost in context. */
function McpSection({ servers, onToggle, onShow }: {
  servers: McpServerView[];
  onToggle?: (name: string, enabled: boolean) => Promise<unknown>;
  /** Opens the full request at the server's tool definitions; read-only. */
  onShow?: (name: string) => void;
}) {
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
          {onShow && server.status === "connected" && server.tokens
            ? <button type="button" className="mcp-row-body mcp-row-show" title={tr("web.mcp.show_tools", { name: server.name })} onClick={() => onShow(server.name)}>
              <span className="mono">{server.name}</span> <span className="muted">{meta}</span><ListMagnifyingGlass size={12} className="mcp-row-show-icon" aria-hidden="true" />
            </button>
            : <span className="mcp-row-body"><span className="mono">{server.name}</span> <span className="muted">{meta}</span></span>}
          {server.toggleable && onToggle ? <button type="button" role="switch" className="switch" aria-checked={server.status !== "disabled"}
            aria-label={tr("web.mcp.toggle", { name: server.name })} disabled={Boolean(busy)} title={tr("web.mcp.toggle_hint")}
            onClick={() => void toggle(server)}><span /></button> : null}
        </li>;
      })}
    </ul>
  </section>;
}

/** Skills on disk with a switch each; off ones leave the catalog from the next request. */
function SkillsSection({ load, onToggle, loaded }: {
  load: () => Promise<SkillView[]>;
  onToggle?: (id: string, enabled: boolean) => Promise<SkillView[]>;
  /** Skills whose body a load_skill call already put in the conversation. */
  loaded: Set<string>;
}) {
  const tr = useT();
  const [skills, setSkills] = useState<SkillView[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let current = true;
    load().then((rows) => { if (current) setSkills(rows); }).catch(() => undefined);
    return () => { current = false; };
  }, [load]);
  if (!skills.length) return null;
  const toggle = async (skill: SkillView) => {
    if (!onToggle || busy) return;
    setBusy(skill.id); setError("");
    try { setSkills(await onToggle(skill.id, !skill.enabled)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(""); }
  };
  return <section className="ctx-section ctx-mcp ctx-skills">
    <div className="section-label"><span>{tr("web.skills.title")}</span><span className="section-count">{skills.filter((skill) => skill.enabled).length}/{skills.length}</span></div>
    {error && <p className="form-error" role="alert">{error}</p>}
    <ul className="mcp-list">
      {skills.map((skill) => <li key={skill.id} className={`mcp-row ${skill.enabled ? "connected" : "disabled"}`} title={skill.description}>
        <span className="status-dot" aria-hidden="true" />
        <span className="mcp-row-body"><span className="mono">{skill.id}</span>{!skill.enabled && loaded.has(skill.id) ? <> <span className="muted">{tr("web.skills.off_loaded")}</span></> : null}</span>
        {onToggle ? <button type="button" role="switch" className="switch" aria-checked={skill.enabled}
          aria-label={tr("web.skills.toggle", { name: skill.id })} disabled={Boolean(busy)} title={tr("web.skills.toggle_hint")}
          onClick={() => void toggle(skill)}><span /></button> : null}
      </li>)}
    </ul>
  </section>;
}
