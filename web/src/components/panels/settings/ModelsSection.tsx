import { X } from "@phosphor-icons/react";
import { useState } from "react";
import { useT } from "../../../i18n";
import type { ModelOverview, ProviderStatus, RuntimeSetting } from "../../../types";
import { confirmDialog } from "../../ui/dialogs";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { Tag } from "../capability";
import { contextFormat, EntryTable, priceFormat } from "./EntryTable";
import { present, type Entry, type SectionContext } from "./entries";
import { SettingRow, type ControlProps } from "./fields";
import { ModelPicker } from "./ModelPicker";
import { SecretRow } from "./SecretField";
import { display } from "./useRuntimeSettings";

type Connection = "providers" | "endpoint";

export function modelsEntries(ctx: SectionContext): Entry[] {
  const { tr, runtime } = ctx;
  const overview = runtime.view?.models;
  const problems = overview?.problems ?? {};
  const modelItem = runtime.item("llm.model");
  const memoryItem = runtime.item("llm.memory_model");
  const modelsItem = runtime.item("llm.models");
  const connectionTerms = [
    tr("web.models.connection"), tr("web.models.use_providers"), tr("web.models.use_endpoint"),
    ...(overview?.providers ?? []).flatMap((provider) => [provider.name, provider.key_env]),
    "llm.base_url", "llm.transport", "OPENAI_BASE_URL", "OPENAI_API_KEY", tr("web.setting.llm.base_url"),
  ].join(" ");
  return present([
    { id: "connection", group: "connection", terms: connectionTerms, node: <ConnectionCard ctx={ctx} /> },
    modelItem && ctx.row("llm.model", {
      control: modelControl(modelItem, overview, tr("web.runtime.empty")), problem: problems["llm.model"],
    }, "models"),
    memoryItem && ctx.row("llm.memory_model", {
      control: modelControl(memoryItem, overview, tr("web.models.same_as_main")), problem: problems["llm.memory_model"],
    }, "models"),
    modelsItem && ctx.row("llm.models", { stacked: true, control: modelListControl(modelsItem, overview) }, "models"),
  ]);
}

export function contextEntries(ctx: SectionContext): Entry[] {
  const { tr } = ctx;
  const suggestions = ctx.runtime.view?.models?.models.map((model) => model.name) ?? [];
  const listOf = (stored: unknown) => Array.isArray(stored) ? stored.map(String) : [];
  return present([
    ctx.row("llm.context_limit"),
    ctx.row("llm.context_limits", {
      stacked: true,
      control: ({ label, stored, disabled, apply }) => <EntryTable label={label} format={contextFormat(tr)}
        stored={listOf(stored)} disabled={disabled} suggestions={suggestions} addLabel={tr("web.entries.add_limit")} apply={apply} />,
    }),
    ctx.row("llm.prices", {
      stacked: true,
      control: ({ label, stored, disabled, apply }) => <EntryTable label={label} format={priceFormat(tr)}
        stored={listOf(stored)} disabled={disabled} suggestions={suggestions} addLabel={tr("web.entries.add_price")} apply={apply} />,
    }),
  ]);
}

/**
 * How models are reached: each catalog provider with its own key, or one
 * OpenAI-compatible endpoint for every model. Only the chosen half shows.
 */
