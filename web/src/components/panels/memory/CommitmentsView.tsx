import { useState } from "react";
import { CalendarBlank, CaretDown, CaretRight, Check, DotsThree } from "@phosphor-icons/react";
import { confirmDialog } from "../../ui/dialogs";
import { MenuButton, type MenuEntry } from "../../ui/Menu";
import { api } from "../../../api";
import { useT } from "../../../i18n";
import { statusLabel } from "../shared";
import { dueState, matches, relativeTime } from "./labels";
import type { Commitment, RunAction } from "./types";

export function CommitmentsView({ projectId, commitments, query, composing, onCloseComposer, run }: {
  projectId: string;
  commitments: Commitment[];
  query: string;
  composing: boolean;
  onCloseComposer: () => void;
  run: RunAction;
}) {
  const tr = useT();
  const [showClosed, setShowClosed] = useState(false);
  const visible = commitments.filter((item) => matches(query, item.content));
  const open = visible.filter((item) => item.status === "open").sort((a, b) => {
    const left = dueState(tr, a.due)?.days ?? Infinity;
    const right = dueState(tr, b.due)?.days ?? Infinity;
    return left - right;
  });
  const closed = visible.filter((item) => item.status !== "open");
  return <div className="mem-view" role="tabpanel">
    {composing && <CommitmentComposer projectId={projectId} run={run} onClose={onCloseComposer} />}
    <p className="mem-lede">{tr("web.memory.commitments.lede")}</p>
    {open.length ? <ul className="mem-list">
      {open.map((item) => <CommitmentItem key={item.id} projectId={projectId} item={item} run={run} />)}
    </ul> : !composing && <div className="mem-empty">
      <p className="mem-empty-title">{query.trim() ? tr("web.memory.empty.no_match") : tr("web.memory.empty.commitments")}</p>
    </div>}
    {closed.length > 0 && <section className="mem-history">
      <button type="button" className="mem-disclosure" aria-expanded={showClosed} onClick={() => setShowClosed(!showClosed)}>
        {showClosed ? <CaretDown size={12} /> : <CaretRight size={12} />}
        {tr("web.memory.commitments.closed", { count: closed.length })}
      </button>
      {showClosed && <ul className="mem-list muted">
        {closed.map((item) => <CommitmentItem key={item.id} projectId={projectId} item={item} run={run} />)}
      </ul>}
    </section>}
  </div>;
}

function CommitmentItem({ projectId, item, run }: { projectId: string; item: Commitment; run: RunAction }) {
  const tr = useT();
  const [rescheduling, setRescheduling] = useState(false);
  const [due, setDue] = useState(item.due.slice(0, 10));
  const isOpen = item.status === "open";
  const state = dueState(tr, item.due);
  const patch = (body: Record<string, unknown>) => run(() => api.updateCommitment(projectId, item.id, body));
  const forget = async () => {
    if (!await confirmDialog({ title: tr("web.memory.commitment.delete_confirm"), message: item.content, danger: true })) return;
    await run(() => api.forgetMemory(projectId, item.id));
  };
  const menu: MenuEntry[] = isOpen
    ? [
      { label: tr("web.memory.commitment.reschedule"), onSelect: () => { setDue(item.due.slice(0, 10)); setRescheduling(true); } },
      { label: tr("web.memory.commitment.dismiss"), onSelect: () => { patch({ status: "dismissed" }).catch(() => undefined); } },
    ]
    : [{ label: tr("web.memory.commitment.reopen"), onSelect: () => { patch({ status: "open" }).catch(() => undefined); } }];
  menu.push("separator", { label: tr("web.panel.delete"), danger: true, onSelect: () => { forget().catch(() => undefined); } });
  return <li className="mem-item mem-commitment">
    <div className="mem-item-head">
      {isOpen
        ? <button type="button" className="mem-check-button" aria-label={tr("web.memory.commitment.done")} title={tr("web.memory.commitment.done")}
          onClick={() => { patch({ status: "done" }).catch(() => undefined); }}><Check size={11} weight="bold" /></button>
        : <span className={`mem-check-button closed ${item.status}`} aria-hidden="true">{item.status === "done" && <Check size={11} weight="bold" />}</span>}
      <div className="mem-item-body static">
        <p className={`mem-item-text${isOpen ? "" : " struck"}`}>{item.content}</p>
        <div className="mem-item-meta">
          {isOpen && state && <span className={`mem-due ${state.tone}`}><CalendarBlank size={12} />{state.text}</span>}
          {isOpen && !state && <span className="mem-dim">{tr("web.memory.commitment.no_due")}</span>}
          {!isOpen && <span>{statusLabel(tr, item.status)}</span>}
          <span className="mem-dim">{relativeTime(tr, isOpen ? item.created_at : item.updated_at)}</span>
        </div>
        {rescheduling && <div className="mem-inline-form">
          <input type="date" aria-label={tr("web.memory.commitment.due")} value={due} onChange={(event) => setDue(event.target.value)} autoFocus />
          <button type="button" className="button" onClick={() => setRescheduling(false)}>{tr("web.cancel")}</button>
          <button type="button" className="button" onClick={() => { patch({ due: "" }).then((ok) => { if (ok) setRescheduling(false); }).catch(() => undefined); }}>{tr("web.memory.commitment.clear_due")}</button>
          <button type="button" className="button primary" disabled={!due} onClick={() => { patch({ due }).then((ok) => { if (ok) setRescheduling(false); }).catch(() => undefined); }}>{tr("web.panel.save")}</button>
        </div>}
      </div>
      <div className="mem-item-actions">
        <MenuButton label={tr("web.panel.actions_for", { name: item.content.slice(0, 40) })} items={menu}><DotsThree size={16} weight="bold" /></MenuButton>
      </div>
    </div>
  </li>;
}

function CommitmentComposer({ projectId, run, onClose }: { projectId: string; run: RunAction; onClose: () => void }) {
  const tr = useT();
  const [content, setContent] = useState("");
  const [due, setDue] = useState("");
  const submit = async () => {
    if (!content.trim()) return;
    if (await run(() => api.createCommitment(projectId, { content, due }))) onClose();
  };
  return <form className="mem-composer" onSubmit={(event) => { event.preventDefault(); submit().catch(() => undefined); }}>
    <input aria-label={tr("web.memory.commitment.content")} placeholder={tr("web.memory.commitment.placeholder")} value={content} autoFocus
      onChange={(event) => setContent(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") onClose(); }} />
    <div className="mem-composer-foot">
      <label className="mem-date">
        <CalendarBlank size={14} aria-hidden="true" />
        <input type="date" aria-label={tr("web.memory.commitment.due")} value={due} onChange={(event) => setDue(event.target.value)} />
      </label>
      <span className="mem-dim">{tr("web.memory.commitment.due_hint")}</span>
      <button type="button" className="button" onClick={onClose}>{tr("web.cancel")}</button>
      <button type="submit" className="button primary" disabled={!content.trim()}>{tr("web.memory.commitment.add")}</button>
    </div>
  </form>;
}
