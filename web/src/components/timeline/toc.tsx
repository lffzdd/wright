import { ListBullets, X } from "@phosphor-icons/react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import { useT } from "../../i18n";

/** Keep these in step with the conversation-stage and outline rules in styles/timeline/ and styles/layout.css. */
export const OUTLINE_DOCK_MIN_ITEMS = 4;
export const TRANSCRIPT_MAX_PX = 820;
export const TRANSCRIPT_MIN_PX = 560;

export type TocItem = {
  id: string;
  text: string;
};

export type TimelineTocRevision = {
  sessionId?: string;
  historyLength: number;
  activeContent?: string;
  activeTurnId?: string;
  timelineLength: number;
};

function tocIdSelector(id: string): string {
  const escaped =
    typeof CSS !== "undefined" && typeof CSS.escape === "function"
      ? CSS.escape(id)
      : id.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  return `[data-toc-id="${escaped}"]`;
}

function extractTocItems(scroller: HTMLElement): TocItem[] {
  const rows = scroller.querySelectorAll<HTMLElement>(".msg-row[data-toc-id]");
  const extracted: TocItem[] = [];
  rows.forEach((row) => {
    const id = row.getAttribute("data-toc-id");
    const text = (row.querySelector(".msg-body")?.textContent || "").replace(/\s+/g, " ").trim();
    if (id && text) extracted.push({ id, text });
  });
  return extracted;
}

function sameItems(a: TocItem[], b: TocItem[]): boolean {
  return (
    a.length === b.length &&
    a.every(
      (item, i) =>
        item.id === b[i].id && item.text === b[i].text
    )
  );
}

export function useTimelineTOC(
  scrollerRef: RefObject<HTMLDivElement | null>,
  revision: TimelineTocRevision
) {
  const [items, setItems] = useState<TocItem[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [tickTops, setTickTops] = useState<Record<string, number>>({});
  const [isOpen, setIsOpen] = useState(false);
  const isManualScrollingRef = useRef(false);
  const scrollLockTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rafRef = useRef<number | null>(null);
  const lastSessionIdRef = useRef<string | undefined>(revision.sessionId);

  useEffect(() => {
    if (lastSessionIdRef.current !== revision.sessionId) {
      lastSessionIdRef.current = revision.sessionId;
      setIsOpen(false);
      setActiveId(null);
    }
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const extracted = extractTocItems(scroller);
    setItems((prev) => (sameItems(prev, extracted) ? prev : extracted));
  }, [
    scrollerRef,
    revision.sessionId,
    revision.historyLength,
    revision.activeContent,
    revision.activeTurnId,
    revision.timelineLength,
  ]);

  const measureTickTops = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller || items.length < 2) {
      setTickTops({});
      return;
    }
    const scrollHeight = scroller.scrollHeight;
    if (scrollHeight <= 0) return;
    const next: Record<string, number> = {};
    items.forEach((item, index) => {
      const el = scroller.querySelector<HTMLElement>(tocIdSelector(item.id));
      if (el) {
        const scrollerTop = scroller.getBoundingClientRect().top;
        const anchor =
          el.getBoundingClientRect().top -
          scrollerTop +
          scroller.scrollTop +
          Math.min(el.offsetHeight, 56) * 0.35;
        next[item.id] = Math.min(96, Math.max(4, (anchor / scrollHeight) * 100));
      } else {
        next[item.id] = items.length === 1 ? 50 : 4 + (index / (items.length - 1)) * 92;
      }
    });
    setTickTops(next);
  }, [items, scrollerRef]);

  useLayoutEffect(() => {
    measureTickTops();
    const scroller = scrollerRef.current;
    if (!scroller || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measureTickTops());
    observer.observe(scroller);
    return () => observer.disconnect();
  }, [measureTickTops, scrollerRef, revision.historyLength, revision.activeContent, revision.timelineLength]);

  useEffect(() => {
    if (items.length < 2 && isOpen) setIsOpen(false);
  }, [items.length, isOpen]);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller || items.length < 2) {
      setActiveId(null);
      return;
    }

    const updateActive = () => {
      if (isManualScrollingRef.current) return;
      const headings = scroller.querySelectorAll<HTMLElement>("[data-toc-id]");
      if (!headings.length) return;

      const scrollerRect = scroller.getBoundingClientRect();
      const nearBottom =
        scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48;
      if (nearBottom) {
        const lastId = headings[headings.length - 1].getAttribute("data-toc-id");
        if (lastId) {
          setActiveId(lastId);
          return;
        }
      }

      let current: string | null = null;
      headings.forEach((heading) => {
        const relativeTop = heading.getBoundingClientRect().top - scrollerRect.top;
        if (relativeTop <= 48) {
          current = heading.getAttribute("data-toc-id");
        }
      });
      setActiveId(current ?? headings[0].getAttribute("data-toc-id"));
    };

    const onScroll = () => {
      if (rafRef.current !== null) return;
      rafRef.current = window.requestAnimationFrame(() => {
        rafRef.current = null;
        updateActive();
        measureTickTops();
      });
    };

    scroller.addEventListener("scroll", onScroll, { passive: true });
    updateActive();
    measureTickTops();

    return () => {
      scroller.removeEventListener("scroll", onScroll);
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [items, measureTickTops, scrollerRef]);

  const scrollToHeading = useCallback(
    (id: string) => {
      const scroller = scrollerRef.current;
      if (!scroller) return;
      const target = scroller.querySelector<HTMLElement>(tocIdSelector(id));
      if (!target) return;

      setActiveId(id);
      scroller.dispatchEvent(new Event("tocjump"));
      isManualScrollingRef.current = true;
      if (scrollLockTimerRef.current) clearTimeout(scrollLockTimerRef.current);
      scrollLockTimerRef.current = setTimeout(() => {
        isManualScrollingRef.current = false;
      }, 800);

      const scrollerRect = scroller.getBoundingClientRect();
      const targetRect = target.getBoundingClientRect();
      const targetTop = targetRect.top - scrollerRect.top + scroller.scrollTop - 44;
      const clampedTop = Math.max(0, targetTop);

      if (typeof scroller.scrollTo === "function") {
        scroller.scrollTo({ top: clampedTop, behavior: "smooth" });
      } else {
        scroller.scrollTop = clampedTop;
      }
    },
    [scrollerRef]
  );

  useEffect(() => {
    return () => {
      if (scrollLockTimerRef.current) clearTimeout(scrollLockTimerRef.current);
    };
  }, []);

  return {
    items,
    activeId,
    tickTops,
    isOpen,
    setIsOpen,
    scrollToHeading,
  };
}

