import { ArrowSquareOut, CaretLeft, CaretRight, ListMagnifyingGlass, MagnifyingGlass } from "@phosphor-icons/react";
import { Fragment, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useT } from "../../i18n";
import type { ContextCategory, ContextDetail, ContextDetailQuery, ContextItem, ContextTurn } from "../../types";
import { useHighlight } from "../code/highlight";
import { SegmentedControl } from "../ui/SegmentedControl";

export const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
export const exact = new Intl.NumberFormat();

/** A share at or below this is a sliver that reads as a glitch, so it gets no shade. */
const SHADE_MIN = 0.04;

/** A row's size against the heaviest row next to it, as a faint fill behind the row. */
export function Shade({ value, max }: { value: number; max: number }) {
  const share = max > 0 ? value / max : 0;
  if (share < SHADE_MIN) return null;
  return <i className="ctx-shade" style={{ width: `${Math.min(1, share) * 100}%` }} aria-hidden="true" />;
}

/** Items drawn at once; the rest wait behind a button so a long session stays responsive. */
const PAGE = 100;

type Sort = "size" | "order";

/** Where an item happened in the conversation, as an action; none for what every request carries. */
export type ConversationJump = (item: ContextItem) => (() => void) | undefined;

/** Categories read as a sequence open in request order; the rest, where one big item is the point, biggest first. */
const SIZE_FIRST = new Set(["tool_schemas", "mcp_schemas", "tool_calls", "tool_output", "mcp_output", "subagent_output", "loaded_skills", "attachments"]);
const SORT_STORE = "wright.context.sort";
const sortKey = (query: ContextDetailQuery) => "turn" in query ? "turn" : query.category;

function readSorts(): Record<string, Sort> {
  try { return JSON.parse(localStorage.getItem(SORT_STORE) ?? "{}") as Record<string, Sort>; } catch { return {}; }
}

/** The order a person last picked for this kind of list, else the kind's default. */
function initialSort(query: ContextDetailQuery): Sort {
  const saved = readSorts()[sortKey(query)];
  if (saved === "size" || saved === "order") return saved;
  return "category" in query && SIZE_FIRST.has(query.category) ? "size" : "order";
}

function rememberSort(query: ContextDetailQuery, sort: Sort) {
  try { localStorage.setItem(SORT_STORE, JSON.stringify({ ...readSorts(), [sortKey(query)]: sort })); } catch { /* per-viewer convenience only */ }
}

