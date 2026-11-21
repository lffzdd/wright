import { useState, type FormEvent, type ReactNode } from "react";
import { useT } from "../../../i18n";
import { confirmDialog } from "../../ui/dialogs";
import { Tag } from "../capability";
import type { SecretsState } from "./resources";

/**
 * Set, replace or remove one API key in ~/.wright/.env. The page never sees
 * a stored value: the field is write-only and clears after each save.
 */
export function SecretControl({ name, secrets, onChanged }: {
  name: string;
  secrets: SecretsState;
  onChanged?: () => void;
}) {
  const tr = useT();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const source = secrets.data?.secrets.find((item) => item.name === name)?.source;
  if (!source) return null;
  if (source === "env") {
    return <span className="secret-control">
      <Tag tone="ok">{tr("web.secret.set_outside")}</Tag>
    </span>;
  }
  const save = async (next: string | null) => {
    const ok = await secrets.set(name, next);
    if (!ok) return;
    setValue("");
    setEditing(false);
    onChanged?.();
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (value.trim()) save(value).catch(() => undefined);
  };
  const remove = async () => {
    if (!await confirmDialog({ title: tr("web.secret.remove_confirm", { name }), danger: true })) return;
    await save(null);
  };
  if (editing) {
    return <form className="secret-control editing" onSubmit={submit}>
      <input type="password" autoComplete="off" spellCheck={false} aria-label={tr("web.secret.value_for", { name })}
        placeholder={tr("web.secret.paste")} value={value} autoFocus onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); setEditing(false); setValue(""); } }} />
      <button type="submit" className="button primary" disabled={!value.trim()}>{tr("web.secret.save")}</button>
      <button type="button" className="text-button" onClick={() => { setEditing(false); setValue(""); }}>{tr("web.cancel")}</button>
    </form>;
  }
  if (source === "missing") {
    return <span className="secret-control">
      <button type="button" className="button" onClick={() => setEditing(true)}>{tr("web.secret.set")}</button>
    </span>;
  }
  return <span className="secret-control">
    <Tag tone="ok">{tr("web.secret.saved")}</Tag>
    <button type="button" className="text-button" onClick={() => setEditing(true)}>{tr("web.secret.replace")}</button>
    <button type="button" className="text-button" onClick={() => { remove().catch(() => undefined); }}>{tr("web.secret.remove")}</button>
  </span>;
}

/** A settings row for one key: its variable name, what it is for, and the control. */
export function SecretRow({ name, title, hint, secrets, onChanged, status }: {
  name: string;
  title?: string;
  hint?: string;
  secrets: SecretsState;
  onChanged?: () => void;
  /** Shown beside the title, such as whether the provider is ready. */
  status?: ReactNode;
}) {
  return <div className="settings-row secret-row" data-secret={name}>
    <div className="runtime-meta">
      <span className="settings-label">{title ?? name}{status}</span>
      {title && <code className="runtime-key">{name}</code>}
      {hint && <p className="hint">{hint}</p>}
    </div>
    <SecretControl name={name} secrets={secrets} onChanged={onChanged} />
  </div>;
}
