import { LockSimple, X } from "@phosphor-icons/react";
import { useState, type FormEvent } from "react";
import { useT } from "../../../i18n";
import type { PermissionRuleRow, SettingScope } from "../../../types";
import { confirmDialog } from "../../ui/dialogs";
import { Select } from "../../ui/Select";
import { Tag } from "../capability";
import { present, type Entry, type SectionContext } from "./entries";
import type { PermissionDefaultsState } from "./resources";

/** Values that loosen access ask first. */
const RISKY: Record<string, string> = {
  "sandbox.default_execution": "host",
  "mcp.trust_project": "true",
};

export function permissionsEntries(ctx: SectionContext): Entry[] {
  const { tr } = ctx;
  const confirmRisky = (key: string) => async (value: unknown) => {
    if (String(value) !== RISKY[key]) return true;
    return confirmDialog({ title: tr(`web.permissions.confirm.${key}`), message: tr(`web.permissions.confirm.${key}.hint`), danger: true });
  };
  const modeTerms = [tr("web.permissions.mode"), tr("web.permissions.mode_hint"), ...["default", "acceptEdits", "plan", "bypass"]
    .map((mode) => tr(`web.permissions.mode.${mode}`))].join(" ");
  const ruleTerms = [tr("web.permissions.rules"), tr("web.permissions.rules_hint"), "deny ask allow",
    ...(ctx.permissions.data?.rules ?? []).map((rule) => rule.text)].join(" ");
  return present([
    { id: "mode", group: "defaults", terms: modeTerms, node: <DefaultModeRow permissions={ctx.permissions} /> },
    ctx.row("sandbox.default_execution", { confirm: confirmRisky("sandbox.default_execution") }, "defaults"),
    ctx.row("sandbox.egress", {}, "defaults"),
    ctx.row("mcp.trust_project", { confirm: confirmRisky("mcp.trust_project") }, "defaults"),
    { id: "rules", group: "rules", terms: ruleTerms, node: <RulesCard key={ctx.projectId ?? ""} permissions={ctx.permissions} hasProject={Boolean(ctx.projectId)} /> },
  ]);
}

function DefaultModeRow({ permissions }: { permissions: PermissionDefaultsState }) {
  const tr = useT();
  const view = permissions.data;
  const change = async (mode: string) => {
    if (mode === "bypass") {
      const ok = await confirmDialog({ title: tr("web.permissions.confirm.bypass"), message: tr("web.permissions.confirm.bypass.hint"), danger: true });
      if (!ok) return;
    }
    await permissions.setMode(mode);
  };
  return <div className="settings-row runtime-row">
    <div className="runtime-meta">
      <span className="settings-label">{tr("web.permissions.mode")}</span>
      <p className="hint">{view ? tr(`web.permissions.mode.${view.mode}.hint`) : tr("web.permissions.mode_hint")}</p>
      {permissions.error && <p className="settings-problem" role="alert">{permissions.error}</p>}
    </div>
    <div className="runtime-control">
      {view && <Select className="field-select" ariaLabel={tr("web.permissions.mode")} value={view.mode}
        onChange={(mode) => { change(mode).catch(() => undefined); }}
        options={view.modes.map((mode) => ({ value: mode, label: tr(`web.permissions.mode.${mode}`), hint: tr(`web.permissions.mode.${mode}.hint`) }))} />}
      <span aria-hidden="true" />
    </div>
  </div>;
}

const EFFECTS: Array<PermissionRuleRow["effect"]> = ["deny", "ask", "allow"];

