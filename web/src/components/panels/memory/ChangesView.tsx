import { ArrowCounterClockwise, Broom, Robot, Sparkle, User } from "@phosphor-icons/react";
import type { ReactNode } from "react";
import { api } from "../../../api";
import { useT } from "../../../i18n";
import { dayHeading, label, matches, relativeTime } from "./labels";
import type { Change, ChangeField, RunAction } from "./types";

const ACTOR_ICONS: Record<Change["actor"], ReactNode> = {
  user: <User size={13} weight="bold" />,
  agent: <Robot size={13} weight="bold" />,
  extractor: <Sparkle size={13} weight="bold" />,
  consolidation: <Broom size={13} weight="bold" />,
};

export function ChangesView({ projectId, changes, query, run }: { projectId: string; changes: Change[]; query: string; run: RunAction }) {
  const tr = useT();
  const visible = changes.filter((item) => matches(query, item.summary, item.note, item.target_id));
  if (!visible.length) {
    return <div className="mem-view" role="tabpanel"><div className="mem-empty">
      <p className="mem-empty-title">{query.trim() ? tr("web.memory.empty.no_match") : tr("web.memory.empty.changes")}</p>
    </div></div>;
  }
  const groups: Array<{ day: string; items: Change[] }> = [];
  for (const item of visible) {
    const day = dayHeading(tr, item.at);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.items.push(item);
    else groups.push({ day, items: [item] });
  }
  return <div className="mem-view" role="tabpanel">
    <p className="mem-lede">{tr("web.memory.changes.lede")}</p>
    {groups.map((group) => <section key={group.day} className="mem-group">
      <header className="mem-group-head"><h2>{group.day}</h2></header>
      <ol className="mem-log">
        {group.items.map((item) => <ChangeItem key={item.id} projectId={projectId} change={item} run={run} />)}
      </ol>
    </section>)}
  </div>;
}

function ChangeItem({ projectId, change, run }: { projectId: string; change: Change; run: RunAction }) {
  const tr = useT();
  const sentence = tr("web.memory.changes.sentence", {
    actor: label(tr, "web.memory.actor", change.actor),
    action: label(tr, "web.memory.action", change.action),
    target: label(tr, "web.memory.target", change.target_type),
  });
  return <li className={`mem-log-item${change.reverted ? " reverted" : ""}`}>
    <span className={`mem-actor ${change.actor}`} aria-hidden="true">{ACTOR_ICONS[change.actor] ?? ACTOR_ICONS.agent}</span>
    <div className="mem-log-main">
      <div className="mem-log-head">
        <span className="mem-log-sentence">{sentence}</span>
        {change.reverted && <span className="mem-flag">{tr("web.memory.changes.reverted")}</span>}
        <span className="mem-dim mem-log-time">{relativeTime(tr, change.at)}</span>
        {change.undoable && <button type="button" className="mem-undo" onClick={() => { run(() => api.revertMemoryChange(projectId, change.id)).catch(() => undefined); }}>
          <ArrowCounterClockwise size={12} />{tr("web.memory.changes.undo")}
        </button>}
      </div>
      {change.summary && !fieldChanged(change, "content") && <p className="mem-log-summary">{change.summary}</p>}
      {change.forgotten && <p className="mem-log-summary mem-dim">{tr("web.memory.changes.forgotten")}</p>}
      {change.fields.length > 0 && <dl className="mem-diff">
        {change.fields.map((field) => <FieldDiff key={field.name} field={field} />)}
      </dl>}
      {change.note && !/^revert #/.test(change.note) && <p className="mem-log-note">{change.note}</p>}
    </div>
  </li>;
}

function fieldChanged(change: Change, name: string): boolean {
  return change.fields.some((field) => field.name === name);
}

function FieldDiff({ field }: { field: ChangeField }) {
  const tr = useT();
  const show = (value: unknown) => {
    if (value === undefined || value === null || value === "") return "—";
    if (typeof value === "boolean") return value ? tr("web.memory.value.yes") : tr("web.memory.value.no");
    const text = String(value);
    const prefix = { tier: "web.memory.tier", kind: "web.memory.kind", status: "web.memory.status", sensitivity: "web.memory.sensitivity" }[field.name];
    return prefix ? label(tr, prefix, text) : text;
  };
  const long = field.name === "content" || field.name === "why" || field.name === "outcome";
  return <>
    <dt>{label(tr, "web.memory.field", field.name)}</dt>
    {long
      ? <dd className="mem-diff-long"><del>{show(field.before)}</del><ins>{show(field.after)}</ins></dd>
      : <dd><span className="mem-diff-old">{show(field.before)}</span><span className="mem-diff-arrow">→</span><span>{show(field.after)}</span></dd>}
  </>;
}