export type TimelineTOCProps = {
  items: TocItem[];
  activeId: string | null;
  isOpen: boolean;
  setIsOpen: Dispatch<SetStateAction<boolean>>;
  scrollToHeading: (id: string) => void;
};

export function TimelineTOC({
  items,
  activeId,
  isOpen,
  setIsOpen,
  scrollToHeading,
}: TimelineTOCProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const tr = useT();

  useEffect(() => {
    if (!isOpen || !activeId || !listRef.current) return;
    const activeEl = listRef.current.querySelector<HTMLElement>(".timeline-toc-item.active");
    const nav = activeEl?.closest(".timeline-toc-nav");
    if (!activeEl || !(nav instanceof HTMLElement)) return;
    const navRect = nav.getBoundingClientRect();
    const itemRect = activeEl.getBoundingClientRect();
    if (itemRect.top < navRect.top) nav.scrollTop -= navRect.top - itemRect.top;
    else if (itemRect.bottom > navRect.bottom) nav.scrollTop += itemRect.bottom - navRect.bottom;
  }, [isOpen, activeId]);

  useEffect(() => {
    if (!isOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setIsOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [isOpen, setIsOpen]);

  if (items.length < 2) return null;

  return (
    <div className="timeline-toc-anchor" ref={containerRef}>
      <button
        type="button"
        className={`timeline-toc-trigger ${isOpen ? "open" : ""}`}
        aria-expanded={isOpen}
        aria-label={tr("web.toc")}
        title={tr("web.toc")}
        onClick={() => setIsOpen((prev) => !prev)}
      >
        <ListBullets size={13} weight="bold" />
        <span className="timeline-toc-trigger-label">{tr("web.toc")}</span>
        <span className="timeline-toc-trigger-count">{items.length}</span>
      </button>

      {isOpen && (
        <div className="timeline-toc-panel" role="dialog" aria-label={tr("web.toc")}>
          <div className="timeline-toc-header">
            <span className="timeline-toc-title">{tr("web.toc")}</span>
            <span className="timeline-toc-count-pill">{items.length}</span>
            <button
              type="button"
              className="timeline-toc-close"
              aria-label={tr("web.toc_close")}
              onClick={() => setIsOpen(false)}
            >
              <X size={12} weight="bold" />
            </button>
          </div>
          <nav className="timeline-toc-nav" aria-label={tr("web.toc")}>
            <ul className="timeline-toc-list" ref={listRef}>
              {items.map((item) => {
                const isActive = activeId === item.id;
                return (
                  <li key={item.id} className="timeline-toc-list-item">
                    <button
                      type="button"
                      className={`timeline-toc-item ${isActive ? "active" : ""}`}
                      aria-current={isActive ? "true" : undefined}
                      title={item.text}
                      onClick={() => {
                        scrollToHeading(item.id);
                        setIsOpen(false);
                      }}
                    >
                      <span className="timeline-toc-item-text">{item.text}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </nav>
        </div>
      )}
    </div>
  );
}

function useOutlineActiveScroll(activeId: string | null, listRef: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const list = listRef.current;
    if (!activeId || !list) return;
    const activeEl = list.querySelector<HTMLElement>("[aria-current='true']");
    if (!activeEl) return;
    const listRect = list.getBoundingClientRect();
    const itemRect = activeEl.getBoundingClientRect();
    if (itemRect.top < listRect.top) list.scrollTop -= listRect.top - itemRect.top;
    else if (itemRect.bottom > listRect.bottom) list.scrollTop += itemRect.bottom - listRect.bottom;
  }, [activeId, listRef]);
}

/** Hovering the rail waits this long before the list opens, so passing over it does nothing. */
const OUTLINE_OPEN_DELAY_MS = 150;
/** Leaving waits a moment, so the pointer can cross the gap between rail and list. */
const OUTLINE_CLOSE_DELAY_MS = 250;

/**
 * The tick rail beside the conversation, and the list of prompts it opens.
 * The list floats over the conversation instead of taking a column, so opening
 * it never reflows the transcript. Hovering opens it; the button pins it.
 */
export function ConversationOutline({
  items,
  activeId,
  tickTops,
  scrollToHeading,
  pinned,
  onPin,
}: {
  items: TocItem[];
  activeId: string | null;
  tickTops: Record<string, number>;
  scrollToHeading: (id: string) => void;
  pinned: boolean;
  onPin: (pinned: boolean) => void;
}) {
  const tr = useT();
  const dockRef = useRef<HTMLUListElement>(null);
  const tickRef = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState(false);
  // The tick under the pointer, marked in the open list so the two read as one.
  const [hot, setHot] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const open = pinned || hovered;
  useOutlineActiveScroll(open ? activeId : null, dockRef);
  useOutlineActiveScroll(activeId, tickRef);
  const later = (next: boolean, delay: number) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setHovered(next), delay);
  };
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  useEffect(() => {
    if (!pinned) return;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onPin(false); };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [pinned, onPin]);
  if (items.length < 2) return null;

  const close = () => {
    if (timer.current) clearTimeout(timer.current);
    setHovered(false);
    onPin(false);
  };

  return (
    <div
      className={`conversation-outline-host${open ? " open" : ""}${pinned ? " pinned" : ""}`}
      onMouseEnter={() => later(true, OUTLINE_OPEN_DELAY_MS)}
      onMouseLeave={() => later(false, OUTLINE_CLOSE_DELAY_MS)}
    >
      <aside className="outline-dock" aria-label={tr("web.toc")} aria-hidden={!open || undefined} inert={!open}>
        <div className="outline-dock-head">
          <span className="outline-dock-title">{tr("web.toc_kicker")}</span>
          <span className="outline-dock-meta">{items.length}</span>
          <button type="button" className="timeline-toc-close" aria-label={tr("web.toc_close")} onClick={close}>
            <X size={12} weight="bold" />
          </button>
        </div>
        <ul className="outline-dock-list" ref={dockRef}>
          {items.map((item) => {
            const isActive = activeId === item.id;
            return (
              <li key={item.id}>
                <button
                  type="button"
                  className={`outline-dock-item${isActive ? " active" : ""}${hot === item.id ? " hot" : ""}`}
                  aria-current={isActive ? "true" : undefined}
                  title={item.text}
                  onClick={() => {
                    scrollToHeading(item.id);
                    // Opened by hover, the list gets out of the way once used; pinned, it stays.
                    if (!pinned) close();
                  }}
                >
                  <span className="outline-dock-mark" aria-hidden="true">
                    <i />
                  </span>
                  <span className="outline-dock-text">{item.text}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </aside>
      <div className="outline-ticks" aria-label={tr("web.toc")} ref={tickRef}>
        <button type="button" className="outline-ticks-expand" aria-label={tr("web.toc_expand")} aria-pressed={pinned} onClick={() => onPin(!pinned)}>
          <ListBullets size={13} weight="bold" />
        </button>
        <div className="outline-ticks-track">
          {items.map((item, index) => {
            const isActive = activeId === item.id;
            const fallbackTop = items.length === 1 ? 50 : 4 + (index / (items.length - 1)) * 92;
            const top = tickTops[item.id] ?? fallbackTop;
            return (
              <button
                key={item.id}
                type="button"
                className={`outline-tick ${isActive ? "active" : ""}`}
                style={{ top: `${top}%` }}
                aria-current={isActive ? "true" : undefined}
                aria-label={tr("web.toc_tick", { n: index + 1, text: item.text })}
                onClick={() => scrollToHeading(item.id)}
                onMouseEnter={() => setHot(item.id)}
                onMouseLeave={() => setHot(null)}
              >
                <i aria-hidden="true" />
                <span className="outline-tick-preview" role="tooltip">{tr("web.toc_tick", { n: index + 1, text: item.text })}</span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
