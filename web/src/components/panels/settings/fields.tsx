import { useEffect, useState, type ReactNode } from "react";
import { useT } from "../../../i18n";
import type { RuntimeSetting, SettingScope } from "../../../types";
import { Select } from "../../ui/Select";
import { Switch, Tag } from "../capability";
import { display, fileValue } from "./useRuntimeSettings";

export type FieldProps = {
  item: RuntimeSetting;
  scope: SettingScope;
  save: (key: string, value: unknown) => Promise<void>;
  showKeys: boolean;
};

/** The label of a key, falling back to the server's description for the hint. */
export function useSettingText(item: RuntimeSetting): { label: string; hint: string } {
  const tr = useT();
  const hintKey = `web.setting.${item.key}.hint`;
  return {
    label: tr(`web.setting.${item.key}`),
    hint: tr(hintKey) === hintKey ? item.description : tr(hintKey),
  };
}

/**
 * One setting: label, hint, and only the status worth reading. The source tag
 * shows when the value is not the default; the raw key only when asked for.
 */
export function SettingRow({ item, scope, save, showKeys, control, stacked = false, children }: FieldProps & {
  /** Replaces the default control for this kind. */
  control?: (props: { label: string; stored: unknown; disabled: boolean; apply: (value: unknown) => void }) => ReactNode;
  /** Full-width control below the text instead of beside it. */
  stacked?: boolean;
  children?: ReactNode;
}) {
  const tr = useT();
  const { label, hint } = useSettingText(item);
  const editable = item.scopes.includes(scope);
  const stored = fileValue(item, scope);
  const isSet = stored !== null && stored !== undefined;
  const apply = (value: unknown) => { save(item.key, value).catch(() => undefined); };
  const fromElsewhere = item.source !== scope && item.source !== "default" && item.source !== "env";
  const body = control
    ? control({ label, stored, disabled: !editable, apply })
    : <SettingControl item={item} label={label} stored={stored} disabled={!editable} apply={apply} />;
  const reset = editable && isSet
    ? <button type="button" className="text-button" onClick={() => apply(null)}>{tr("web.runtime.reset")}</button>
    : null;
  const status = <>
    {(item.source !== "default" || showKeys || !editable) && <div className="settings-status">
      {showKeys && <code className="runtime-key">{item.key}</code>}
      {item.source !== "default" && <Tag tone={item.source === "env" ? "warning" : "accent"}>{tr(`web.runtime.source.${item.source}`)}</Tag>}
      {fromElsewhere && <span className="hint">{tr("web.runtime.in_use", { value: display(item.value) || tr("web.runtime.empty") })}</span>}
      {!editable && <span className="hint">{tr("web.runtime.user_only")}</span>}
    </div>}
    {item.env_override && <p className="settings-env-note">{tr("web.runtime.env_note", { name: item.env.join(" / ") })}</p>}
  </>;
  return <div className={`settings-row runtime-row${stacked ? " stacked" : ""}`} data-setting={item.key}>
    <div className="runtime-meta">
      <span className="settings-label">{label}</span>
      {hint && <p className="hint">{hint}</p>}
      {status}
    </div>
    <div className="runtime-control">
      {body}
      {reset ?? <span aria-hidden="true" />}
    </div>
    {children}
  </div>;
}

function SettingControl({ item, label, stored, disabled, apply }: {
  item: RuntimeSetting;
  label: string;
  stored: unknown;
  disabled: boolean;
  apply: (value: unknown) => void;
}) {
  const tr = useT();
  if (item.kind === "bool") {
    const checked = typeof stored === "boolean" ? stored : Boolean(item.value);
    return <Switch label={label} checked={checked} disabled={disabled} onChange={apply} />;
  }
  if (item.kind === "choice") {
    return <Select className="field-select" ariaLabel={label} disabled={disabled} value={typeof stored === "string" ? stored : ""}
      onChange={(next) => apply(next || null)}
      options={[
        { value: "", label: tr("web.runtime.default_option", { value: display(item.default) }) },
        ...item.choices.map((choice) => {
          const key = `web.setting.choice.${item.key}.${choice}`;
          return { value: choice, label: tr(key) === key ? choice : tr(key) };
        }),
      ]} />;
  }
  return <TextSetting item={item} label={label} stored={stored} disabled={disabled} apply={apply} />;
}

export function TextSetting({ item, label, stored, disabled, apply }: {
  item: RuntimeSetting;
  label: string;
  stored: unknown;
  disabled: boolean;
  apply: (value: unknown) => void;
}) {
  const tr = useT();
  const saved = display(stored);
  const [draft, setDraft] = useState(saved);
  useEffect(() => { setDraft(saved); }, [saved]);
  const commit = () => {
    const text = draft.trim();
    if (text === saved) return;
    if (!text) { apply(null); return; }
    apply(item.kind === "int" && /^-?\d+$/.test(text) ? Number(text) : text);
  };
  const placeholder = display(item.value) || display(item.default) || tr("web.runtime.empty");
  return <input aria-label={label}
    inputMode={item.kind === "int" ? "numeric" : undefined}
    value={draft} placeholder={placeholder} disabled={disabled} spellCheck={false}
    className={item.kind === "path" ? "mono-input" : undefined}
    onChange={(event) => setDraft(event.target.value)}
    onBlur={commit}
    onKeyDown={(event) => {
      if (event.key === "Enter") { event.preventDefault(); commit(); }
      if (event.key === "Escape") { event.preventDefault(); setDraft(saved); }
    }} />;
}

/** Format a token count: 1M, 1.05M, 200K. */
export function formatTokens(tokens: number | null | undefined): string {
  if (!tokens) return "";
  if (tokens >= 1_000_000) return `${Number((tokens / 1_000_000).toFixed(2))}M`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}K`;
  return String(tokens);
}

/** USD per million tokens, without trailing zeros. */
export function formatUsd(value: number): string {
  return `$${Number(value.toFixed(4))}`;
}
