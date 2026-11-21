import { CaretDown } from "@phosphor-icons/react";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { MarkdownContent } from "../code/Markdown";
import { useT } from "../../i18n";
import { CodeView } from "../code/CodeView";

/**
 * Caps tall tool output at a few lines with a fade and a "Show all" toggle,
 * so the timeline never nests one scroll box inside another.
 */
export function Expandable({ children }: { children: ReactNode }) {
  const tr = useT();
  const box = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [open, setOpen] = useState(false);
  useLayoutEffect(() => {
    const element = box.current;
    if (!element) return;
    const measure = () => setOverflows(element.scrollHeight > element.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [children]);
  return <div className={`expandable${open ? " open" : ""}${overflows && !open ? " clipped" : ""}`}>
    <div ref={box} className="expandable-body">{children}</div>
    {(overflows || open) && <button type="button" className="expandable-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
      <CaretDown size={11} className={open ? "flipped" : ""} aria-hidden="true" />
      {tr(open ? "web.output.collapse" : "web.output.expand")}
    </button>}
  </div>;
}

/** The parsed value when a tool's output text is a JSON object or array, else undefined. */
export function parseJsonOutput(text: string): unknown {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return undefined;
  try {
    const value: unknown = JSON.parse(trimmed);
    return value && typeof value === "object" ? value : undefined;
  } catch {
    return undefined;
  }
}

/** Pretty-printed, syntax-colored JSON. */
export function JsonView({ value }: { value: unknown }) {
  const lines = JSON.stringify(value, null, 2).split("\n").map((text) => ({ text }));
  return <CodeView lines={lines} lang="json" wrap label="JSON" />;
}

// Bookkeeping fields a person rarely needs; they stay in the raw view.
const HIDDEN_KEYS = new Set(["id", "schema_version", "project_id", "session_id", "created_at", "updated_at", "version"]);
const TITLE_KEYS = ["name", "title"];
const SUMMARY_KEYS = ["description", "summary"];
// Fields that point at a file mean prose fields hold file text, not Markdown.
const FILE_KEYS = ["path", "file", "file_path"];

/** A string worth reading as text rather than as an escaped JSON value. */
function isProse(value: unknown): value is string {
  return typeof value === "string" && (value.includes("\n") || value.length > 120);
}

function firstText(record: Record<string, unknown>, keys: string[]): [string, string] | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim() && !value.includes("\n") && value.length <= 200) return [key, value];
  }
  return null;
}

function scalar(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

/**
 * A JSON tool result laid out for reading: a record with a name or long text
 * shows its title, summary, short fields and unescaped text, with the full JSON
 * folded below. Anything else is shown as highlighted JSON.
 */
export function StructuredOutput({ value }: { value: unknown }) {
  const tr = useT();
  if (!value || typeof value !== "object" || Array.isArray(value)) return <Expandable><JsonView value={value} /></Expandable>;
  const record = value as Record<string, unknown>;
  const title = firstText(record, TITLE_KEYS);
  const summary = firstText(record, SUMMARY_KEYS.filter((key) => key !== title?.[0]));
  const used = new Set([title?.[0], summary?.[0]].filter(Boolean) as string[]);
  const prose = Object.entries(record).filter(([key, item]) => !used.has(key) && isProse(item)) as Array<[string, string]>;
  if (!title && !prose.length) return <Expandable><JsonView value={record} /></Expandable>;
  prose.forEach(([key]) => used.add(key));
  const fields = Object.entries(record).filter(([key, item]) => !used.has(key) && !HIDDEN_KEYS.has(key) && item !== null && item !== "" && !(Array.isArray(item) && !item.length));
  const markdown = !FILE_KEYS.some((key) => typeof record[key] === "string");
  return <div className="structured-output">
    {(title || summary) && <header className="so-head">
      {title && <div className="so-title">{title[1]}</div>}
      {summary && <p className="so-summary">{summary[1]}</p>}
    </header>}
    {fields.length > 0 && <dl className="so-fields">
      {fields.map(([key, item]) => <div key={key} className="so-field">
        <dt>{key}</dt>
        <dd className={typeof item === "string" ? "" : "mono"}>{scalar(item)}</dd>
      </div>)}
    </dl>}
    {prose.map(([key, text]) => <section key={key} className="so-prose">
      {(prose.length > 1 || fields.length > 0 || title) && <div className="so-label">{key}</div>}
      <Expandable>{markdown ? <div className="markdown-body"><MarkdownContent content={text} /></div> : <pre className="so-text">{text}</pre>}</Expandable>
    </section>)}
    <details className="so-raw" onToggle={(event) => {
      if (!event.currentTarget.open) return;
      const panel = event.currentTarget.lastElementChild;
      if (panel instanceof HTMLElement) panel.scrollIntoView({ block: "nearest" });
    }}>
      <summary><CaretDown size={11} aria-hidden="true" />{tr("web.output.raw")}</summary>
      <JsonView value={record} />
    </details>
  </div>;
}
