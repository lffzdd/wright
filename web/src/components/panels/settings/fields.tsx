import { DotsThree, LockSimple } from "@phosphor-icons/react";
import { useEffect, useState, type ReactNode } from "react";
import { useT } from "../../../i18n";
import type { RuntimeSetting, SettingScope } from "../../../types";
import { MenuButton, type MenuEntry } from "../../ui/Menu";
import { Select } from "../../ui/Select";
import { Switch, Tag } from "../capability";
import { display, fileValue } from "./useRuntimeSettings";

/** What every row needs from the page. */
export type FieldContext = {
  save: (key: string, value: unknown, scope: SettingScope) => Promise<void>;
  showKeys: boolean;
  /** A project is open, so project-safe keys can be set for it alone. */
  hasProject: boolean;
};

export type ControlProps = { label: string; stored: unknown; disabled: boolean; apply: (value: unknown) => void };

/** The label of a key, falling back to the server's description for the hint. */
export function useSettingText(item: RuntimeSetting): { label: string; hint: string } {
  const tr = useT();
  const hintKey = `web.setting.${item.key}.hint`;
  return {
    label: tr(`web.setting.${item.key}`),
    hint: tr(hintKey) === hintKey ? item.description : tr(hintKey),
  };
}

/** The file a row starts editing: the project's when it already sets the key. */
function initialScope(item: RuntimeSetting, hasProject: boolean): SettingScope {
  const projectSet = item.project_value !== null && item.project_value !== undefined;
  return hasProject && item.scopes.includes("project") && projectSet ? "project" : "user";
}

/**
 * One setting: label, hint, control, and a menu for the less common moves
 * (set it for this project only, go back to the user value, reset). A value
 * from the environment locks the row; the page header lists every such key.
 */
export function SettingRow({ item, ctx, control, stacked = false, problem, confirm, children }: {
  item: RuntimeSetting;
  ctx: FieldContext;
  /** Replaces the default control for this kind. */
  control?: (props: ControlProps) => ReactNode;
  /** Full-width control below the text instead of beside it. */
  stacked?: boolean;
  /** A blocking problem with the effective value, such as a model no session can reach. */
  problem?: string;
  /** Asked before a value is written; false keeps the old one. */
  confirm?: (value: unknown) => Promise<boolean>;
  children?: ReactNode;
}) {
  const tr = useT();
  const { label, hint } = useSettingText(item);
  const [scope, setScope] = useState<SettingScope>(() => initialScope(item, ctx.hasProject));
  const projectSafe = ctx.hasProject && item.scopes.includes("project");
  const locked = item.env_override;
  const editable = item.scopes.includes(scope) && !locked;
  const stored = fileValue(item, scope);
  const isSet = stored !== null && stored !== undefined;
  const write = (value: unknown, target: SettingScope = scope) => {
    ctx.save(item.key, value, target).catch(() => undefined);
  };
  const apply = (value: unknown) => {
    if (!confirm || value === null) { write(value); return; }
    confirm(value).then((ok) => { if (ok) write(value); }).catch(() => undefined);
  };
  const body = control
    ? control({ label, stored, disabled: !editable, apply })
    : <SettingControl item={item} label={label} stored={stored} disabled={!editable} apply={apply} />;
  const projectSet = item.project_value !== null && item.project_value !== undefined;
  const menu = rowMenu({ tr, projectSafe, scope, isSet: isSet && editable, projectSet, locked, setScope, write });
  // Editing one file while the other decides: say what is in effect.
  const elsewhere = !locked && item.source !== scope && item.source !== "default";
  return <div className={`settings-row runtime-row${stacked ? " stacked" : ""}`} data-setting={item.key}>
    <div className="runtime-meta">
      <span className="settings-label">
        {label}
        {scope === "project" && <Tag tone="accent">{tr("web.settings.row.this_project")}</Tag>}
      </span>
      {hint && <p className="hint">{hint}</p>}
      {(ctx.showKeys || elsewhere) && <div className="settings-status">
        {ctx.showKeys && <code className="runtime-key">{item.key}</code>}
        {elsewhere && <span className="hint">{tr(`web.settings.row.in_use_${item.source}`, { value: display(item.value) || tr("web.runtime.empty") })}</span>}
      </div>}
      {problem && <p className="settings-problem" role="note">{tr("web.runtime.problem", { message: problem })}</p>}
    </div>
    <div className="runtime-control">
      {body}
      <div className="settings-row-tools">
        {locked && <span className="settings-lock" role="img" aria-label={tr("web.settings.locked_by", { name: item.env.join(" / ") })}
          title={tr("web.settings.locked_by", { name: item.env.join(" / ") })}><LockSimple size={14} /></span>}
        {menu.length > 0 && <MenuButton label={tr("web.settings.row.more", { name: label })} items={menu}>
          <DotsThree size={16} weight="bold" />
        </MenuButton>}
      </div>
    </div>
    {children}
  </div>;
}

function rowMenu({ tr, projectSafe, scope, isSet, projectSet, locked, setScope, write }: {
  tr: ReturnType<typeof useT>;
  projectSafe: boolean;
  scope: SettingScope;
  isSet: boolean;
  projectSet: boolean;
  locked: boolean;
  setScope: (scope: SettingScope) => void;
  write: (value: unknown, target?: SettingScope) => void;
}): MenuEntry[] {
  if (locked) return [];
  const items: MenuEntry[] = [];
  if (projectSafe && scope === "user") {
    items.push({ label: tr("web.settings.row.project_only"), hint: tr("web.settings.row.project_only_hint"), onSelect: () => setScope("project") });
  }
  if (projectSafe && scope === "project") {
    items.push({
      label: tr("web.settings.row.use_user"),
      onSelect: () => { if (projectSet) write(null, "project"); setScope("user"); },
    });
  }
  if (isSet) items.push({ label: tr("web.runtime.reset"), onSelect: () => write(null) });
  return items;
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
        { value: "", label: tr("web.runtime.default_option", { value: choiceLabel(tr, item.key, display(item.default)) }) },
        ...item.choices.map((choice) => ({ value: choice, label: choiceLabel(tr, item.key, choice) })),
      ]} />;
  }
  return <TextSetting item={item} label={label} stored={stored} disabled={disabled} apply={apply} />;
}

export function choiceLabel(tr: ReturnType<typeof useT>, key: string, choice: string): string {
  const name = `web.setting.choice.${key}.${choice}`;
  return tr(name) === name ? choice : tr(name);
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
