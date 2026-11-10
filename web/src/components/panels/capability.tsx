import { DotsThree } from "@phosphor-icons/react";
import { useEffect, useRef, type ReactNode } from "react";
import { useT } from "../../i18n";
import { MenuButton, type MenuEntry } from "../ui/Menu";

/** A capability screen shown in place of the conversation. */
export function CapabilityPage({ id, title, subtitle, actions, notice, children }: {
  id: string;
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  notice?: ReactNode;
  children: ReactNode;
}) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => { ref.current?.focus({ preventScroll: true }); }, [id]);
  return <section ref={ref} tabIndex={-1} className="cap-page" aria-labelledby={id}>
    <header className="cap-head">
      <div className="cap-head-inner">
        <div className="cap-title">
          <h1 id={id}>{title}</h1>
          {subtitle && <p className="hint">{subtitle}</p>}
        </div>
        {actions && <div className="cap-head-actions">{actions}</div>}
      </div>
    </header>
    <div className="cap-scroll">
      <div className="cap-body">
        {notice}
        {children}
      </div>
    </div>
  </section>;
}

export function EmptyState({ icon, title, children, action }: { icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return <div className="cap-empty">
    {icon && <span className="cap-empty-icon" aria-hidden="true">{icon}</span>}
    <p className="cap-empty-title">{title}</p>
    {children && <p className="hint">{children}</p>}
    {action}
  </div>;
}

export function Tag({ tone = "neutral", children }: { tone?: "neutral" | "ok" | "warning" | "danger" | "accent"; children: ReactNode }) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}

/** One list entry: a title, an optional preview, tags, and a "more" menu. */
export function Row({ title, preview, meta, side, menu, children }: {
  title: string;
  preview?: string;
  meta?: ReactNode;
  side?: ReactNode;
  menu?: MenuEntry[];
  children?: ReactNode;
}) {
  const tr = useT();
  return <li className="cap-row">
    <div className="cap-row-head">
      <div className="cap-row-main">
        <span className="cap-row-title">{title}</span>
        {preview && <p className="cap-row-preview">{preview}</p>}
        {meta && <div className="cap-row-meta">{meta}</div>}
      </div>
      {(side || menu) && <div className="cap-row-side">
        {side}
        {menu && <MenuButton label={tr("web.panel.actions_for", { name: title })} items={menu}><DotsThree size={16} weight="bold" /></MenuButton>}
      </div>}
    </div>
    {children}
  </li>;
}

export function Segmented<T extends string>({ label, value, options, onChange }: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
}) {
  return <div className="segmented" role="radiogroup" aria-label={label}>
    {options.map((option) => <button key={option.value} type="button" role="radio" aria-checked={option.value === value} onClick={() => { if (option.value !== value) onChange(option.value); }}>{option.label}</button>)}
  </div>;
}

export function Switch({ label, checked, disabled, onChange }: { label: string; checked: boolean; disabled?: boolean; onChange: (next: boolean) => void }) {
  return <button type="button" className="switch" role="switch" aria-label={label} title={label} aria-checked={checked} disabled={disabled} onClick={() => onChange(!checked)}><span /></button>;
}

export function Tabs<T extends string>({ label, value, tabs, onChange }: {
  label: string;
  value: T;
  tabs: Array<{ value: T; label: string; count?: number }>;
  onChange: (value: T) => void;
}) {
  return <div className="cap-tabs" role="tablist" aria-label={label}>
    {tabs.map((tab) => <button key={tab.value} type="button" role="tab" aria-selected={tab.value === value} onClick={() => onChange(tab.value)}>
      {tab.label}{tab.count !== undefined && <span className="cap-tab-count">{tab.count}</span>}
    </button>)}
  </div>;
}