function ConnectionCard({ ctx }: { ctx: SectionContext }) {
  const tr = useT();
  const { runtime, field, secrets } = ctx;
  const baseUrl = runtime.item("llm.base_url");
  const transport = runtime.item("llm.transport");
  const inUse: Connection = display(baseUrl?.value) ? "endpoint" : "providers";
  const [chosen, setChosen] = useState<Connection>(inUse);
  const connection = baseUrl?.env_override ? inUse : chosen;
  const overview = runtime.view?.models;
  const choose = async (next: Connection) => {
    if (next === "providers" && baseUrl && baseUrl.user_value != null) {
      const ok = await confirmDialog({ title: tr("web.models.leave_endpoint"), message: tr("web.models.leave_endpoint_hint") });
      if (!ok) return;
      await runtime.save("llm.base_url", null, "user");
    }
    setChosen(next);
  };
  return <div className="connection">
    <div className="settings-row connection-head">
      <div className="runtime-meta">
        <span className="settings-label">{tr("web.models.connection")}</span>
        <p className="hint">{tr(connection === "endpoint" ? "web.models.endpoint_hint" : "web.models.providers_hint")}</p>
      </div>
      <SegmentedControl<Connection> ariaLabel={tr("web.models.connection")} value={connection} disabled={Boolean(baseUrl?.env_override)}
        onChange={(next) => { choose(next).catch(() => undefined); }}
        options={[{ value: "providers", label: tr("web.models.use_providers") }, { value: "endpoint", label: tr("web.models.use_endpoint") }]} />
    </div>
    {connection === "endpoint"
      ? <>
        {baseUrl && <SettingRow item={baseUrl} ctx={field} />}
        <SecretRow name={overview?.endpoint.key_env ?? "OPENAI_API_KEY"} hint={tr("web.models.endpoint_key_hint")} secrets={secrets}
          onChanged={runtime.reload} />
        {transport && <SettingRow item={transport} ctx={field} />}
      </>
      : <ProviderRows overview={overview} ctx={ctx} />}
  </div>;
}

function ProviderRows({ overview, ctx }: { overview: ModelOverview | undefined; ctx: SectionContext }) {
  if (!overview) return null;
  return <>{overview.providers.map((provider) => <SecretRow key={provider.id} name={provider.key_env}
    title={provider.name} secrets={ctx.secrets} onChanged={ctx.runtime.reload} status={<ProviderStatusTag provider={provider} />} />)}</>;
}

function ProviderStatusTag({ provider }: { provider: ProviderStatus }) {
  const tr = useT();
  if (provider.reachable) return <Tag tone="ok">{tr("web.models.ready")}</Tag>;
  if (provider.key_set && !provider.base_url) return <Tag tone="warning">{tr("web.models.needs_endpoint")}</Tag>;
  return null;
}

/** Model name fields: catalog suggestions plus any typed name. */
export function modelControl(item: RuntimeSetting, overview: ModelOverview | undefined, emptyLabel: string) {
  return ({ label, stored, disabled, apply }: ControlProps) => <ModelPicker label={label} overview={overview}
    disabled={disabled} value={typeof stored === "string" ? stored : ""}
    placeholder={display(item.value) || emptyLabel}
    onCommit={apply} />;
}

/** `llm.models`: chips, plus a picker that adds one. */
export function modelListControl(item: RuntimeSetting, overview: ModelOverview | undefined) {
  return (props: ControlProps) => <ModelListField item={item} overview={overview} {...props} />;
}

function ModelListField({ item, overview, label, stored, disabled, apply }: ControlProps & {
  item: RuntimeSetting;
  overview: ModelOverview | undefined;
}) {
  const tr = useT();
  const own = Array.isArray(stored) ? stored.map(String) : [];
  const effective = Array.isArray(item.value) ? item.value.map(String) : [];
  const reachable = new Set(overview?.providers.filter((provider) => provider.reachable).map((provider) => provider.id));
  const presets = (overview?.models ?? [])
    .filter((model) => model.preset && reachable.has(model.provider))
    .map((model) => model.name);
  const save = (next: string[]) => apply(next.length ? next : null);
  const empty = presets.length ? tr("web.models.presets_used", { names: presets.join(", ") }) : tr("web.models.no_presets");
  return <div className="model-list">
    {own.length > 0
      ? <ul className="chip-list" aria-label={label}>
        {own.map((name) => <li key={name} className="chip">
          <span>{name}</span>
          {!disabled && <button type="button" aria-label={tr("web.models.remove", { name })}
            onClick={() => save(own.filter((entry) => entry !== name))}><X size={10} weight="bold" /></button>}
        </li>)}
      </ul>
      : <p className="hint model-list-empty">{effective.length ? tr("web.runtime.in_use", { value: effective.join(", ") }) : empty}</p>}
    {!disabled && <ModelPicker label={tr("web.models.add")} overview={overview} value="" clearOnCommit exclude={own}
      placeholder={tr("web.models.add_placeholder")}
      onCommit={(name) => { if (name && !own.includes(name)) save([...own, name]); }} />}
  </div>;
}
