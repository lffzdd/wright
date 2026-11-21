import { LockSimple, MagnifyingGlass } from "@phosphor-icons/react";
import { useState } from "react";
import { useT } from "../../i18n";
import type { RuntimeSetting } from "../../types";
import type { ThoughtStyle } from "../../thoughtStyle";
import { CapabilityPage } from "./capability";
import { grouped, SECTIONS, settingTerms, type Entry, type PreferencePatch, type SectionContext, type SectionId } from "./settings/entries";
import { SettingRow, useSettingText } from "./settings/fields";
import { contextEntries, modelsEntries } from "./settings/ModelsSection";
import { advancedEntries, generalEntries, knowledgeEntries, memoryEntries } from "./settings/OtherSections";
import { permissionsEntries } from "./settings/PermissionsSection";
import { useHooks, useMcpConfig, usePermissionDefaults, useSecrets, useSkillSwitches } from "./settings/resources";
import { toolsEntries } from "./settings/ToolsSection";
import { useRuntimeSettings, useSaveStatus } from "./settings/useRuntimeSettings";
import { PanelNotice, type Notice } from "./shared";

const BUILDERS: Record<SectionId, (ctx: SectionContext) => Entry[]> = {
  general: generalEntries,
  models: modelsEntries,
  context: contextEntries,
  permissions: permissionsEntries,
  tools: toolsEntries,
  memory: memoryEntries,
  knowledge: knowledgeEntries,
  advanced: advancedEntries,
};

/** Sections whose rows need the typed settings to have loaded. */
const NEEDS_SETTINGS = new Set<SectionId>(["models", "context", "memory", "knowledge"]);

const SHOW_KEYS = "wright.settings.showKeys";
function readShowKeys(): boolean {
  try { return localStorage.getItem(SHOW_KEYS) === "1"; } catch { return false; }
}
function writeShowKeys(value: boolean) {
  try { localStorage.setItem(SHOW_KEYS, value ? "1" : "0"); } catch { /* per-viewer convenience only */ }
}

export function SettingsPage({
  theme, thoughtStyle = "quiet", inspectorOpen, save, projectId = null,
}: {
  theme: "dark" | "light";
  thoughtStyle?: ThoughtStyle;
  inspectorOpen: boolean;
  projectId?: string | null;
  save: (patch: PreferencePatch) => Promise<void>;
}) {
  const tr = useT();
  const status = useSaveStatus();
  const runtime = useRuntimeSettings(projectId, status.run);
  const secrets = useSecrets(status.run);
  const permissions = usePermissionDefaults(projectId, status.run);
  const skills = useSkillSwitches(projectId, status.run);
  const mcp = useMcpConfig(projectId, status.run);
  const hooks = useHooks(status.run);
  const [section, setSection] = useState<SectionId>("general");
  const [query, setQuery] = useState("");
  const [showKeys, setShowKeysState] = useState(readShowKeys);

  const needle = query.trim().toLowerCase();
  const field = { save: runtime.save, showKeys, hasProject: Boolean(projectId) };
  const ctx: SectionContext = {
    tr, projectId, searching: Boolean(needle), runtime, field, secrets, permissions, skills, mcp, hooks, showKeys,
    setShowKeys: (value) => { setShowKeysState(value); writeShowKeys(value); },
    appearance: {
      theme, thoughtStyle, inspectorOpen,
      apply: (patch) => { status.run(() => save(patch)).catch(() => undefined); },
    },
    row: (key, extra = {}, group) => {
      const item = runtime.item(key);
      if (!item) return null;
      // Keyed by project so each row picks its file again when the project changes.
      return { id: key, group, terms: settingTerms(tr, item), node: <SettingRow key={projectId ?? ""} item={item} ctx={field} {...extra} /> };
    },
  };

  const sectionsShown: SectionId[] = needle ? SECTIONS : [section];
  const visible = sectionsShown
    .map((id) => ({ id, items: sectionItems(id, ctx, needle) }))
    .filter((group) => !needle || group.items.length);

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
        <StatusBar notice={status.notice} loadError={runtime.error} warnings={runtime.view?.warnings ?? []} locked={lockedSettings(runtime.view?.settings)} />
        {visible.length === 0 && <p className="hint settings-no-match">{tr("web.settings.no_match", { query: query.trim() })}</p>}
        {visible.map((group) => <section key={group.id} className="settings-section" aria-labelledby={`settings-section-${group.id}`}>
          <header className="settings-section-head">
            <h2 id={`settings-section-${group.id}`}>{tr(`web.settings.section.${group.id}`)}</h2>
            {!needle && <p className="hint">{tr(`web.settings.section.${group.id}.hint`)}</p>}
          </header>
          {grouped(group.items).map((card) => <SettingsCard key={`${group.id}-${card.group}-${card.items[0].id}`} group={card.group} items={card.items} />)}
        </section>)}
      </div>
    </div>
  </CapabilityPage>;
}

function sectionItems(id: SectionId, ctx: SectionContext, needle: string): Entry[] {
  if (NEEDS_SETTINGS.has(id) && !ctx.runtime.view) {
    return needle ? [] : [{ id: "loading", terms: "", node: <p className="hint settings-gated">{ctx.runtime.error ? "" : ctx.tr("web.panel.loading")}</p> }];
  }
  const items = BUILDERS[id](ctx);
  return needle ? items.filter((entry) => entry.terms.toLowerCase().includes(needle)) : items;
}

function SettingsCard({ group, items }: { group: string; items: Entry[] }) {
  const tr = useT();
  const hintKey = `web.settings.group.${group}.hint`;
  return <div className="settings-card-group">
    {group && <div className="settings-group-head">
      <h3>{tr(`web.settings.group.${group}`)}</h3>
      {tr(hintKey) !== hintKey && <p className="hint">{tr(hintKey)}</p>}
    </div>}
    <div className="settings-panel">
      {items.map((entry) => <div key={entry.id} className="settings-entry">{entry.node}</div>)}
    </div>
  </div>;
}

function lockedSettings(settings: RuntimeSetting[] | undefined): RuntimeSetting[] {
  return (settings ?? []).filter((item) => item.env_override);
}

/** Save state, load errors, and which keys the environment decides. */
function StatusBar({ notice, loadError, warnings, locked }: { notice: Notice; loadError: string; warnings: string[]; locked: RuntimeSetting[] }) {
  const tr = useT();
  return <div className="settings-status-bar">
    <div className="settings-status-line">
      <span className="hint">{tr("web.runtime.applies_to_new")}</span>
      <PanelNotice notice={notice} />
    </div>
    {loadError && <p className="settings-problem" role="alert">{loadError}</p>}
    {warnings.map((warning) => <p key={warning} className="settings-problem">{warning}</p>)}
    {locked.length > 0 && <details className="settings-locked">
      <summary><LockSimple size={13} aria-hidden="true" />{tr("web.settings.locked_count", { count: locked.length })}</summary>
      <ul>{locked.map((item) => <LockedItem key={item.key} item={item} />)}</ul>
      <p className="hint">{tr("web.settings.locked_hint")}</p>
    </details>}
  </div>;
}

function LockedItem({ item }: { item: RuntimeSetting }) {
  const { label } = useSettingText(item);
  return <li><span>{label}</span><code>{item.env.join(" / ")}</code></li>;
}