/** One category or turn in full: every item, searchable, each opening to its content. */
export function ContextDetailView({ query, category, turn, label, load, onBack, conversation, onLocate, onOpenRequest }: {
  query: ContextDetailQuery;
  /** What the snapshot already knows, shown until the full list arrives or when it cannot. */
  category?: ContextCategory;
  turn?: ContextTurn;
  label: (id: string) => string;
  load?: (query: ContextDetailQuery) => Promise<ContextDetail>;
  onBack: () => void;
  conversation?: ConversationJump;
  /** Opens the full request scrolled to this item. */
  onLocate?: (itemId: string) => void;
  /** Opens the full request at this category or turn. */
  onOpenRequest?: () => void;
}) {
  const tr = useT();
  const [detail, setDetail] = useState<ContextDetail | null>(null);
  const [failed, setFailed] = useState(!load);
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<Sort>(() => initialSort(query));
  const [shown, setShown] = useState(PAGE);
  const [open, setOpen] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const queryKey = JSON.stringify(query);

  useEffect(() => {
    root.current?.focus();
  }, []);

  useEffect(() => {
    if (!load) return;
    let current = true;
    setDetail(null); setFailed(false);
    load(query).then((result) => { if (current) setDetail(result); }).catch(() => { if (current) setFailed(true); });
    return () => { current = false; };
    // The query object is rebuilt by the parent on every render; its content is the key.
  }, [queryKey, load]);

  const head = detail?.category ?? category;
  const headTurn = detail?.turn ?? turn;
  const items = useMemo(() => {
    const source = detail?.items ?? category?.items ?? [];
    const needle = filter.trim().toLowerCase();
    const matched = needle
      ? source.filter((item) => [item.label, item.detail, item.content].some((value) => value?.toLowerCase().includes(needle)))
      : source;
    return [...matched].sort(sort === "size" ? (a, b) => b.tokens - a.tokens : (a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  }, [detail, category, filter, sort]);
  const tokens = detail?.tokens ?? head?.tokens ?? headTurn?.tokens ?? 0;
  const count = detail ? detail.items.length : head?.count ?? items.length;
  const title = headTurn ? tr("web.context.turn", { n: headTurn.index + 1 }) : label(head?.id ?? ("category" in query ? query.category : ""));
  const byTool = (head?.by_tool ?? []).filter((tool) => tool.count > 1);
  const itemKey = (item: ContextItem, index: number) => item.id ?? `${item.label}-${index}`;

  return <div className="ctx-detail" ref={root} tabIndex={-1} onKeyDown={(event) => { if (event.key === "Escape" && !event.defaultPrevented) { event.preventDefault(); onBack(); } }}>
    <button type="button" className="ctx-back" onClick={onBack}><CaretLeft size={12} weight="bold" aria-hidden="true" />{tr("web.context.back")}</button>
    <header className="ctx-detail-head">
      {head ? <span className={`ctx-dot ctx-c-${head.id}`} aria-hidden="true" /> : null}
      <h3>{title}</h3>
      <span className="ctx-legend-tokens" title={`${exact.format(tokens)} tok`}>{compact.format(tokens)}</span>
      {onOpenRequest ? <button type="button" className="icon-button ctx-request-open" title={tr("web.context.open_section")} aria-label={tr("web.context.open_section")} onClick={onOpenRequest}><ListMagnifyingGlass size={14} aria-hidden="true" /></button> : null}
    </header>
    {headTurn ? <p className="ctx-detail-sub">{headTurn.label}</p> : null}
    <p className="ctx-detail-sub">{tr(count === 1 ? "web.context.items_count_one" : "web.context.items_count", { count })}</p>
    {headTurn ? <TurnBar turn={headTurn} max={headTurn.tokens} legend /> : null}
    {byTool.length > 0 && <div className="ctx-by-tool" aria-label={tr("web.context.by_tool")}>
      {byTool.map((tool) => <span key={tool.label} className="ctx-tool-sum" title={`${exact.format(tool.tokens)} tok`}><span className="mono">{tool.label}</span> ×{tool.count} · {compact.format(tool.tokens)}</span>)}
    </div>}
    <div className="ctx-detail-tools">
      <label className="ctx-filter">
        <MagnifyingGlass size={12} aria-hidden="true" />
        <input type="search" value={filter} placeholder={tr("web.context.filter")} aria-label={tr("web.context.filter")} onChange={(event) => { setFilter(event.target.value); setShown(PAGE); }} />
      </label>
      <SegmentedControl value={sort} ariaLabel={tr("web.context.sort")} onChange={(value) => { setSort(value); rememberSort(query, value); }}
        options={[{ value: "order", label: tr("web.context.sort_order") }, { value: "size", label: tr("web.context.sort_size") }]} />
    </div>
    {!detail && !failed ? <p className="hint" role="status">{tr("web.context.loading")}</p> : null}
    {failed ? <p className="hint">{tr("web.context.detail_unavailable")}</p> : null}
    {items.length === 0 && (detail || failed) ? <p className="empty-small">{tr("web.context.no_match")}</p> : null}
    <ul className="ctx-detail-list">
      {items.slice(0, shown).map((item, index) => {
        const key = itemKey(item, index);
        return <ItemRow key={key} item={item} open={open === key} onToggle={detail ? () => setOpen(open === key ? null : key) : undefined}
          categoryLabel={headTurn && item.category ? label(item.category) : undefined}
          onConversation={conversation?.(item)} onLocate={item.id && onLocate ? () => onLocate(item.id!) : undefined}
          highlight={filter.trim().toLowerCase() || undefined} />;
      })}
    </ul>
    {items.length > shown ? <button type="button" className="button ghost ctx-more" onClick={() => setShown(shown + PAGE)}>{tr("web.context.show_more", { count: Math.min(PAGE, items.length - shown) })}</button> : null}
  </div>;
}

/** `text` with every case-insensitive hit of `needle` marked. */
function marked(text: string, needle?: string): ReactNode {
  if (!needle) return text;
  const lower = text.toLowerCase();
  const out: ReactNode[] = [];
  let from = 0;
  for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, at + needle.length)) {
    if (at > from) out.push(text.slice(from, at));
    out.push(<mark key={at} className="ctx-mark">{text.slice(at, at + needle.length)}</mark>);
    from = at + needle.length;
  }
  if (!out.length) return text;
  if (from < text.length) out.push(text.slice(from));
  return out;
}

