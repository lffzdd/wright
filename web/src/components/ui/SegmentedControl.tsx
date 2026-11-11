/** Two-or-more-way switch styled like inspector permission mode tabs. */
export function SegmentedControl<T extends string>({ value, options, onChange, disabled = false, ariaLabel, describedBy, className = "" }: {
  value: T;
  options: Array<{ value: T; label: string; title?: string; ariaLabel?: string }>;
  onChange: (value: T) => void;
  disabled?: boolean;
  ariaLabel: string;
  describedBy?: string;
  className?: string;
}) {
  return <div className={`segmented-control${className ? ` ${className}` : ""}`.trim()} role="radiogroup"
    aria-label={ariaLabel} aria-describedby={describedBy}>
    {options.map((item) => <button key={item.value} type="button" role="radio" aria-checked={value === item.value}
      className={`segmented-option ${value === item.value ? "active" : ""}`.trim()} disabled={disabled}
      title={item.title} aria-label={item.ariaLabel ?? item.label} onClick={() => onChange(item.value)}>
      {item.label}
    </button>)}
  </div>;
}
