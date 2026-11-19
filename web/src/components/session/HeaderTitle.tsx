import { useLayoutEffect, useRef } from "react";

/** The session title in the top bar; click or Enter edits it in place. */
export function HeaderTitle({ value, hint, onCommit, onBegin, editRef }: {
  value: string;
  hint: string;
  onCommit: (next: string) => void;
  onBegin?: () => void;
  editRef?: { current: (() => void) | null };
}) {
  const node = useRef<HTMLSpanElement>(null);
  const editing = useRef(false);
  const armed = useRef(false);
  const valueRef = useRef(value);
  valueRef.current = value;
  useLayoutEffect(() => {
    const el = node.current;
    if (!el || editing.current) return;
    if (el.textContent !== value) el.textContent = value;
  }, [value]);
  const begin = () => {
    const el = node.current;
    if (!el || el.contentEditable === "true") return;
    onBegin?.();
    editing.current = true;
    armed.current = false;
    el.setAttribute("contenteditable", "true");
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    window.setTimeout(() => { armed.current = true; }, 0);
  };
  const finish = (save: boolean) => {
    const el = node.current;
    if (!el || el.contentEditable !== "true") return;
    const text = el.textContent?.trim() ?? "";
    el.removeAttribute("contenteditable");
    editing.current = false;
    armed.current = false;
    if (save && text && text !== valueRef.current) onCommit(text);
    else el.textContent = valueRef.current;
  };
  if (editRef) editRef.current = begin;
  return <span
    ref={node}
    className="title"
    role="heading"
    aria-level={1}
    title={hint}
    tabIndex={0}
    spellCheck={false}
    onClick={begin}
    onKeyDown={(event) => {
      if (event.nativeEvent.isComposing || event.keyCode === 229) return;
      if (event.key === "Enter") { event.preventDefault(); if (editing.current) finish(true); else begin(); }
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); finish(false); }
    }}
    onBlur={() => { if (armed.current) finish(true); }}
  >{value}</span>;
}
