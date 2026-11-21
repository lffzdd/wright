import { ArrowsInLineVertical, ArrowsOutLineVertical, CaretRight, Check, Copy, MagnifyingGlass, Warning, X } from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useT } from "../../i18n";
import type { ContextItem, ContextRequest } from "../../types";
import { compact, type ConversationJump, exact, ItemRow, Shade } from "./ContextDetail";

type Section = { key: string; title: string; category?: string; turn?: number; items: ContextItem[]; tokens: number };

/** Consecutive items of one category, or of one turn, form a section, in request order. */
function sectionsOf(items: ContextItem[], label: (id: string) => string, turnTitle: (index: number) => string, turnLabels: Map<number, string>): Section[] {
  const sections: Section[] = [];
  for (const item of items) {
    const key = item.turn != null ? `turn:${item.turn}` : `cat:${item.category ?? ""}`;
    let last = sections[sections.length - 1];
    if (!last || last.key !== key) {
      const title = item.turn != null
        ? [turnTitle(item.turn), turnLabels.get(item.turn)].filter(Boolean).join(" · ")
        : label(item.category ?? "");
      last = { key, title, category: item.turn != null ? undefined : item.category, turn: item.turn, items: [], tokens: 0 };
      sections.push(last);
    }
    last.items.push(item);
    last.tokens += item.tokens;
  }
  return sections;
}

/** Same flash as a jump in the conversation, replayed on every call. */
function flash(element: HTMLElement | null | undefined) {
  if (!element) return;
  element.classList.remove("reveal-flash");
  void element.offsetWidth;
  element.classList.add("reveal-flash");
  window.setTimeout(() => element.classList.remove("reveal-flash"), 2000);
}

/** What was folded and opened, and where it was scrolled, per session; kept until reload. */
type Remembered = { expanded: Set<string>; open: Set<string>; scroll: number };
const remembered = new Map<string, Remembered>();

const matches = (item: ContextItem, needle: string) =>
  [item.label, item.detail, item.content].some((value) => value?.toLowerCase().includes(needle));

