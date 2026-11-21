import { useT } from "../../i18n";

/** Commit composer pinned to the bottom of the Changes panel; it commits the staged group only. */
export function CommitBar({ id, count, message, onMessage, pending, blocked, onCommit }: {
  id: string;
  count: number;
  message: string;
  onMessage: (message: string) => void;
  pending: boolean;
  blocked: boolean;
  onCommit: () => void;
}) {
  const tr = useT();
  const label = tr(count === 1 ? "web.changes.commit_one" : "web.changes.commit_many", { count });
  const submit = () => { if (!blocked && message.trim()) onCommit(); };
  return <form className="changes-commit" onSubmit={(event) => { event.preventDefault(); submit(); }}>
    <label className="sr-only" htmlFor={id}>{tr("web.changes.commit_message")}</label>
    <textarea id={id} rows={1} value={message} disabled={pending}
      placeholder={tr("web.changes.commit_placeholder")}
      onChange={(event) => onMessage(event.target.value)}
      onKeyDown={(event) => {
        if (event.key !== "Enter" || !(event.metaKey || event.ctrlKey)) return;
        event.preventDefault();
        submit();
      }} />
    <button type="submit" className="button primary" title={tr("web.changes.commit_help")}
      disabled={blocked || !message.trim()}>{label}</button>
  </form>;
}
