import { CaretDown } from "@phosphor-icons/react";
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { MenuPopover, rectOf, type Anchor } from "./Menu";

export type SelectOption = { value: string; label: string; hint?: string; icon?: ReactNode; disabled?: boolean };

/** Styled replacement for a native `<select>`: a trigger button plus the shared floating menu. */
export function Select({ value, options, onChange, ariaLabel, labelledBy, title, disabled = false, className = "", wide = false, placeholder }: {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  ariaLabel?: string;
  labelledBy?: string;
  title?: string;
  disabled?: boolean;
  className?: string;
  wide?: boolean;
  placeholder?: string;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const close = useRef((restoreFocus: boolean) => {
    setAnchor(null);
    if (restoreFocus) trigger.current?.focus();
  }).current;
  const current = options.find((option) => option.value === value);
  const open = () => { if (trigger.current) setAnchor(rectOf(trigger.current)); };
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); open(); }
  };
  return <>
    <button
      ref={trigger}
      type="button"
      className={`select-trigger${className ? ` ${className}` : ""}`}
      aria-label={labelledBy ? undefined : ariaLabel}
      aria-labelledby={labelledBy}
      aria-haspopup="menu"
      aria-expanded={anchor !== null}
      title={title}
      disabled={disabled}
      onKeyDown={onKeyDown}
      onClick={(event) => {
        event.stopPropagation();
        if (anchor) setAnchor(null); else open();
      }}
    >
      {current?.icon && <span className="select-icon ic" aria-hidden="true">{current.icon}</span>}
      <span className="select-value">{current?.label ?? placeholder ?? value}</span>
      <CaretDown className="select-caret" size={10} weight="bold" aria-hidden="true" />
    </button>
    {anchor && <MenuPopover
      anchor={anchor}
      align="start"
      matchWidth
      wide={wide}
      className="select-popover"
      label={ariaLabel}
      onClose={close}
      items={options.map((option) => ({
        label: option.label,
        hint: option.hint,
        icon: option.icon,
        disabled: option.disabled,
        checked: option.value === value,
        onSelect: () => { if (option.value !== value) onChange(option.value); },
      }))}
    />}
  </>;
}
