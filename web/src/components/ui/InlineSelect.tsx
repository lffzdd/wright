import { useId } from "react";
import { Select } from "./Select";

/** Labelled form dropdown built on {@link Select}. */
export function InlineSelect({ label, ariaLabel, value, options, onChange, disabled = false, className = "" }: {
  label?: string;
  ariaLabel?: string;
  value: string;
  options: Array<{ value: string; label: string; hint?: string }>;
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
}) {
  const labelId = useId();
  return <div className={`grant-form-select${className ? ` ${className}` : ""}`}>
    {label ? <span className="grant-form-select-label" id={labelId}>{label}</span> : null}
    <Select className="grant-form-select-trigger" value={value} options={options} onChange={onChange}
      disabled={disabled} ariaLabel={ariaLabel ?? label} labelledBy={label ? labelId : undefined} />
  </div>;
}
