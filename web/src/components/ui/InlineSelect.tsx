import { useEffect, useId, useRef, useState } from "react";

/** Inspector-style dropdown: styled trigger and in-app menu (not a native `<select>`). */
export function InlineSelect({ label, ariaLabel, value, options, onChange, disabled = false, menuUp = false, className = "" }: {
  label?: string;
  ariaLabel?: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
  disabled?: boolean;
  menuUp?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const current = options.find((option) => option.value === value)?.label ?? value;
  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const labelledBy = label ? listId : undefined;
  return <div className={`grant-form-select${menuUp ? " grant-form-select-up" : ""}${className ? ` ${className}` : ""}`.trim()} ref={rootRef}>
    {label ? <span className="grant-form-select-label" id={listId}>{label}</span> : null}
    <button type="button" className="grant-form-select-trigger" disabled={disabled}
      aria-label={labelledBy ? undefined : ariaLabel}
      aria-labelledby={labelledBy}
      aria-haspopup="listbox" aria-expanded={open}
      onClick={() => setOpen((currentOpen) => !currentOpen)}>
      <span className="grant-form-select-value">{current}</span>
    </button>
    {open ? <ul className="grant-form-select-menu" role="listbox" aria-label={ariaLabel ?? label}>
      {options.map((option) => <li key={option.value}>
        <button type="button" role="option" aria-selected={value === option.value}
          className={value === option.value ? "active" : ""}
          onClick={() => { onChange(option.value); setOpen(false); }}>
          {option.label}
        </button>
      </li>)}
    </ul> : null}
  </div>;
}