/** A line of `content` around the first hit, for an item that matched only in its body. */
function snippetOf(content: string, needle: string): string | null {
  const at = content.toLowerCase().indexOf(needle);
  if (at < 0) return null;
  const start = Math.max(0, at - 40);
  const text = content.slice(start, at + needle.length + 100).replace(/\s+/g, " ").trim();
  return `${start > 0 ? "…" : ""}${text}`;
}

export function ItemRow({ item, open, onToggle, categoryLabel, onConversation, onLocate, anchor, highlight }: {
  item: ContextItem;
  open: boolean;
  /** Absent until the full content has been fetched. */
  onToggle?: () => void;
  categoryLabel?: string;
  /** Shows where the item happened in the conversation. */
  onConversation?: () => void;
  /** Shows the item among its neighbours in the full request. */
  onLocate?: () => void;
  /** Marks the row so the full-request view can scroll to it. */
  anchor?: string;
  /** Lower-cased search text: marked in the row, with a snippet when only the body has it. */
  highlight?: string;
}) {
  const tr = useT();
  const keyed = item.key ? tr(`web.context_item.${item.key}`) : "";
  const name = keyed && keyed !== `web.context_item.${item.key}` ? keyed : item.label;
  const count = item.count ?? 0;
  const detail = item.mcp ? tr(count === 1 ? "web.mcp.tools_one" : "web.mcp.tools", { count })
    : item.key === "skill_catalog" ? tr(count === 1 ? "web.context.skill_count_one" : "web.context.skill_count", { count })
    : item.key === "image" ? [tr(count === 1 ? "web.context.image_count_one" : "web.context.image_count", { count }), item.detail].filter(Boolean).join(" · ")
    : item.loaded ? tr("web.context.loaded")
    : item.detail ?? "";
  const inRow = highlight ? [name, detail].some((value) => value.toLowerCase().includes(highlight)) : true;
  const snippet = highlight && !inRow && item.content ? snippetOf(item.content, highlight) : null;
  const body = <>
    {onToggle ? <CaretRight size={11} className="ctx-caret" aria-hidden="true" /> : null}
    {categoryLabel ? <span className={`ctx-dot ctx-c-${item.category}`} title={categoryLabel} aria-label={categoryLabel} /> : null}
    <span className="ctx-item-text">
      <span className={item.key || item.prose ? "ctx-item-name" : "ctx-item-name mono"}>{marked(name, highlight)}</span>
      {detail ? <span className={item.mcp || item.key === "skill_catalog" || item.key === "image" || item.loaded ? "ctx-item-detail" : "ctx-item-detail mono"}>{marked(detail, highlight)}</span> : null}
      {snippet ? <span className="ctx-item-snippet">{marked(snippet, highlight)}</span> : null}
    </span>
    {item.folded ? <span className="ctx-badge">{tr("web.context.folded_badge")}</span> : null}
    <span className="ctx-legend-tokens" title={`${exact.format(item.tokens)} tok`}>{compact.format(item.tokens)}</span>
  </>;
  return <li className={`ctx-entry${open ? " open" : ""}`} data-anchor={anchor}>
    <div className="ctx-entry-row">
      {onToggle ? <button type="button" className="ctx-item" aria-expanded={open} onClick={onToggle}>{body}</button> : <div className="ctx-item">{body}</div>}
      {onLocate ? <button type="button" className="icon-button ctx-jump" title={tr("web.context.locate")} aria-label={tr("web.context.locate")} onClick={onLocate}><ListMagnifyingGlass size={13} aria-hidden="true" /></button> : null}
      {onConversation ? <button type="button" className="icon-button ctx-jump" title={tr("web.context.show_call")} aria-label={tr("web.context.show_call")} onClick={onConversation}><ArrowSquareOut size={13} aria-hidden="true" /></button> : null}
    </div>
    {open ? <div className="ctx-content">
      {item.content ? <ContentBody text={item.content} prose={item.prose} highlight={highlight} /> : <p className="hint">{tr("web.context.no_content")}</p>}
      {item.truncated ? <p className="hint">{tr("web.context.truncated")}</p> : null}
    </div> : null}
  </li>;
}

