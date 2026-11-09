import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from "react";
import { useT } from "../../i18n";

type ConfirmOptions = { title: string; message?: string; confirmLabel?: string; danger?: boolean };
type PromptOptions = { title: string; message?: string; initial?: string; placeholder?: string; confirmLabel?: string; multiline?: boolean };

type Request =
  | { id: number; kind: "confirm"; options: ConfirmOptions; resolve: (value: boolean) => void }
  | { id: number; kind: "prompt"; options: PromptOptions; resolve: (value: string | null) => void };

let current: Request | null = null;
let nextId = 0;
let hosts = 0;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((listener) => listener());

function cancel(request: Request) {
  if (request.kind === "confirm") request.resolve(false);
  else request.resolve(null);
}

function open(request: Request) {
  if (current) cancel(current);
  current = request;
  emit();
}

function settle(value: boolean | string | null) {
  const request = current;
  current = null;
  emit();
  if (!request) return;
  if (request.kind === "confirm") request.resolve(Boolean(value));
  else request.resolve(typeof value === "string" ? value : null);
}

/** In-app replacement for `window.confirm`. Resolves `false` when dismissed. */
export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  // Components rendered without the app shell (unit tests, isolated previews) keep working.
  if (!hosts) return Promise.resolve(window.confirm([options.title, options.message].filter(Boolean).join("\n\n")));
  return new Promise((resolve) => open({ id: ++nextId, kind: "confirm", options, resolve }));
}

/** In-app replacement for `window.prompt`. Resolves `null` when dismissed. */
export function promptDialog(options: PromptOptions): Promise<string | null> {
  if (!hosts) return Promise.resolve(window.prompt([options.title, options.message].filter(Boolean).join("\n\n"), options.initial ?? ""));
  return new Promise((resolve) => open({ id: ++nextId, kind: "prompt", options, resolve }));
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** Mount once in the app shell; it renders whichever dialog is open. */
export function DialogHost() {
  const request = useSyncExternalStore(subscribe, () => current);
  useEffect(() => {
    hosts += 1;
    return () => {
      hosts -= 1;
      if (!hosts && current) { cancel(current); current = null; emit(); }
    };
  }, []);
  if (!request) return null;
  return <SmallDialog key={request.id} request={request} />;
}

function SmallDialog({ request }: { request: Request }) {
  const tr = useT();
  const { options } = request;
  const prompt = request.kind === "prompt" ? request.options : null;
  const [value, setValue] = useState(prompt?.initial ?? "");
  const field = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const confirm = useRef<HTMLButtonElement>(null);
  const restore = useRef<Element | null>(document.activeElement);
  useEffect(() => {
    if (field.current) { field.current.focus(); field.current.select(); }
    else confirm.current?.focus();
    const previous = restore.current;
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (request.kind === "prompt") {
      if (!value.trim()) return;
      settle(value);
    } else settle(true);
  };
  const danger = request.kind === "confirm" && request.options.danger;
  return <div className="small-dialog-backdrop" onPointerDown={(event) => { if (event.target === event.currentTarget) settle(request.kind === "prompt" ? null : false); }}>
    <form
      className="small-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="small-dialog-title"
      aria-describedby={options.message ? "small-dialog-message" : undefined}
      onSubmit={submit}
      onKeyDown={(event) => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); settle(request.kind === "prompt" ? null : false); }
        if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && prompt?.multiline) submit(event);
      }}
    >
      <h2 id="small-dialog-title">{options.title}</h2>
      {options.message && <p id="small-dialog-message">{options.message}</p>}
      {prompt && (prompt.multiline
        ? <textarea ref={field} aria-label={options.title} rows={8} value={value} placeholder={prompt.placeholder} onChange={(event) => setValue(event.target.value)} />
        : <input ref={field} aria-label={options.title} value={value} placeholder={prompt.placeholder} onChange={(event) => setValue(event.target.value)} />)}
      <div className="small-dialog-actions">
        <button type="button" className="button" onClick={() => settle(request.kind === "prompt" ? null : false)}>{tr("web.cancel")}</button>
        <button ref={confirm} type="submit" className={`button ${danger ? "danger-solid" : "primary"}`} disabled={Boolean(prompt) && !value.trim()}>{options.confirmLabel ?? tr("web.confirm")}</button>
      </div>
    </form>
  </div>;
}
