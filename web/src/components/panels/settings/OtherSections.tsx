import type { ReactNode } from "react";
import { getLocale } from "../../../i18n";
import { Segmented, Switch } from "../capability";
import { note, present, type Entry, type SectionContext } from "./entries";
import { SecretRow } from "./SecretField";

/** A preference row: label, optional hint, control. */
function plainRow(label: string, control: ReactNode, hint?: string) {
  return <div className="settings-row">
    <div className="runtime-meta"><span className="settings-label">{label}</span>{hint && <p className="hint">{hint}</p>}</div>
    {control}
  </div>;
}

export function generalEntries(ctx: SectionContext): Entry[] {
  const { tr, appearance } = ctx;
  return present([
    {
      id: "theme", group: "appearance", terms: `${tr("web.theme")} ${tr("web.theme.light")} ${tr("web.theme.dark")}`,
      node: plainRow(tr("web.theme"), <Segmented label={tr("web.theme")} value={appearance.theme} onChange={(theme) => appearance.apply({ theme })}
        options={[{ value: "light", label: tr("web.theme.light") }, { value: "dark", label: tr("web.theme.dark") }]} />),
    },
    {
      id: "language", group: "appearance", terms: `${tr("web.language_label")} ${tr("web.language_note")} English 中文`,
      node: plainRow(tr("web.language_label"), <Segmented label={tr("web.language_label")} value={getLocale()}
        onChange={(value) => appearance.apply({ interface_language: value })}
        options={[{ value: "en", label: "English" }, { value: "zh-CN", label: "中文" }]} />, tr("web.language_note")),
    },
    {
      id: "thought_style", group: "appearance",
      terms: `${tr("web.thought_style")} ${tr("web.thought_style.quiet")} ${tr("web.thought_style.lead")}`,
      node: plainRow(tr("web.thought_style"), <Segmented label={tr("web.thought_style")} value={appearance.thoughtStyle}
        onChange={(thought_style) => appearance.apply({ thought_style })}
        options={[
          { value: "quiet", label: tr("web.thought_style.quiet") },
          { value: "lead", label: tr("web.thought_style.lead") },
        ]} />, tr("web.thought_style_note")),
    },
    {
      id: "inspector", group: "appearance", terms: `${tr("web.inspector_open")} ${tr("web.panel.layout")}`,
      node: plainRow(tr("web.inspector_open"), <Switch label={tr("web.inspector_open")} checked={appearance.inspectorOpen}
        onChange={(next) => appearance.apply({ inspector_open: next })} />),
    },
    ctx.row("session.auto_title", {}, "sessions"),
  ]);
}

export function memoryEntries(ctx: SectionContext): Entry[] {
  // A server without the memory switches has memory on.
  const memoryOn = ctx.runtime.item("memory.enabled")?.value !== false;
  return present([
    ctx.row("memory.enabled"),
    ...(memoryOn || ctx.searching
      ? [ctx.row("memory.recall"), ctx.row("memory.auto_extract")]
      : [note("memory-off", ctx.tr("web.settings.memory_off"))]),
  ]);
}

export function knowledgeEntries(ctx: SectionContext): Entry[] {
  const knowledgeOn = Boolean(ctx.runtime.item("knowledge.enabled")?.value);
  const detail = ["knowledge.retriever", "knowledge.reranker", "knowledge.rag_dir", "knowledge.index"].map((key) => ctx.row(key));
  const key: Entry = {
    id: "SILICONFLOW_API_KEY", terms: `SILICONFLOW_API_KEY ${ctx.tr("web.knowledge.key")}`,
    node: <SecretRow name="SILICONFLOW_API_KEY" hint={ctx.tr("web.knowledge.key_hint")} secrets={ctx.secrets} />,
  };
  return present([
    ctx.row("knowledge.enabled"),
    ...(knowledgeOn || ctx.searching ? [...detail, key] : [note("knowledge-off", ctx.tr("web.settings.knowledge_off"))]),
  ]);
}

export function advancedEntries(ctx: SectionContext): Entry[] {
  const { tr, runtime } = ctx;
  const view = runtime.view;
  const environment = view?.environment ?? [];
  const files: Array<[string, string | null | undefined]> = [
    [tr("web.settings.file.user"), view?.paths.user],
    [tr("web.settings.file.project"), view?.paths.project],
    [tr("web.settings.file.permissions"), ctx.permissions.data?.path],
    [tr("web.settings.file.keys"), ctx.secrets.data?.path],
    [tr("web.settings.file.mcp_user"), ctx.mcp.data?.files.user?.path],
    [tr("web.settings.file.mcp_project"), ctx.mcp.data?.files.project?.path],
  ];
  return present([
    ctx.row("logging.level"),
    {
      id: "show-keys", terms: `${tr("web.settings.show_keys")} ${tr("web.settings.show_keys_hint")}`,
      node: plainRow(tr("web.settings.show_keys"), <Switch label={tr("web.settings.show_keys")} checked={ctx.showKeys}
        onChange={ctx.setShowKeys} />, tr("web.settings.show_keys_hint")),
    },
    view && {
      id: "files", group: "files", terms: `${tr("web.settings.files")} settings.json .env mcp.json permission_settings.json`,
      node: <dl className="settings-files settings-gated">
        {files.filter(([, path]) => path).map(([label, path]) => <div key={label} className="settings-file">
          <dt>{label}</dt><dd><code>{path}</code></dd>
        </div>)}
      </dl>,
    },
    environment.length > 0 && {
      id: "environment", group: "environment", terms: environment.map((item) => item.name).join(" "),
      node: <dl className="settings-files settings-gated">
        {environment.map((item) => <div key={item.name} className="settings-file">
          <dt><code>{item.name}</code></dt>
          <dd>{item.value ? <code>{item.value}</code> : <span className="hint">{tr("web.runtime.empty")}</span>}
            <p className="hint">{envText(tr, item.name, item.description)}</p></dd>
        </div>)}
      </dl>,
    },
  ]);
}

function envText(tr: SectionContext["tr"], name: string, fallback: string): string {
  const key = `web.settings.env.${name}`;
  return tr(key) === key ? fallback : tr(key);
}