function RulesCard({ permissions, hasProject }: { permissions: PermissionDefaultsState; hasProject: boolean }) {
  const tr = useT();
  const view = permissions.data;
  if (!view) return null;
  const remove = async (rule: PermissionRuleRow) => {
    const ok = await confirmDialog({ title: tr("web.permissions.remove_confirm", { rule: rule.text }), message: tr("web.permissions.remove_hint") });
    if (ok) await permissions.removeRule(rule.scope, rule.id);
  };
  return <div className="permission-rules">
    {EFFECTS.map((effect) => {
      const rules = view.rules.filter((rule) => rule.effect === effect && rule.scope !== "session");
      const own = rules.filter((rule) => !rule.locked);
      const builtin = rules.filter((rule) => rule.locked);
      const item = (rule: PermissionRuleRow) => <RuleItem key={`${rule.scope}-${rule.id}`} rule={rule}
        onRemove={() => { remove(rule).catch(() => undefined); }} />;
      return <div key={effect} className="permission-rule-group">
        <div className="permission-rule-head">
          <span className="settings-label">{tr(`web.permissions.effect.${effect}`)}</span>
          <span className="hint">{tr(`web.permissions.effect.${effect}.hint`)}</span>
        </div>
        {own.length > 0 && <ul className="permission-rule-list">{own.map(item)}</ul>}
        {own.length === 0 && builtin.length === 0 && <p className="hint permission-rule-empty">{tr(`web.permissions.effect.${effect}.empty`)}</p>}
        {/* Built-in rules cannot change; folded so the person's own rules stay in view. */}
        {builtin.length > 0 && <details className="permission-builtin">
          <summary className="hint">{tr("web.permissions.builtin_count", { count: builtin.length })}</summary>
          <ul className="permission-rule-list">{builtin.map(item)}</ul>
        </details>}
      </div>;
    })}
    <AddRuleForm permissions={permissions} hasProject={hasProject} />
    <p className="hint runtime-path">{tr("web.permissions.file", { path: view.path })}</p>
  </div>;
}

function RuleItem({ rule, onRemove }: { rule: PermissionRuleRow; onRemove: () => void }) {
  const tr = useT();
  const scopeTag = rule.locked ? tr("web.permissions.builtin") : tr(`web.permissions.scope.${rule.scope}`);
  return <li className="permission-rule" title={rule.description}>
    <code>{rule.text}</code>
    <Tag tone={rule.scope === "project" ? "accent" : "neutral"}>{scopeTag}</Tag>
    {rule.locked
      ? <span className="settings-lock" role="img" aria-label={tr("web.permissions.locked")} title={tr("web.permissions.locked")}><LockSimple size={13} /></span>
      : <button type="button" className="icon-button" aria-label={tr("web.permissions.remove", { rule: rule.text })} onClick={onRemove}>
        <X size={12} weight="bold" />
      </button>}
  </li>;
}

function AddRuleForm({ permissions, hasProject }: { permissions: PermissionDefaultsState; hasProject: boolean }) {
  const tr = useT();
  const [text, setText] = useState("");
  const [effect, setEffect] = useState<"deny" | "ask">("deny");
  const [scope, setScope] = useState<SettingScope>(hasProject ? "project" : "user");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!text.trim()) return;
    if (await permissions.addRule({ effect, scope, text: text.trim() })) setText("");
  };
  const scopes = hasProject ? ["project", "user"] as SettingScope[] : ["user"] as SettingScope[];
  return <form className="permission-rule-add" onSubmit={(event) => { submit(event).catch(() => undefined); }}>
    <Select className="field-select" ariaLabel={tr("web.permissions.add_effect")} value={effect}
      onChange={(next) => setEffect(next as "deny" | "ask")}
      options={[{ value: "deny", label: tr("web.permissions.effect.deny") }, { value: "ask", label: tr("web.permissions.effect.ask") }]} />
    <Select className="field-select" ariaLabel={tr("web.permissions.add_scope")} value={scope}
      onChange={(next) => setScope(next as SettingScope)}
      options={scopes.map((value) => ({ value, label: tr(`web.permissions.scope.${value}`) }))} />
    <input className="mono-input" aria-label={tr("web.permissions.add_rule")} placeholder={tr("web.permissions.add_placeholder")}
      value={text} spellCheck={false} onChange={(event) => setText(event.target.value)} />
    <button type="submit" className="button" disabled={!text.trim()}>{tr("web.permissions.add")}</button>
  </form>;
}
