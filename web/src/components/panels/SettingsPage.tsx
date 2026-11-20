import { MagnifyingGlass } from "@phosphor-icons/react";
import { useState, type ReactNode } from "react";
import { CapabilityPage, Segmented, Switch, Tag } from "./capability";
import { PageNotice, noteOf, type Notice } from "./shared";
import { contextFormat, EntryTable, priceFormat } from "./settings/EntryTable";
import { SettingRow } from "./settings/fields";
import { modelControl, modelListControl, ProvidersCard } from "./settings/ModelsSection";
import { useRuntimeSettings, type RuntimeSettingsState } from "./settings/useRuntimeSettings";
import { getLocale, useT } from "../../i18n";
import type { RuntimeSetting } from "../../types";

type SectionId = "models" | "context" | "memory" | "sessions" | "knowledge" | "appearance" | "advanced";
const SECTIONS: SectionId[] = ["models", "context", "memory", "sessions", "knowledge", "appearance", "advanced"];
const RUNTIME_SECTIONS = new Set<SectionId>(["models", "context", "memory", "sessions", "knowledge", "advanced"]);

type Entry = { id: string; terms: string; node: ReactNode };
type PreferencePatch = { theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string };

const SHOW_KEYS = "wright.settings.showKeys";
function readShowKeys(): boolean {
  try { return localStorage.getItem(SHOW_KEYS) === "1"; } catch { return false; }
}
function writeShowKeys(value: boolean) {
  try { localStorage.setItem(SHOW_KEYS, value ? "1" : "0"); } catch { /* per-viewer convenience only */ }
}

export function SettingsPage({
  theme, inspectorOpen, save, projectId = null,
}: {
  theme: "dark" | "light";
  inspectorOpen: boolean;
  projectId?: string | null;
  save: (patch: PreferencePatch) => Promise<void>;
}) {
  const tr = useT();
  const runtime = useRuntimeSettings(projectId);
  const [section, setSection] = useState<SectionId>("models");
  const [query, setQuery] = useState("");
  const [showKeys, setShowKeysState] = useState(readShowKeys);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const setShowKeys = (value: boolean) => { setShowKeysState(value); writeShowKeys(value); };
  const apply = async (patch: PreferencePatch) => {
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await save(patch); setNotice({ tone: "ok", text: tr("web.saved") }); }
    catch (error) { setNotice(noteOf(error)); }
  };

  const entries = useSectionEntries({ runtime, showKeys, setShowKeys, theme, inspectorOpen, apply, searching: Boolean(query.trim()) });
  const needle = query.trim().toLowerCase();
  const visible = needle
    ? SECTIONS.map((id) => ({ id, items: entries[id].filter((entry) => entry.terms.toLowerCase().includes(needle)) })).filter((group) => group.items.length)
    : [{ id: section, items: entries[section] }];
  const showsRuntime = visible.some((group) => RUNTIME_SECTIONS.has(group.id));

  const search = <label className="settings-search">
    <MagnifyingGlass size={14} aria-hidden="true" />
    <input type="search" aria-label={tr("web.settings.search")} placeholder={tr("web.settings.search")} value={query}
      onChange={(event) => setQuery(event.target.value)}
      onKeyDown={(event) => { if (event.key === "Escape" && query) { event.preventDefault(); event.stopPropagation(); setQuery(""); } }} />
  </label>;

  return <CapabilityPage id="settings-title" title={tr("web.settings")} subtitle={tr("web.settings_hint")} actions={search} wide>
    <div className="settings-layout">
      <nav className="settings-nav" aria-label={tr("web.settings.sections")}>
        {SECTIONS.map((id) => <button key={id} type="button" aria-current={!needle && section === id ? "page" : undefined}
          onClick={() => { setQuery(""); setSection(id); }}>
          {tr(`web.settings.section.${id}`)}
        </button>)}
      </nav>
      <div className="settings-content">
        {showsRuntime && <ScopeBar runtime={runtime} projectId={projectId} />}
        {visible.some((group) => group.id === "appearance") && <PageNotice notice={notice} />}
        {visible.length === 0 && <p className="hint settings-no-match">{tr("web.settings.no_match", { query: query.trim() })}</p>}
        {visible.map((group) => <section key={group.id} className="settings-section" aria-labelledby={`settings-section-${group.id}`}>
          <header className="settings-section-head">
            <h2 id={`settings-section-${group.id}`}>{tr(`web.settings.section.${group.id}`)}</h2>
            {!needle && <p className="hint">{tr(`web.settings.section.${group.id}.hint`)}</p>}
          </header>
          <div className="settings-panel">
            {group.items.map((entry) => <div key={entry.id} className="settings-entry">{entry.node}</div>)}
          </div>
        </section>)}
      </div>
    </div>
  </CapabilityPage>;
}

