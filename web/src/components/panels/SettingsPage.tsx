import { useState } from "react";
import { CapabilityPage, Segmented, Switch } from "./capability";
import { RuntimeSettings } from "./RuntimeSettings";
import { PageNotice, noteOf, type Notice } from "./shared";
import { getLocale, useT } from "../../i18n";

export function SettingsPage({
  theme, inspectorOpen, save, projectId = null,
}: {
  theme: "dark" | "light";
  inspectorOpen: boolean;
  projectId?: string | null;
  save: (patch: { theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string }) => Promise<void>;
}) {
  const tr = useT();
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const apply = async (patch: { theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string }) => {
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await save(patch); setNotice({ tone: "ok", text: tr("web.saved") }); }
    catch (error) { setNotice(noteOf(error)); }
  };
  return <CapabilityPage id="settings-title" title={tr("web.settings")} subtitle={tr("web.settings_hint")} notice={<PageNotice notice={notice} />}>
    <section className="settings-group">
      <h3>{tr("web.panel.appearance")}</h3>
      <div className="settings-row">
        <span className="settings-label">{tr("web.theme")}</span>
        <Segmented label={tr("web.theme")} value={theme} onChange={(value) => { apply({ theme: value }).catch(() => undefined); }} options={[{ value: "light", label: tr("web.theme.light") }, { value: "dark", label: tr("web.theme.dark") }]} />
      </div>
      <div className="settings-row">
        <div><span className="settings-label">{tr("web.language_label")}</span><p className="hint">{tr("web.language_note")}</p></div>
        <Segmented label={tr("web.language_label")} value={getLocale()} onChange={(value) => { apply({ interface_language: value }).catch(() => undefined); }} options={[{ value: "en", label: "English" }, { value: "zh-CN", label: "中文" }]} />
      </div>
    </section>
    <section className="settings-group">
      <h3>{tr("web.panel.layout")}</h3>
      <div className="settings-row">
        <span className="settings-label">{tr("web.inspector_open")}</span>
        <Switch label={tr("web.inspector_open")} checked={inspectorOpen} onChange={(next) => { apply({ inspector_open: next }).catch(() => undefined); }} />
      </div>
    </section>
    <RuntimeSettings projectId={projectId} />
  </CapabilityPage>;
}
