import { Check } from "@phosphor-icons/react";
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

export type MenuEntry = {
  label: string;
  onSelect: () => void;
  icon?: ReactNode;
  hint?: string;
  danger?: boolean;
  disabled?: boolean;
  /** Set for single-choice menus; renders a radio item. */
  checked?: boolean;
} | "separator";

type Anchor = { left: number; right: number; top: number; bottom: number };

const GAP = 4;
const EDGE = 8;

/** A small floating menu positioned next to an anchor rect or a pointer position. */
export function MenuPopover({ anchor, items, onClose, label, align = "end", wide = false }: {
  anchor: Anchor;
  items: MenuEntry[];
  onClose: (restoreFocus: boolean) => void;
  label?: string;
  align?: "start" | "end";
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const { width, height } = node.getBoundingClientRect();
    let left = align === "end" ? anchor.right - width : anchor.left;
    let top = anchor.bottom + GAP;
    if (top + height > window.innerHeight - EDGE) top = Math.max(EDGE, anchor.top - height - GAP);
    left = Math.min(Math.max(EDGE, left), Math.max(EDGE, window.innerWidth - width - EDGE));
    setPosition({ left, top });
  }, [anchor.left, anchor.right, anchor.top, anchor.bottom, align]);
  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const closeOutside = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) onClose(false);
    };
    const closeOnResize = () => onClose(false);
    window.addEventListener("pointerdown", closeOutside, true);
    window.addEventListener("resize", closeOnResize);
    return () => {
      window.removeEventListener("pointerdown", closeOutside, true);
      window.removeEventListener("resize", closeOnResize);
    };
  }, [onClose]);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const buttons = [...(ref.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const move = (next: number) => { event.preventDefault(); buttons[(next + buttons.length) % buttons.length]?.focus(); };
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(true); }
    else if (event.key === "ArrowDown") move(index + 1);
    else if (event.key === "ArrowUp") move(index - 1);
    else if (event.key === "Home") move(0);
    else if (event.key === "End") move(buttons.length - 1);
    else if (event.key === "Tab") onClose(false);
  };
  return createPortal(<div
    ref={ref}
    className={`menu-popover${wide ? " wide" : ""}`}
    role="menu"
    aria-label={label}
    style={position ? { left: position.left, top: position.top } : { left: anchor.left, top: anchor.bottom + GAP, visibility: "hidden" }}
    onKeyDown={onKeyDown}
    onClick={(event) => event.stopPropagation()}
    onContextMenu={(event) => event.preventDefault()}
  >
    {items.map((item, index) => item === "separator"
      ? <div className="menu-separator" role="separator" key={`separator-${index}`} />
      : <button
        type="button"
        key={item.label}
        role={item.checked === undefined ? "menuitem" : "menuitemradio"}
        aria-checked={item.checked === undefined ? undefined : item.checked}
        className={`menu-item${item.danger ? " danger" : ""}${item.hint ? " has-hint" : ""}`}
        disabled={item.disabled}
        onClick={() => { onClose(true); item.onSelect(); }}
      >
        {item.checked !== undefined
          ? <span className="menu-icon">{item.checked ? <Check size={12} weight="bold" /> : null}</span>
          : item.icon ? <span className="menu-icon">{item.icon}</span> : null}
        <span className="menu-copy"><span className="menu-label">{item.label}</span>{item.hint && <span className="menu-hint">{item.hint}</span>}</span>
      </button>)}
  </div>, document.body);
}

function rectOf(element: Element): Anchor {
  const { left, right, top, bottom } = element.getBoundingClientRect();
  return { left, right, top, bottom };
}

/** A trigger button that opens a {@link MenuPopover}. */
export function MenuButton({ label, items, children, className = "icon-button", align = "end", wide = false, title }: {
  label: string;
  items: MenuEntry[] | (() => MenuEntry[]);
  children: ReactNode;
  className?: string;
  align?: "start" | "end";
  wide?: boolean;
  title?: string;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const close = useRef((restoreFocus: boolean) => {
    setAnchor(null);
    if (restoreFocus) trigger.current?.focus();
  }).current;
  return <>
    <button
      ref={trigger}
      type="button"
      className={className}
      aria-label={label}
      title={title}
      aria-haspopup="menu"
      aria-expanded={anchor !== null}
      onClick={(event) => {
        event.stopPropagation();
        const rect = rectOf(event.currentTarget);
        setAnchor((current) => current ? null : rect);
      }}
    >{children}</button>
    {anchor && <MenuPopover anchor={anchor} items={typeof items === "function" ? items() : items} onClose={close} label={label} align={align} wide={wide} />}
  </>;
}

/** Right-click support: spread `onContextMenu` on the target and render `menu`. */
export function useContextMenu(items: () => MenuEntry[], label?: string) {
  const [point, setPoint] = useState<Anchor | null>(null);
  const close = useRef(() => setPoint(null)).current;
  return {
    onContextMenu: (event: MouseEvent) => {
      event.preventDefault();
      setPoint({ left: event.clientX, right: event.clientX, top: event.clientY, bottom: event.clientY });
    },
    menu: point ? <MenuPopover anchor={point} items={items()} onClose={close} label={label} align="start" /> : null,
  };
}
