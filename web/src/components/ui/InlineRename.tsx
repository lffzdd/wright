import { useEffect, useRef, useState } from "react";

/** Edit a title in place: Enter or blur saves, Escape cancels. */
export function InlineRename({ value, label, onSubmit, onCancel, className = "" }: {
  value: string;
  label: string;
  onSubmit: (value: string) => void;
  onCancel: () => void;
  className?: string;
}) {
  const [draft, setDraft] = useState(value);
  const input = useRef<HTMLInputElement>(null);
  const done = useRef(false);
  useEffect(() => { input.current?.focus(); input.current?.select(); }, []);
  const finish = (save: boolean) => {
    if (done.current) return;
    done.current = true;
    const next = draft.trim();
    if (save && next && next !== value) onSubmit(next);
    else onCancel();
  };
  return <input
    ref={input}
    className={`inline-rename ${className}`}
    aria-label={label}
    value={draft}
    onChange={(event) => setDraft(event.target.value)}
    onClick={(event) => event.stopPropagation()}
    onBlur={() => finish(true)}
    onKeyDown={(event) => {
      if (event.nativeEvent.isComposing || event.keyCode === 229) return;
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); finish(false); }
    }}
  />;
}
