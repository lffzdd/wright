import { X } from "@phosphor-icons/react";
import { useT } from "../../../i18n";
import type { ModelOverview, ProviderStatus, RuntimeSetting } from "../../../types";
import { Tag } from "../capability";
import { ModelPicker } from "./ModelPicker";
import { display } from "./useRuntimeSettings";

/** Which providers can be used, and the variable that holds each one's key. */
export function ProvidersCard({ overview }: { overview: ModelOverview }) {
  const tr = useT();
  const { endpoint } = overview;
  return <div className="settings-card providers-card">
    <div className="settings-card-head">
      <span className="settings-label">{tr("web.models.providers")}</span>
      <p className="hint">{tr("web.models.providers_hint")}</p>
    </div>
    {endpoint.base_url && <div className="providers-endpoint">
      <span>{tr("web.models.endpoint_active")}</span>
      <code>{endpoint.base_url}</code>
      <Tag tone={endpoint.key_set ? "ok" : "danger"}>{endpoint.key_env} {endpoint.key_set ? tr("web.models.key_set") : tr("web.models.key_missing")}</Tag>
    </div>}
    <ul className="providers-grid">
      {overview.providers.map((provider) => <ProviderTile key={provider.id} provider={provider} customEndpoint={Boolean(endpoint.base_url)} />)}
    </ul>
  </div>;
}

function ProviderTile({ provider, customEndpoint }: { provider: ProviderStatus; customEndpoint: boolean }) {
  const tr = useT();
  let status: { tone: "ok" | "warning" | "neutral"; text: string };
  if (provider.reachable) status = { tone: "ok", text: customEndpoint ? tr("web.models.via_endpoint") : tr("web.models.ready") };
  else if (customEndpoint) status = { tone: "neutral", text: tr("web.models.not_used") };
  else if (provider.key_set && !provider.base_url) status = { tone: "warning", text: tr("web.models.needs_endpoint") };
  else status = { tone: "neutral", text: tr("web.models.no_key") };
  return <li className={`provider-tile${provider.reachable ? " ready" : ""}`}>
    <div className="provider-tile-head">
      <span className={`provider-dot ${status.tone}`} aria-hidden="true" />
      <span className="provider-name">{provider.name}</span>
      <Tag tone={status.tone}>{status.text}</Tag>
    </div>
    <code className="provider-env" title={provider.key_set ? tr("web.models.key_set") : tr("web.models.key_missing")}>
      {provider.key_env}{provider.key_set ? " ✓" : ""}
    </code>
  </li>;
}

type ControlProps = { label: string; stored: unknown; disabled: boolean; apply: (value: unknown) => void };

/** Model name fields: catalog suggestions plus any typed name. */
export function modelControl(item: RuntimeSetting, overview: ModelOverview | undefined, emptyLabel: string) {
  return ({ label, stored, disabled, apply }: ControlProps) => <ModelPicker label={label} overview={overview} disabled={disabled}
    value={typeof stored === "string" ? stored : ""}
    placeholder={display(item.value) || emptyLabel}
    onCommit={apply} />;
}

/** `llm.models`: chips, plus a picker that adds one. */
export function modelListControl(item: RuntimeSetting, overview: ModelOverview | undefined) {
  return (props: ControlProps) => <ModelListField item={item} overview={overview} {...props} />;
}

function ModelListField({ item, overview, label, stored, disabled, apply }: ControlProps & { item: RuntimeSetting; overview: ModelOverview | undefined }) {
  const tr = useT();
  const own = Array.isArray(stored) ? stored.map(String) : [];
  const effective = Array.isArray(item.value) ? item.value.map(String) : [];
  const presets = overview
    ? overview.models.filter((model) => model.preset && overview.providers.some((provider) => provider.id === model.provider && provider.reachable)).map((model) => model.name)
    : [];
  const save = (next: string[]) => apply(next.length ? next : null);
  return <div className="model-list">
    {own.length > 0
      ? <ul className="chip-list" aria-label={label}>
        {own.map((name) => <li key={name} className="chip">
          <span>{name}</span>
          {!disabled && <button type="button" aria-label={tr("web.models.remove", { name })} onClick={() => save(own.filter((entry) => entry !== name))}><X size={10} weight="bold" /></button>}
        </li>)}
      </ul>
      : <p className="hint model-list-empty">{effective.length
        ? tr("web.runtime.in_use", { value: effective.join(", ") })
        : presets.length ? tr("web.models.presets_used", { names: presets.join(", ") }) : tr("web.models.no_presets")}</p>}
    {!disabled && <ModelPicker label={tr("web.models.add")} overview={overview} value="" clearOnCommit exclude={own}
      placeholder={tr("web.models.add_placeholder")}
      onCommit={(name) => { if (name && !own.includes(name)) save([...own, name]); }} />}
  </div>;
}