/** Which file runtime edits go to, and how the layers stand. */
function ScopeBar({ runtime, projectId }: { runtime: RuntimeSettingsState; projectId: string | null }) {
  const tr = useT();
  const { view, scope, setScope, notice } = runtime;
  const path = view ? (scope === "user" ? view.paths.user : view.paths.project) : null;
  const projectCount = view ? view.settings.filter((item) => item.project_value !== null && item.project_value !== undefined).length : 0;
  return <div className="settings-scope-bar">
    <div className="settings-scope-main">
      <div className="settings-scope-target">
        <span className="hint">{tr("web.runtime.save_to")}</span>
        {projectId
          ? <Segmented label={tr("web.runtime.save_to")} value={scope} onChange={setScope} options={[
            { value: "user", label: tr("web.panel.scope_user") },
            { value: "project", label: tr("web.panel.scope_project") },
          ]} />
          : <Tag>{tr("web.panel.scope_user")}</Tag>}
        {projectId && projectCount > 0 && <span className="hint">{tr("web.runtime.project_count", { count: projectCount })}</span>}
      </div>
      <span className="hint settings-apply-note">{tr("web.runtime.applies_to_new")}</span>
    </div>
    {path && <p className="hint runtime-path">{tr("web.runtime.editing", { path })}</p>}
    {!projectId && view && <p className="hint">{tr("web.runtime.no_project")}</p>}
    {notice.text && <p role={notice.tone === "error" ? "alert" : "status"} className={`runtime-notice ${notice.tone === "error" ? "form-error" : "hint"}`}>{notice.text}</p>}
    {view?.warnings.map((warning) => <p key={warning} className="runtime-notice form-error">{warning}</p>)}
  </div>;
}

/** Search text for one key: its label, hint and dotted name. */
function useTerms() {
  const tr = useT();
  return (item: RuntimeSetting) => {
    const hintKey = `web.setting.${item.key}.hint`;
    return [tr(`web.setting.${item.key}`), tr(hintKey) === hintKey ? item.description : tr(hintKey), item.key, ...item.env].join(" ");
  };
}