const looksLikeJson = (text: string) => /^\s*[[{]/.test(text);
const TAG = /(<\/?[A-Za-z][\w:.-]*(?:\s[^<>\n]*)?\/?>)/g;

/** Wrapper tags such as `<MEMORY_CONTEXT>` dimmed, the text between them as is. */
function tagged(line: string, needle?: string): ReactNode {
  const parts = line.split(TAG);
  return parts.length === 1 ? marked(line, needle) : parts.map((part, index) => index % 2 ? <span key={index} className="ctx-tag">{marked(part, needle)}</span> : <Fragment key={index}>{marked(part, needle)}</Fragment>);
}

/**
 * An item's content exactly as sent, only colored: JSON by its grammar; other
 * text with wrapper tags dimmed and Markdown headings and fences tinted.
 */
function ContentBody({ text, prose, highlight }: { text: string; prose?: boolean; highlight?: string }) {
  const json = looksLikeJson(text);
  const tokens = useHighlight(text, json ? "json" : undefined);
  const lines = text.split("\n");
  let fenced = false;
  const body = lines.map((line, index) => {
    const tokenLine = tokens?.[index];
    let content: ReactNode;
    if (tokenLine?.length) {
      content = tokenLine.map((token, at) => <span key={at} className="cv-tok" style={token.style as CSSProperties}>{marked(token.content, highlight)}</span>);
    } else if (json) {
      content = marked(line, highlight);
    } else {
      const fence = /^\s*```/.test(line);
      const inside = fenced || fence;
      if (fence) fenced = !fenced;
      content = inside ? <span className="ctx-md-code">{marked(line, highlight)}</span>
        : /^#{1,6}\s/.test(line) ? <span className="ctx-md-head">{marked(line, highlight)}</span>
        : tagged(line, highlight);
    }
    return <Fragment key={index}>{content}{index < lines.length - 1 ? "\n" : null}</Fragment>;
  });
  return <pre className={prose && !json ? "prose" : undefined}>{body}</pre>;
}

export const TURN_PARTS = ["text", "reasoning", "tool_calls", "tool_results"] as const;

/** A turn's tokens split by kind, sized against the heaviest turn shown. */
export function TurnBar({ turn, max, legend }: { turn: ContextTurn; max: number; legend?: boolean }) {
  const tr = useT();
  const scale = max || 1;
  return <>
    <span className="ctx-turn-bar" aria-hidden="true">
      {TURN_PARTS.map((part) => turn.parts[part] > 0 ? <i key={part} className={`ctx-p-${part}`} style={{ width: `${(turn.parts[part] / scale) * 100}%` }} title={`${tr(`web.context.turn_part.${part}`)} · ${exact.format(turn.parts[part])} tok`} /> : null)}
    </span>
    {legend ? <TurnLegend turn={turn} /> : null}
  </>;
}

export function TurnLegend({ turn }: { turn?: ContextTurn }) {
  const tr = useT();
  return <ul className="ctx-turn-legend">
    {TURN_PARTS.map((part) => <li key={part}><span className={`ctx-dot ctx-p-${part}`} aria-hidden="true" />{tr(`web.context.turn_part.${part}`)}{turn ? <span className="ctx-legend-tokens">{compact.format(turn.parts[part])}</span> : null}</li>)}
  </ul>;
}
