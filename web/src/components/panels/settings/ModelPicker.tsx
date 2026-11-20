import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useT } from "../../../i18n";
import type { CatalogModelInfo, ModelOverview } from "../../../types";
import { formatTokens, formatUsd } from "./fields";

type Option = CatalogModelInfo & { reachable: boolean; providerName: string };

/** Catalog models, reachable providers first, filtered by what was typed. */
function useOptions(overview: ModelOverview | undefined, query: string, exclude: readonly string[]): Option[] {
  return useMemo(() => {
    if (!overview) return [];
    const providers = new Map(overview.providers.map((item, index) => [item.id, { ...item, index }]));
    const needle = query.trim().toLowerCase();
    const skip = new Set(exclude.map((name) => name.toLowerCase()));
    return overview.models
      .filter((model) => !skip.has(model.name.toLowerCase()))
      .filter((model) => !needle || model.name.toLowerCase().includes(needle) || (providers.get(model.provider)?.name ?? "").toLowerCase().includes(needle))
      .map((model) => {
        const provider = providers.get(model.provider);
        return { ...model, reachable: Boolean(provider?.reachable), providerName: provider?.name ?? model.provider };
      })
      .sort((a, b) => {
        if (a.reachable !== b.reachable) return a.reachable ? -1 : 1;
        const order = (providers.get(a.provider)?.index ?? 99) - (providers.get(b.provider)?.index ?? 99);
        if (order) return order;
        // Within a provider: presets (current models) first, then table order.
        return Number(b.preset) - Number(a.preset);
      });
  }, [overview, query, exclude]);
}

/**
 * A text field that suggests catalog models. Any name can still be typed:
 * a model outside the catalog works with a custom endpoint.
 */
export function ModelPicker({ label, value, placeholder, overview, disabled = false, exclude = [], clearOnCommit = false, onCommit }: {
  label: string;
  value: string;
  placeholder: string;
  overview: ModelOverview | undefined;
  disabled?: boolean;
  exclude?: readonly string[];
  /** Empty the field after each pick, for adding to a list. */
  clearOnCommit?: boolean;
  onCommit: (value: string | null) => void;
}) {
  const tr = useT();
  const listId = useId();
  const [draft, setDraft] = useState(value);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [filter, setFilter] = useState("");
  const list = useRef<HTMLUListElement>(null);
  useEffect(() => { setDraft(value); }, [value]);
  const options = useOptions(overview, filter, exclude);
  const shown = options.slice(0, 60);

  useEffect(() => {
    if (active < 0) return;
    list.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [active]);

  const commit = (text: string) => {
    const next = text.trim();
    setOpen(false);
    setActive(-1);
    setFilter("");
    if (clearOnCommit) {
      setDraft("");
      if (next) onCommit(next);
      return;
    }
    setDraft(next);
    if (next === value) return;
    onCommit(next || null);
  };

  return <div className="model-picker">
    <input
      role="combobox"
      aria-label={label}
      aria-expanded={open}
      aria-controls={listId}
      aria-autocomplete="list"
      aria-activedescendant={active >= 0 ? `${listId}-${active}` : undefined}
      value={draft}
      placeholder={placeholder}
      disabled={disabled}
      spellCheck={false}
      autoComplete="off"
      onFocus={() => { setFilter(""); setOpen(true); }}
      onChange={(event) => { setDraft(event.target.value); setFilter(event.target.value); setOpen(true); setActive(-1); }}
      onBlur={() => commit(draft)}
      onKeyDown={(event) => {
        if (event.key === "ArrowDown") { event.preventDefault(); setOpen(true); setActive((index) => Math.min(index + 1, shown.length - 1)); }
        else if (event.key === "ArrowUp") { event.preventDefault(); setActive((index) => Math.max(index - 1, -1)); }
        else if (event.key === "Enter") { event.preventDefault(); commit(active >= 0 && shown[active] ? shown[active].name : draft); }
        else if (event.key === "Escape") {
          event.preventDefault();
          if (open) { setOpen(false); setActive(-1); } else setDraft(clearOnCommit ? "" : value);
        }
      }}
    />
    {open && !disabled && shown.length > 0 && <ul ref={list} id={listId} role="listbox" aria-label={label} className="model-picker-list">
      {shown.flatMap((option, index) => {
        const rows = [];
        if (index === 0 || shown[index - 1].provider !== option.provider) {
          rows.push(<li key={`group:${option.provider}`} role="presentation" className="model-group">
            {option.providerName}
            {!option.reachable && <span className="model-group-note">{tr("web.models.no_key_short")}</span>}
          </li>);
        }
        rows.push(<li
          key={option.name}
          id={`${listId}-${index}`}
          data-index={index}
          role="option"
          aria-selected={index === active}
          className={`model-option${option.reachable ? "" : " unreachable"}`}
          onMouseDown={(event) => { event.preventDefault(); commit(option.name); }}
          onMouseEnter={() => setActive(index)}
        >
          <span className="model-option-name">{option.name}</span>
          <span className="model-option-meta">
            {option.context_window ? <span>{formatTokens(option.context_window)}</span> : null}
            {option.price ? <span>{formatUsd(option.price.input)} / {formatUsd(option.price.output)}</span> : null}
          </span>
        </li>);
        return rows;
      })}
    </ul>}
  </div>;
}
