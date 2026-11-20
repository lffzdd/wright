import { Plus, X } from "@phosphor-icons/react";
import { useEffect, useId, useRef, useState, type FocusEvent } from "react";
import { useT } from "../../../i18n";

export type EntryColumn = { label: string; placeholder?: string; numeric?: boolean };

export type EntryFormat = {
  columns: EntryColumn[];
  /** Cells of a stored entry; unparseable entries come back as the raw text in the first cell. */
  parse: (entry: string) => string[];
  /** The stored entry for these cells, or an error message. */
  format: (cells: string[]) => { entry: string } | { error: string };
};

type Row = { id: number; cells: string[]; error?: string };

let nextRowId = 0;
const rowsOf = (entries: readonly string[], format: EntryFormat): Row[] =>
  entries.map((entry) => ({ id: ++nextRowId, cells: format.parse(entry) }));

/**
 * A list of `name=values` entries edited as a table. Each row is checked on
 * its own; nothing is saved while a row is invalid, and blank rows drop out.
 */
export function EntryTable({ label, format, stored, disabled, suggestions = [], addLabel, apply }: {
  label: string;
  format: EntryFormat;
  stored: readonly string[];
  disabled: boolean;
  suggestions?: readonly string[];
  addLabel: string;
  apply: (value: string[] | null) => void;
}) {
  const tr = useT();
  const listId = useId();
  const savedKey = stored.join("\n");
  const [rows, setRows] = useState<Row[]>(() => rowsOf(stored, format));
  const focusNew = useRef<number | null>(null);
  const table = useRef<HTMLDivElement>(null);
  useEffect(() => { setRows(rowsOf(stored, format)); }, [savedKey]);

  useEffect(() => {
    if (focusNew.current === null) return;
    table.current?.querySelector<HTMLInputElement>(`[data-row="${focusNew.current}"] input`)?.focus();
    focusNew.current = null;
  });

  const commit = (next: Row[]) => {
    const checked = next
      .filter((row) => row.cells.some((cell) => cell.trim()))
      .map((row) => {
        const result = format.format(row.cells.map((cell) => cell.trim()));
        return "error" in result ? { ...row, error: result.error } : { ...row, error: undefined, entry: result.entry };
      });
    // Keep blank rows the person is still filling in.
    const blank = next.filter((row) => !row.cells.some((cell) => cell.trim()));
    setRows([...checked, ...blank]);
    if (checked.some((row) => row.error)) return;
    const entries = checked.map((row) => (row as Row & { entry: string }).entry);
    if (entries.join("\n") === savedKey) return;
    apply(entries.length ? entries : null);
  };

  const onRowBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
    commit(rows);
  };

  const edit = (id: number, index: number, value: string) =>
    setRows((current) => current.map((row) => row.id === id ? { ...row, cells: row.cells.map((cell, at) => at === index ? value : cell) } : row));

  const columns = `minmax(0, 2fr) ${format.columns.slice(1).map(() => "minmax(0, 1fr)").join(" ")} 28px`;
  return <div className="entry-table" ref={table} role="group" aria-label={label}>
    {rows.length > 0 && <div className="entry-row entry-head" style={{ gridTemplateColumns: columns }} aria-hidden="true">
      {format.columns.map((column) => <span key={column.label}>{column.label}</span>)}
      <span />
    </div>}
    {rows.map((row, rowIndex) => <div key={row.id} className="entry-row-wrap">
      <div className="entry-row" data-row={row.id} style={{ gridTemplateColumns: columns }} onBlur={onRowBlur}>
        {format.columns.map((column, index) => <input
          key={column.label}
          aria-label={`${column.label} ${rowIndex + 1}`}
          aria-invalid={row.error ? true : undefined}
          className={index === 0 ? "mono-input" : "num-input"}
          inputMode={column.numeric ? "decimal" : undefined}
          list={index === 0 && suggestions.length ? listId : undefined}
          placeholder={column.placeholder}
          value={row.cells[index] ?? ""}
          disabled={disabled}
          spellCheck={false}
          onChange={(event) => edit(row.id, index, event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); commit(rows); } }}
        />)}
        <button type="button" className="icon-button entry-remove" aria-label={tr("web.entries.remove", { row: rowIndex + 1 })} disabled={disabled}
          onClick={() => commit(rows.filter((item) => item.id !== row.id))}>
          <X size={12} weight="bold" />
        </button>
      </div>
      {row.error && <p className="form-error entry-error" role="alert">{row.error}</p>}
    </div>)}
    {!disabled && <button type="button" className="text-button entry-add" onClick={() => {
      const id = ++nextRowId;
      focusNew.current = id;
      setRows((current) => [...current, { id, cells: format.columns.map(() => "") }]);
    }}><Plus size={12} weight="bold" /> {addLabel}</button>}
    {suggestions.length > 0 && <datalist id={listId}>{suggestions.map((name) => <option key={name} value={name} />)}</datalist>}
  </div>;
}

const isNumber = (text: string) => /^\d+(\.\d+)?$/.test(text);

export function priceFormat(tr: (key: string, params?: Record<string, string | number>) => string): EntryFormat {
  return {
    columns: [
      { label: tr("web.entries.model"), placeholder: "my-model" },
      { label: tr("web.entries.input"), placeholder: "3", numeric: true },
      { label: tr("web.entries.output"), placeholder: "15", numeric: true },
      { label: tr("web.entries.cache_read"), placeholder: tr("web.entries.optional"), numeric: true },
    ],
    parse: (entry) => {
      const [name, values = ""] = entry.split("=", 2);
      const [input = "", output = "", cache = ""] = values.split("/");
      return [name.trim(), input.trim(), output.trim(), cache.trim()];
    },
    format: ([name, input, output, cache]) => {
      if (!name) return { error: tr("web.entries.need_model") };
      if (!isNumber(input) || !isNumber(output) || (cache && !isNumber(cache))) return { error: tr("web.entries.need_price") };
      return { entry: `${name}=${input}/${output}${cache ? `/${cache}` : ""}` };
    },
  };
}

export function contextFormat(tr: (key: string, params?: Record<string, string | number>) => string): EntryFormat {
  return {
    columns: [
      { label: tr("web.entries.model"), placeholder: "my-model" },
      { label: tr("web.entries.tokens"), placeholder: "200000", numeric: true },
    ],
    parse: (entry) => {
      const [name, tokens = ""] = entry.split("=", 2);
      return [name.trim(), tokens.trim()];
    },
    format: ([name, tokens]) => {
      if (!name) return { error: tr("web.entries.need_model") };
      const clean = tokens.replace(/[_,]/g, "");
      if (!/^\d+$/.test(clean) || Number(clean) <= 0) return { error: tr("web.entries.need_tokens") };
      return { entry: `${name}=${clean}` };
    },
  };
}
