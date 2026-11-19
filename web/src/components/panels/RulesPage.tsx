import { useEffect, useState } from "react";
import { Plus, Scroll } from "@phosphor-icons/react";
import { confirmDialog } from "../ui/dialogs";
import { CapabilityPage, EmptyState, Row, Segmented, Tag } from "./capability";
import { Field, PageNotice, noteOf, preview, type Notice } from "./shared";
import { api } from "../../api";
import { useT } from "../../i18n";

export function RulesPage({ projectId }: { projectId: string | null }) {
  const tr = useT();
  const [rules, setRules] = useState<Array<Record<string, unknown>>>([]);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [creating, setCreating] = useState(false);
  const [skillId, setSkillId] = useState("");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState("");
  const [scope, setScope] = useState<"project" | "user">("project");
  const load = async () => {
    if (!projectId) return;
    setNotice({ tone: "busy", text: tr("web.panel.loading") });
    try {
      const result = await api.rules(projectId);
      setRules(result.rules);
      setNotice({ tone: "idle", text: "" });
    } catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { setRules([]); setCreating(false); load().catch(() => undefined); }, [projectId]);
  const save = async () => {
    if (!projectId || !skillId.trim() || !body.trim()) { setNotice({ tone: "error", text: tr("web.panel.rule_required") }); return; }
    if (!await confirmDialog({ title: tr("web.panel.save_rule_confirm", { name: skillId, scope }) })) return;
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await api.saveRule(projectId, skillId.trim(), { scope, description, body, confirm: true }); setSkillId(""); setDescription(""); setBody(""); setCreating(false); await load(); }
    catch (error) { setNotice(noteOf(error)); }
  };
  const remove = async (rule: Record<string, unknown>) => {
    if (!projectId || !await confirmDialog({ title: tr("web.panel.delete_rule_confirm", { name: String(rule.id), scope: String(rule.scope ?? "project") }), danger: true })) return;
    try { await api.deleteRule(projectId, String(rule.id), String(rule.scope ?? "project")); await load(); }
    catch (error) { setNotice(noteOf(error)); }
  };
  const newButton = <button type="button" className="button primary" onClick={() => setCreating(true)}><Plus size={12} weight="bold" />{tr("web.panel.new_rule")}</button>;
  return <CapabilityPage id="rules-title" title={tr("web.panel.rules")} subtitle={projectId ? tr("web.panel.rules_hint") : undefined}
    actions={projectId && !creating ? newButton : undefined} notice={<PageNotice notice={notice} />}>
    {!projectId ? <EmptyState icon={<Scroll size={22} />} title={tr("web.panel.no_project")}>{tr("web.panel.needs_workspace")}</EmptyState> : <div className="cap-stack">
      {creating && <div className="cap-form">
        <h3>{tr("web.panel.add_rule")}</h3>
        <Field label={tr("web.panel.rule_id")}><input aria-label="Rule id" value={skillId} onChange={(event) => setSkillId(event.target.value)} placeholder="e.g. use-uv" autoFocus /></Field>
        <Field label={tr("web.panel.description")}><input aria-label="Rule description" value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
        <Field label={tr("web.panel.rule_body")}><textarea aria-label="Rule body" className="mono" value={body} onChange={(event) => setBody(event.target.value)} rows={8} /></Field>
        <div className="dialog-field"><span>{tr("web.panel.scope")}</span><Segmented label={tr("web.panel.scope")} value={scope} onChange={setScope} options={[{ value: "project", label: tr("web.panel.scope_project") }, { value: "user", label: tr("web.panel.scope_user") }]} /></div>
        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => setCreating(false)}>{tr("web.cancel")}</button>
          <button type="button" className="button primary" onClick={() => { save().catch(() => undefined); }}>{tr("web.panel.save_rule")}</button>
        </div>
      </div>}
      {rules.length ? <ul className="cap-list">{rules.map((rule) => {
        const failed = !rule.id;
        const ruleScope = String(rule.scope ?? "");
        return <Row key={`${ruleScope}-${rule.id ?? rule.error}`} title={failed ? tr("web.panel.rule_error") : String(rule.id)}
          preview={failed ? String(rule.error ?? "") : preview(rule.description)}
          meta={<>{ruleScope && <Tag tone="accent">{tr(`web.panel.scope_tag_${ruleScope}`)}</Tag>}{Boolean(rule.shadowed) && <Tag tone="warning">{tr("web.panel.shadowed")}</Tag>}{failed && <Tag tone="danger">{tr("web.status.failed")}</Tag>}</>}
          menu={failed ? undefined : [{ label: tr("web.panel.delete"), danger: true, onSelect: () => { remove(rule).catch(() => undefined); } }]} />;
      })}</ul> : !creating && notice.tone !== "busy" && <EmptyState icon={<Scroll size={22} />} title={tr("web.panel.no_rules")} />}
    </div>}
  </CapabilityPage>;
}