function useSectionEntries({ runtime, showKeys, setShowKeys, theme, inspectorOpen, apply, searching }: {
  runtime: RuntimeSettingsState;
  showKeys: boolean;
  setShowKeys: (value: boolean) => void;
  theme: "dark" | "light";
  inspectorOpen: boolean;
  apply: (patch: PreferencePatch) => Promise<void>;
  searching: boolean;
}): Record<SectionId, Entry[]> {
  const tr = useT();
  const terms = useTerms();
  const { view, scope, save, item } = runtime;
  const overview = view?.models;
  const field = { scope, save, showKeys };

  const row = (key: string, extra: Partial<Parameters<typeof SettingRow>[0]> = {}): Entry | null => {
    const found = item(key);
    if (!found) return null;
    return { id: key, terms: terms(found), node: <SettingRow item={found} {...field} {...extra} /> };
  };
  const listOf = (stored: unknown) => Array.isArray(stored) ? stored.map(String) : [];
  const present = (list: Array<Entry | null>) => list.filter((entry): entry is Entry => entry !== null);

  const modelItem = item("llm.model");
  const memoryItem = item("llm.memory_model");
  const modelsItem = item("llm.models");
  const baseUrl = item("llm.base_url");
  const transport = item("llm.transport");
  const suggestions = overview?.models.map((model) => model.name) ?? [];

  const endpointRows = present([row("llm.base_url"), row("llm.transport")]);
  const endpointChanged = [baseUrl, transport].some((entry) => entry && entry.source !== "default");
  const endpoint: Entry | null = endpointRows.length ? {
    id: "endpoint",
    terms: [tr("web.models.custom_endpoint"), ...endpointRows.map((entry) => entry.terms)].join(" "),
    node: <details className="settings-disclosure" open={endpointChanged || searching || undefined}>
      <summary>
        <span className="settings-label">{tr("web.models.custom_endpoint")}</span>
        <span className="hint">{endpointChanged ? tr("web.models.custom_endpoint_on") : tr("web.models.custom_endpoint_hint")}</span>
      </summary>
      {endpointRows.map((entry) => <div key={entry.id}>{entry.node}</div>)}
    </details>,
  } : null;

  const problems = overview?.problems ?? {};
  // A server without the memory switches has memory on.
  const memoryOn = item("memory.enabled")?.value !== false;
  const memoryOff: Entry = {
    id: "memory-off",
    terms: "",
    node: <p className="hint settings-gated">{tr("web.settings.memory_off")}</p>,
  };
  const knowledgeOn = Boolean(item("knowledge.enabled")?.value);
  const knowledgeDetail = present(["knowledge.retriever", "knowledge.reranker", "knowledge.rag_dir", "knowledge.index"].map((key) => row(key)));
  const knowledgeOff: Entry = {
    id: "knowledge-off",
    terms: "",
    node: <p className="hint settings-gated">{tr("web.settings.knowledge_off")}</p>,
  };

  const loading: Entry[] = view ? [] : [{ id: "loading", terms: "", node: <p className="hint">{runtime.notice.tone === "error" ? "" : tr("web.panel.loading")}</p> }];

  return {
    models: view ? present([
      overview ? { id: "providers", terms: [tr("web.models.providers"), tr("web.models.providers_hint"), ...overview.providers.flatMap((provider) => [provider.name, provider.key_env])].join(" "), node: <ProvidersCard overview={overview} /> } : null,
      modelItem ? row("llm.model", { control: modelControl(modelItem, overview, tr("web.runtime.empty")), problem: problems["llm.model"] }) : null,
      modelsItem ? row("llm.models", { stacked: true, control: modelListControl(modelsItem, overview) }) : null,
      endpoint,
    ]) : loading,
    context: view ? present([
      row("llm.context_limit"),
      row("llm.context_limits", {
        stacked: true,
        control: ({ label, stored, disabled, apply: write }) => <EntryTable label={label} format={contextFormat(tr)} stored={listOf(stored)} disabled={disabled}
          suggestions={suggestions} addLabel={tr("web.entries.add_limit")} apply={write} />,
      }),
      row("llm.prices", {
        stacked: true,
        control: ({ label, stored, disabled, apply: write }) => <EntryTable label={label} format={priceFormat(tr)} stored={listOf(stored)} disabled={disabled}
          suggestions={suggestions} addLabel={tr("web.entries.add_price")} apply={write} />,
      }),
    ]) : loading,
    memory: view ? present([
      row("memory.enabled"),
      ...(memoryOn || searching ? present([row("memory.recall"), row("memory.auto_extract")]) : [memoryOff]),
      // Also names sessions and judges the egress check, so it stays visible with memory off.
      memoryItem ? row("llm.memory_model", { control: modelControl(memoryItem, overview, tr("web.models.same_as_main")), problem: problems["llm.memory_model"] }) : null,
    ]) : loading,
    sessions: view ? present([row("session.auto_title")]) : loading,
    knowledge: view ? present([
      row("knowledge.enabled"),
      ...(knowledgeOn || searching ? knowledgeDetail : [knowledgeOff]),
    ]) : loading,
    appearance: [
      {
        id: "theme",
        terms: `${tr("web.theme")} ${tr("web.theme.light")} ${tr("web.theme.dark")}`,
        node: <div className="settings-row">
          <span className="settings-label">{tr("web.theme")}</span>
          <Segmented label={tr("web.theme")} value={theme} onChange={(value) => { apply({ theme: value }).catch(() => undefined); }} options={[{ value: "light", label: tr("web.theme.light") }, { value: "dark", label: tr("web.theme.dark") }]} />
        </div>,
      },
      {
        id: "language",
        terms: `${tr("web.language_label")} ${tr("web.language_note")} English 中文`,
        node: <div className="settings-row">
          <div><span className="settings-label">{tr("web.language_label")}</span><p className="hint">{tr("web.language_note")}</p></div>
          <Segmented label={tr("web.language_label")} value={getLocale()} onChange={(value) => { apply({ interface_language: value }).catch(() => undefined); }} options={[{ value: "en", label: "English" }, { value: "zh-CN", label: "中文" }]} />
        </div>,
      },
      {
        id: "inspector",
        terms: `${tr("web.inspector_open")} ${tr("web.panel.layout")}`,
        node: <div className="settings-row">
          <span className="settings-label">{tr("web.inspector_open")}</span>
          <Switch label={tr("web.inspector_open")} checked={inspectorOpen} onChange={(next) => { apply({ inspector_open: next }).catch(() => undefined); }} />
        </div>,
      },
    ],
    advanced: [
      ...(view ? present([row("logging.level")]) : loading),
      {
        id: "show-keys",
        terms: `${tr("web.settings.show_keys")} ${tr("web.settings.show_keys_hint")}`,
        node: <div className="settings-row">
          <div><span className="settings-label">{tr("web.settings.show_keys")}</span><p className="hint">{tr("web.settings.show_keys_hint")}</p></div>
          <Switch label={tr("web.settings.show_keys")} checked={showKeys} onChange={setShowKeys} />
        </div>,
      },
      ...(view ? [{
        id: "files",
        terms: `${tr("web.settings.files")} settings.json .env`,
        node: <div className="settings-row stacked">
          <div className="runtime-meta">
            <span className="settings-label">{tr("web.settings.files")}</span>
            <p className="hint">{tr("web.settings.files_hint")}</p>
          </div>
          <dl className="settings-files">
            <dt>{tr("web.panel.scope_user")}</dt><dd><code>{view.paths.user}</code></dd>
            {view.paths.project && <><dt>{tr("web.panel.scope_project")}</dt><dd><code>{view.paths.project}</code></dd></>}
          </dl>
        </div>,
      }] : []),
    ],
  };
}