/** Every item of the next request, in the order it is sent, with a table of contents. */
export function ContextRequestDialog({ load, memoryKey, focus, section: focusSection, label, onClose, conversation }: {
  load: (raw?: boolean) => Promise<ContextRequest>;
  /** Folding and scroll are restored for the same key when the dialog opens again. */
  memoryKey?: string;
  /** Item to scroll to and open once loaded. */
  focus?: string;
  /** Section to scroll to once loaded, as `cat:<id>` or `turn:<index>`. */
  section?: string;
  label: (id: string) => string;
  onClose: () => void;
  /** Jumps to the conversation; the dialog closes first. */
  conversation?: ConversationJump;
}) {
  const tr = useT();
  const [data, setData] = useState<ContextRequest | null>(null);
  const [failed, setFailed] = useState(false);
  const [filter, setFilter] = useState("");
  // Sections start folded, so the request reads as an outline. Reopening keeps
  // what was unfolded last time; a jump adds its target to that.
  const saved = useRef(memoryKey ? remembered.get(memoryKey) : undefined);
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    const next = new Set(saved.current?.expanded);
    if (focusSection) next.add(focusSection);
    return next;
  });
  const [open, setOpen] = useState<Set<string>>(() => {
    const next = new Set(saved.current?.open);
    if (focus) next.add(focus);
    return next;
  });
  const latest = useRef({ expanded, open });
  latest.current = { expanded, open };
  const [copied, setCopied] = useState<"" | "ok" | "failed">("");
  const dialog = useRef<HTMLDivElement>(null);
  const main = useRef<HTMLDivElement>(null);
  const restore = useRef<Element | null>(document.activeElement);
  const focused = useRef(false);

  useEffect(() => {
    dialog.current?.focus();
    const previous = restore.current;
    const body = main.current;
    return () => {
      if (memoryKey) remembered.set(memoryKey, { ...latest.current, scroll: body?.scrollTop ?? 0 });
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, [memoryKey]);

  useEffect(() => {
    let current = true;
    setData(null); setFailed(false);
    load().then((result) => { if (current) setData(result); }).catch(() => { if (current) setFailed(true); });
    return () => { current = false; };
  }, [load]);

  // Back where it was left, unless the dialog was opened to show something.
  useEffect(() => {
    if (!data || focus || focusSection || focused.current) return;
    focused.current = true;
    if (main.current && saved.current?.scroll) main.current.scrollTop = saved.current.scroll;
  }, [data, focus, focusSection]);

  const turnLabels = useMemo(() => new Map((data?.turns ?? []).map((turn) => [turn.index, turn.label])), [data]);
  const sections = useMemo(() => sectionsOf(data?.items ?? [], label, (index) => tr("web.context.turn", { n: index + 1 }), turnLabels), [data, label, tr, turnLabels]);
  const needle = filter.trim().toLowerCase();
  const shown = useMemo(() => needle
    ? sections.map((section) => ({ ...section, items: section.items.filter((item) => matches(item, needle)) })).filter((section) => section.items.length > 0)
    : sections, [sections, needle]);
  const matchCount = shown.reduce((sum, section) => sum + section.items.length, 0);
  const heaviest = Math.max(...sections.map((section) => section.tokens), 1);

  useEffect(() => {
    if (!data || !focusSection || focus || focused.current) return;
    const head = main.current?.querySelector<HTMLElement>(`[data-section="${focusSection}"]`);
    if (!head) return;
    focused.current = true;
    head.scrollIntoView?.({ block: "start" });
    flash(head.querySelector<HTMLElement>(":scope > .ctx-group-head"));
  }, [data, focus, focusSection]);

  useEffect(() => {
    if (!data || !focus || focused.current) return;
    // Unfold the section holding the focused item first; the row renders after.
    const home = sections.find((section) => section.items.some((item) => item.id === focus));
    if (home && !expanded.has(home.key)) {
      setExpanded((previous) => new Set(previous).add(home.key));
      return;
    }
    const row = [...(main.current?.querySelectorAll<HTMLElement>("[data-anchor]") ?? [])].find((element) => element.dataset.anchor === focus);
    if (!row) return;
    focused.current = true;
    row.scrollIntoView?.({ block: "center" });
    flash(row);
  }, [data, focus, sections, expanded]);

  const jump = (key: string) => {
    setExpanded((previous) => new Set(previous).add(key));
    requestAnimationFrame(() => {
      const section = main.current?.querySelector<HTMLElement>(`[data-section="${key}"]`);
      section?.scrollIntoView?.({ block: "start" });
      flash(section?.querySelector<HTMLElement>(":scope > .ctx-group-head"));
    });
  };
  const toggleSection = (key: string) => setExpanded((previous) => {
    const next = new Set(previous);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  const allOpen = sections.length > 0 && sections.every((section) => expanded.has(section.key));
  const toggleItem = (id: string) => setOpen((previous) => {
    const next = new Set(previous);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const copy = async () => {
    try {
      const raw = await load(true);
      await navigator.clipboard.writeText(JSON.stringify(raw.request ?? {}, null, 2));
      setCopied("ok");
    } catch { setCopied("failed"); }
    window.setTimeout(() => setCopied(""), 2000);
  };
  const jumpOf = (item: ContextItem) => {
    const go = conversation?.(item);
    return go ? () => { onClose(); go(); } : undefined;
  };
  // The same total as the ring: anchored on the provider's last count when there is one.
  const headline = data ? data.calibrated_used ?? data.used ?? data.tokens : 0;
  const itemId = (item: ContextItem, index: number) => item.id ?? `${item.label}-${index}`;

  return createPortal(<div className="ctx-request-backdrop" onPointerDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="ctx-request" role="dialog" aria-modal="true" aria-labelledby="ctx-request-title" ref={dialog} tabIndex={-1}
      onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }}>
      <header className="ctx-request-bar">
        <h2 id="ctx-request-title">{tr("web.context.full_request")}</h2>
        {data ? <span className="ctx-legend-tokens" title={`${exact.format(headline)} tok`}>{compact.format(headline)}{data.limit ? ` / ${compact.format(data.limit)}` : ""}</span> : null}
        {data ? <span className="ctx-request-source">{tr(data.calibrated_used != null ? "web.context.request_calibrated" : "web.context.request_estimated")}</span> : null}
        <label className="ctx-filter ctx-request-search">
          <MagnifyingGlass size={12} aria-hidden="true" />
          <input type="search" value={filter} placeholder={tr("web.context.search")} aria-label={tr("web.context.search")} onChange={(event) => setFilter(event.target.value)} />
        </label>
        {needle ? <span className="ctx-request-count" role="status">{tr("web.context.matches", { count: matchCount })}</span> : null}
        <button type="button" className="icon-button ctx-request-tool" disabled={!data}
          title={tr(allOpen ? "web.context.collapse_all" : "web.context.expand_all")} aria-label={tr(allOpen ? "web.context.collapse_all" : "web.context.expand_all")}
          onClick={() => setExpanded(allOpen ? new Set() : new Set(sections.map((section) => section.key)))}>
          {allOpen ? <ArrowsInLineVertical size={14} aria-hidden="true" /> : <ArrowsOutLineVertical size={14} aria-hidden="true" />}
        </button>
        <button type="button" className={`icon-button ctx-request-tool${copied ? ` ${copied}` : ""}`} disabled={!data} onClick={() => void copy()}
          title={tr(copied === "ok" ? "web.context.copied" : copied === "failed" ? "web.context.copy_failed" : "web.context.copy_json")}
          aria-label={tr(copied === "ok" ? "web.context.copied" : copied === "failed" ? "web.context.copy_failed" : "web.context.copy_json")}>
          {copied === "ok" ? <Check size={14} aria-hidden="true" /> : copied === "failed" ? <Warning size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
        </button>
        <button type="button" className="icon-button" aria-label={tr("web.context.close")} title={tr("web.context.close")} onClick={onClose}><X size={14} aria-hidden="true" /></button>
      </header>
      <div className="ctx-request-body">
        <nav className="ctx-request-toc" aria-label={tr("web.context.contents")}>
          <ol>
            {shown.map((section) => <li key={section.key}>
              <button type="button" onClick={() => jump(section.key)} title={`${exact.format(section.tokens)} tok`}>
                <span className={`ctx-dot ctx-c-${section.category ?? "turn"}`} aria-hidden="true" />
                <span className="ctx-request-toc-label">{section.title}</span>
                <span className="ctx-legend-tokens">{compact.format(section.tokens)}</span>
                <Shade value={section.tokens} max={heaviest} />
              </button>
            </li>)}
          </ol>
        </nav>
        <div className="ctx-request-main" ref={main}>
          {!data && !failed ? <p className="hint" role="status">{tr("web.context.loading")}</p> : null}
          {failed ? <p className="hint">{tr("web.context.detail_unavailable")}</p> : null}
          {data && shown.length === 0 ? <p className="empty-small">{tr("web.context.no_match")}</p> : null}
          {shown.map((section) => {
            // While searching, every section with a match is open.
            const isOpen = Boolean(needle) || expanded.has(section.key);
            return <section key={section.key} className={`ctx-request-section${isOpen ? " open" : ""}`} data-section={section.key}>
              <button type="button" className="ctx-group-head" aria-expanded={isOpen} onClick={() => toggleSection(section.key)}>
                <CaretRight size={11} className="ctx-caret" aria-hidden="true" />
                <span className={`ctx-dot ctx-c-${section.category ?? "turn"}`} aria-hidden="true" />
                <span className="ctx-request-section-title">{section.title}</span>
                <span className="ctx-count">{section.items.length}</span>
                <span className="ctx-legend-tokens">{compact.format(section.tokens)}</span>
              </button>
              {isOpen ? <ul className="ctx-detail-list">
                {section.items.map((item, index) => {
                  const id = itemId(item, index);
                  return <ItemRow key={id} item={item} anchor={id} open={open.has(id)} onToggle={() => toggleItem(id)} onConversation={jumpOf(item)} highlight={needle || undefined}
                    categoryLabel={section.turn != null && item.category ? label(item.category) : undefined} />;
                })}
              </ul> : null}
            </section>;
          })}
        </div>
      </div>
    </div>
  </div>, document.body);
}
