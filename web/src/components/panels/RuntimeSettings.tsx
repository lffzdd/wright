import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { useT } from "../../i18n";
import type { RuntimeSetting, RuntimeSettingsView, SettingScope } from "../../types";
import { Segmented, Switch, Tag } from "./capability";

type Notice = { tone: "idle" | "busy" | "ok" | "error"; text: string };

const GROUPS = ["llm", "session", "logging", "knowledge"] as const;

/** Accept only a well-formed view, so an old server or a stub cannot break the page. */
function asView(value: unknown): RuntimeSettingsView | null {
  const candidate = value as Partial<RuntimeSettingsView> | null;
  if (!candidate || !Array.isArray(candidate.settings) || !candidate.paths || typeof candidate.paths.user !== "string") return null;
  return { paths: candidate.paths, settings: candidate.settings, warnings: Array.isArray(candidate.warnings) ? candidate.warnings : [] };
}

/** Text shown for a value in an input or a default hint. */
function display(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function fileValue(item: RuntimeSetting, scope: SettingScope): unknown {
  return scope === "user" ? item.user_value : item.project_value;
}

/**
 * Typed runtime settings from ~/.wright/settings.json and the project's
 * .wright/settings.json. The server applies the layer rules; this only edits
 * one file at a time and shows where each effective value came from.
 */
export function RuntimeSettings({ projectId }: { projectId: string | null }) {
  const tr = useT();
  const [view, setView] = useState<RuntimeSettingsView | null>(null);
  const [scope, setScope] = useState<SettingScope>("user");
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const generation = useRef(0);

  useEffect(() => {
    const request = ++generation.current;
    setView(null);
    setScope("user");
    api.runtimeSettings(projectId)
      .then((next) => { if (request === generation.current) { setView(asView(next)); setNotice({ tone: "idle", text: "" }); } })
      .catch((error: unknown) => { if (request === generation.current) setNotice({ tone: "error", text: error instanceof Error ? error.message : String(error) }); });
  }, [projectId]);

  const save = async (key: string, value: unknown) => {
    const request = ++generation.current;
    setNotice({ tone: "busy", text: tr("web.saving") });
    try {
      const next = await api.setRuntimeSetting(projectId, { scope, key, value });
      if (request !== generation.current) return;
      setView(asView(next));
      setNotice({ tone: "ok", text: tr("web.saved") });
    } catch (error) {
      if (request === generation.current) setNotice({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    }
  };

  const path = view ? (scope === "user" ? view.paths.user : view.paths.project) : null;
  return <section className="settings-group runtime-settings" aria-labelledby="runtime-settings-title">
    <div className="runtime-settings-head">
      <div>
        <h3 id="runtime-settings-title">{tr("web.runtime.title")}</h3>
        <p className="hint">{tr("web.runtime.hint")}</p>
      </div>
      {view && <div className="runtime-scope">
        <span className="hint">{tr("web.runtime.save_to")}</span>
        {projectId
          ? <Segmented label={tr("web.runtime.save_to")} value={scope} onChange={setScope} options={[
            { value: "user", label: tr("web.panel.scope_user") },
            { value: "project", label: tr("web.panel.scope_project") },
          ]} />
          : <Tag>{tr("web.panel.scope_user")}</Tag>}
      </div>}
    </div>
    {path && <p className="hint runtime-path">{tr("web.runtime.editing", { path })}</p>}
    {!projectId && view && <p className="hint runtime-path">{tr("web.runtime.no_project")}</p>}
    {notice.text && <p role={notice.tone === "error" ? "alert" : "status"} className={`runtime-notice ${notice.tone === "error" ? "form-error" : "hint"}`}>{notice.text}</p>}
    {view?.warnings.map((warning) => <p key={warning} className="runtime-notice form-error">{warning}</p>)}
    {view && GROUPS.map((group) => {
      const items = view.settings.filter((item) => item.key.startsWith(`${group}.`));
      if (!items.length) return null;
      return <div key={group} className="runtime-group" role="group" aria-label={tr(`web.runtime.group.${group}`)}>
        <h4>{tr(`web.runtime.group.${group}`)}</h4>
        {items.map((item) => <SettingRow key={`${scope}:${item.key}`} item={item} scope={scope} save={save} />)}
      </div>;
    })}
  </section>;
}

function SettingRow({ item, scope, save }: {
  item: RuntimeSetting;
  scope: SettingScope;
  save: (key: string, value: unknown) => Promise<void>;
}) {
  const tr = useT();
  const label = tr(`web.setting.${item.key}`);
  const hintKey = `web.setting.${item.key}.hint`;
  const hint = tr(hintKey) === hintKey ? item.description : tr(hintKey);
  const editable = item.scopes.includes(scope);
  const stored = fileValue(item, scope);
  const isSet = stored !== null && stored !== undefined;
  const apply = (value: unknown) => { save(item.key, value).catch(() => undefined); };
  return <div className="settings-row runtime-row">
    <div className="runtime-meta">
      <span className="settings-label">{label}</span>
      <p className="hint">{hint}</p>
      <div className="runtime-tags">
        <code className="runtime-key">{item.key}</code>
        <Tag tone={item.source === "default" ? "neutral" : item.source === "env" ? "warning" : "accent"}>{tr(`web.runtime.source.${item.source}`)}</Tag>
        {item.env_override && <Tag tone="warning">{tr("web.runtime.env_override", { name: item.env.join(" / ") })}</Tag>}
        {item.source !== scope && item.source !== "default" && <span className="hint">{tr("web.runtime.in_use", { value: display(item.value) || tr("web.runtime.empty") })}</span>}
        {!editable && <span className="hint">{tr("web.runtime.user_only")}</span>}
      </div>
    </div>
    <div className="runtime-control">
      <SettingControl item={item} label={label} stored={stored} disabled={!editable} apply={apply} />
      {editable && isSet ? <button type="button" className="text-button" onClick={() => apply(null)}>{tr("web.runtime.reset")}</button> : <span aria-hidden="true" />}
    </div>
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
    return <select aria-label={label} disabled={disabled} value={typeof stored === "string" ? stored : ""}
      onChange={(event) => apply(event.target.value || null)}>
      <option value="">{tr("web.runtime.default_option", { value: display(item.default) })}</option>
      {item.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
    </select>;
  }
  return <TextSetting item={item} label={label} stored={stored} disabled={disabled} apply={apply} />;
}

function TextSetting({ item, label, stored, disabled, apply }: {
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
    title={item.kind === "list" ? tr("web.runtime.list_hint") : undefined}
    value={draft} placeholder={placeholder} disabled={disabled} spellCheck={false}
    onChange={(event) => setDraft(event.target.value)}
    onBlur={commit}
    onKeyDown={(event) => {
      if (event.key === "Enter") { event.preventDefault(); commit(); }
      if (event.key === "Escape") { event.preventDefault(); setDraft(saved); }
    }} />;
}
