import { useMemo, useState } from "react";
import { CaretDown, CaretRight, DotsThree, PencilSimple, PushPin, ShieldWarning } from "@phosphor-icons/react";
import { confirmDialog } from "../../ui/dialogs";
import { MenuButton, type MenuEntry } from "../../ui/Menu";
import { Select } from "../../ui/Select";
import { api } from "../../../api";
import { useT } from "../../../i18n";
import { label, matches, relativeTime, spaceName } from "./labels";
import type { Fact, FactKind, RunAction, Space, Tier } from "./types";

const KINDS: FactKind[] = ["preference", "constraint", "fact", "feedback", "reference"];
const TIERS: Tier[] = ["pinned", "indexed", "archived"];
const NEW_SPACE = "__new__";

type TierFilter = "all" | Tier;

export function FactsView({ projectId, facts, spaces, query, composing, onCloseComposer, run }: {
  projectId: string;
  facts: Fact[];
  spaces: Space[];
  query: string;
  composing: boolean;
  onCloseComposer: () => void;
  run: RunAction;
}) {
  const tr = useT();
  const [tier, setTier] = useState<TierFilter>("all");
  const [space, setSpace] = useState("all");
  const [showHistory, setShowHistory] = useState(false);
  const visible = useMemo(() => facts.filter((fact) => (space === "all" || fact.space_id === space)
    && matches(query, fact.content, fact.why, ...fact.markers)), [facts, space, query]);
  const active = visible.filter((fact) => fact.status === "active");
  const quarantined = visible.filter((fact) => fact.status === "quarantined");
  const history = visible.filter((fact) => fact.status === "superseded");
  const counts = Object.fromEntries(TIERS.map((value) => [value, active.filter((fact) => fact.tier === value).length])) as Record<Tier, number>;
  const shown = tier === "all" ? TIERS : [tier];
  const usedSpaces = spaces.filter((item) => facts.some((fact) => fact.space_id === item.id));
  const filtered = Boolean(query.trim()) || space !== "all";

  return <div className="mem-view" role="tabpanel">
    {composing && <FactComposer projectId={projectId} spaces={spaces} run={run} onClose={onCloseComposer} />}

    {quarantined.length > 0 && <section className="mem-review" aria-labelledby="mem-review-title">
      <header>
        <ShieldWarning size={16} weight="fill" aria-hidden="true" />
        <div>
          <h2 id="mem-review-title">{tr("web.memory.review.title", { count: quarantined.length })}</h2>
          <p>{tr("web.memory.review.hint")}</p>
        </div>
      </header>
      <ul>
        {quarantined.map((fact) => <li key={fact.id}>
          <p className="mem-review-text">{fact.content}</p>
          <div className="mem-review-actions">
            <span className="mem-meta-text">{spaceName(tr, fact.space_id, spaces)} · {relativeTime(tr, fact.created_at)}</span>
            <button type="button" className="button" onClick={() => { forgetFact(tr, projectId, fact, run).catch(() => undefined); }}>{tr("web.memory.review.discard")}</button>
            <button type="button" className="button primary" onClick={() => { run(() => api.confirmFact(projectId, fact.id)).catch(() => undefined); }}>{tr("web.memory.review.keep")}</button>
          </div>
        </li>)}
      </ul>
    </section>}

    <div className="mem-filters">
      <div className="mem-tiers" role="radiogroup" aria-label={tr("web.memory.filter.tier")}>
        {(["all", ...TIERS] as TierFilter[]).map((value) => <button key={value} type="button" role="radio" aria-checked={tier === value}
          onClick={() => setTier(value)}>
          {tr(`web.memory.tier.${value}`)}
          <span className="mem-count">{value === "all" ? active.length : counts[value]}</span>
        </button>)}
      </div>
      {usedSpaces.length > 1 && <Select className="field-select mem-space-filter" ariaLabel={tr("web.memory.filter.space")} value={space} onChange={setSpace}
        options={[{ value: "all", label: tr("web.memory.filter.all_spaces") }, ...usedSpaces.map((item) => ({ value: item.id, label: spaceName(tr, item.id, spaces) }))]} />}
    </div>

    {active.length === 0 && !composing ? <div className="mem-empty">
      <p className="mem-empty-title">{filtered ? tr("web.memory.empty.no_match") : tr("web.memory.empty.facts")}</p>
      {!filtered && <p className="hint">{tr("web.memory.empty.facts_hint")}</p>}
    </div> : shown.map((value) => {
      const items = active.filter((fact) => fact.tier === value);
      if (!items.length) return tier === "all" ? null : <div key={value} className="mem-empty"><p className="hint">{tr("web.memory.empty.tier")}</p></div>;
      return <section key={value} className="mem-group" aria-label={tr(`web.memory.tier.${value}`)}>
        <header className="mem-group-head">
          <h2>{tr(`web.memory.tier.${value}`)}</h2>
          <span>{tr(`web.memory.tier.${value}_hint`)}</span>
        </header>
        <ul className="mem-list">
          {items.map((fact) => <FactItem key={fact.id} projectId={projectId} fact={fact} spaces={spaces} run={run} />)}
        </ul>
      </section>;
    })}

    {history.length > 0 && <section className="mem-history">
      <button type="button" className="mem-disclosure" aria-expanded={showHistory} onClick={() => setShowHistory(!showHistory)}>
        {showHistory ? <CaretDown size={12} /> : <CaretRight size={12} />}
        {tr("web.memory.history.toggle", { count: history.length })}
      </button>
      {showHistory && <ul className="mem-list muted">
        {history.map((fact) => {
          const next = facts.find((item) => item.id === fact.superseded_by);
          return <li key={fact.id} className="mem-item">
            <p className="mem-item-text struck">{fact.content}</p>
            <div className="mem-item-meta">
              <span>{relativeTime(tr, fact.valid_to || fact.updated_at)}</span>
              {next && <span className="mem-replaced">→ {next.content}</span>}
            </div>
          </li>;
        })}
      </ul>}
    </section>}
  </div>;
}

async function forgetFact(tr: ReturnType<typeof useT>, projectId: string, fact: Fact, run: RunAction) {
  if (!await confirmDialog({ title: tr("web.memory.fact.forget_confirm"), message: fact.content, confirmLabel: tr("web.memory.fact.forget"), danger: true })) return;
  await run(() => api.forgetMemory(projectId, fact.id));
}

function FactItem({ projectId, fact, spaces, run }: { projectId: string; fact: Fact; spaces: Space[]; run: RunAction }) {
  const tr = useT();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(fact.content);
  const [why, setWhy] = useState(fact.why);
  const [kind, setKind] = useState<FactKind>(fact.kind);
  const fromUser = fact.origin === "user_stated" || fact.origin === "user_confirmed";
  const guarded = fact.sensitivity !== "none" || fact.space_id.startsWith("repo:");
  const pinned = fact.tier === "pinned";

  const save = async () => {
    if (!draft.trim()) return;
    const changed: Record<string, unknown> = { expected_revision: fact.revision };
    if (draft !== fact.content) changed.content = draft;
    if (why !== fact.why) changed.why = why;
    if (kind !== fact.kind) changed.kind = kind;
    if (Object.keys(changed).length === 1) { setEditing(false); return; }
    if (await run(() => api.updateFact(projectId, fact.id, changed))) setEditing(false);
  };
  const setTier = (tier: Tier) => { run(() => api.updateFact(projectId, fact.id, { tier })).catch(() => undefined); };

  const menu: MenuEntry[] = [
    { label: tr("web.panel.edit"), onSelect: () => { setDraft(fact.content); setWhy(fact.why); setKind(fact.kind); setEditing(true); setOpen(true); } },
  ];
  if (fromUser) menu.push({ label: pinned ? tr("web.memory.fact.unpin") : tr("web.memory.fact.pin"), onSelect: () => setTier(pinned ? "indexed" : "pinned") });
  if (fact.tier === "archived") menu.push({ label: tr("web.memory.fact.index"), onSelect: () => setTier("indexed") });
  if (fact.tier === "indexed") menu.push({ label: tr("web.memory.fact.archive"), onSelect: () => setTier("archived") });
  if (guarded) menu.push({ label: fact.shareable ? tr("web.memory.fact.keep_private") : tr("web.memory.fact.allow_share"), onSelect: () => { run(() => api.updateFact(projectId, fact.id, { shareable: !fact.shareable })).catch(() => undefined); } });
  menu.push("separator", { label: tr("web.memory.fact.forget"), danger: true, onSelect: () => { forgetFact(tr, projectId, fact, run).catch(() => undefined); } });

  return <li className={`mem-item${open ? " open" : ""}`}>
    {editing ? <div className="mem-edit">
      <textarea aria-label={tr("web.memory.fact.content")} value={draft} onChange={(event) => setDraft(event.target.value)} rows={3} autoFocus
        onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); save().catch(() => undefined); } if (event.key === "Escape") setEditing(false); }} />
      <KindPicker value={kind} onChange={setKind} />
      <input aria-label={tr("web.memory.fact.why")} placeholder={tr("web.memory.fact.why_placeholder")} value={why} onChange={(event) => setWhy(event.target.value)} />
      <div className="mem-edit-actions">
        <button type="button" className="button" onClick={() => setEditing(false)}>{tr("web.cancel")}</button>
        <button type="button" className="button primary" disabled={!draft.trim()} onClick={() => { save().catch(() => undefined); }}>{tr("web.panel.save")}</button>
      </div>
    </div> : <>
      <div className="mem-item-head">
        <button type="button" className="mem-item-body" aria-expanded={open} onClick={() => setOpen(!open)}>
          <p className="mem-item-text">{fact.content}</p>
          <div className="mem-item-meta">
            <span className="mem-kind" data-kind={fact.kind}>{label(tr, "web.memory.kind", fact.kind)}</span>
            <span>{spaceName(tr, fact.space_id, spaces)}</span>
            <span>{label(tr, "web.memory.origin", fact.origin)}</span>
            {fact.sensitivity !== "none" && <span className={`mem-flag ${fact.sensitivity}`}>{label(tr, "web.memory.sensitivity", fact.sensitivity)}</span>}
            {fact.shareable && <span className="mem-flag shareable">{tr("web.memory.fact.shareable")}</span>}
            <span className="mem-dim">{fact.use_count ? tr("web.memory.fact.used", { count: fact.use_count }) : tr("web.memory.fact.unused")}</span>
          </div>
        </button>
        <div className="mem-item-actions">
          {fromUser && <button type="button" className={`icon-button mem-pin${pinned ? " on" : ""}`} aria-pressed={pinned}
            aria-label={pinned ? tr("web.memory.fact.unpin") : tr("web.memory.fact.pin")} title={pinned ? tr("web.memory.fact.unpin") : tr("web.memory.fact.pin")}
            onClick={() => setTier(pinned ? "indexed" : "pinned")}><PushPin size={14} weight={pinned ? "fill" : "regular"} /></button>}
          <button type="button" className="icon-button" aria-label={tr("web.panel.edit")} title={tr("web.panel.edit")}
            onClick={() => { setDraft(fact.content); setWhy(fact.why); setKind(fact.kind); setEditing(true); setOpen(true); }}><PencilSimple size={14} /></button>
          <MenuButton label={tr("web.panel.actions_for", { name: fact.content.slice(0, 40) })} items={menu}><DotsThree size={16} weight="bold" /></MenuButton>
        </div>
      </div>
      {open && <dl className="mem-detail">
        {fact.why && <><dt>{tr("web.memory.fact.why")}</dt><dd>{fact.why}</dd></>}
        <dt>{tr("web.memory.fact.reaches")}</dt><dd>{tr(`web.memory.tier.${fact.tier}_hint`)}</dd>
        {fact.sensitivity !== "none" && <><dt>{tr("web.memory.fact.egress")}</dt><dd>{fact.shareable ? tr("web.memory.fact.egress_open") : tr("web.memory.fact.egress_guarded")}</dd></>}
        {fact.markers.length > 0 && <><dt>{tr("web.memory.fact.markers")}</dt><dd className="mem-markers">{fact.markers.map((marker) => <code key={marker}>{marker}</code>)}</dd></>}
        {fact.sources.length > 0 && <><dt>{tr("web.memory.fact.sources")}</dt><dd className="mem-markers">{fact.sources.map((source) => <code key={source} title={source}>{source.length > 28 ? `${source.slice(0, 28)}…` : source}</code>)}</dd></>}
        <dt>{tr("web.memory.fact.timeline")}</dt>
        <dd>{tr("web.memory.fact.created", { time: relativeTime(tr, fact.created_at) })}
          {fact.updated_at !== fact.created_at && <> · {tr("web.memory.fact.updated", { time: relativeTime(tr, fact.updated_at) })}</>}
          {fact.last_used_at && <> · {tr("web.memory.fact.last_used", { time: relativeTime(tr, fact.last_used_at) })}</>}
        </dd>
      </dl>}
    </>}
  </li>;
}

function KindPicker({ value, onChange }: { value: FactKind; onChange: (kind: FactKind) => void }) {
  const tr = useT();
  return <div className="mem-chips" role="radiogroup" aria-label={tr("web.memory.fact.kind")}>
    {KINDS.map((kind) => <button key={kind} type="button" role="radio" aria-checked={value === kind} data-kind={kind}
      title={tr(`web.memory.kind.${kind}_hint`)} onClick={() => onChange(kind)}>{label(tr, "web.memory.kind", kind)}</button>)}
  </div>;
}

function FactComposer({ projectId, spaces, run, onClose }: { projectId: string; spaces: Space[]; run: RunAction; onClose: () => void }) {
  const tr = useT();
  const [content, setContent] = useState("");
  const [kind, setKind] = useState<FactKind>("fact");
  const [space, setSpace] = useState("current");
  const [personal, setPersonal] = useState("");
  const [pin, setPin] = useState(false);
  const personalSpaces = spaces.filter((item) => item.type === "personal");
  const target = space === NEW_SPACE ? personal.trim() : space;
  const ready = Boolean(content.trim()) && Boolean(target);

  const submit = async () => {
    if (!ready) return;
    const subject = target === "global" ? "user" : target === "current" ? "project" : "other";
    if (await run(() => api.createFact(projectId, { content, kind, space: target, subject, pin }))) onClose();
  };

  return <form className="mem-composer" onSubmit={(event) => { event.preventDefault(); submit().catch(() => undefined); }}>
    <textarea aria-label={tr("web.memory.fact.content")} placeholder={tr("web.memory.composer.placeholder")} value={content} rows={3} autoFocus
      onChange={(event) => setContent(event.target.value)}
      onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); submit().catch(() => undefined); } if (event.key === "Escape") onClose(); }} />
    <div className="mem-composer-row">
      <span className="mem-composer-label">{tr("web.memory.fact.kind")}</span>
      <KindPicker value={kind} onChange={setKind} />
    </div>
    <div className="mem-composer-row">
      <span className="mem-composer-label">{tr("web.memory.composer.where")}</span>
      <Select className="field-select mem-composer-space" ariaLabel={tr("web.memory.composer.where")} value={space} onChange={setSpace}
        options={[
          { value: "current", label: tr("web.memory.space.project_long") },
          { value: "global", label: tr("web.memory.space.global_long") },
          ...personalSpaces.map((item) => ({ value: item.id, label: item.name })),
          { value: NEW_SPACE, label: tr("web.memory.space.new_personal") },
        ]} />
      {space === NEW_SPACE && <input aria-label={tr("web.memory.space.personal_name")} placeholder={tr("web.memory.space.personal_name")} value={personal}
        onChange={(event) => setPersonal(event.target.value)} className="mem-composer-personal" />}
    </div>
    <div className="mem-composer-foot">
      <label className="mem-check">
        <input type="checkbox" checked={pin} onChange={(event) => setPin(event.target.checked)} />
        <span>{tr("web.memory.composer.pin")}</span>
      </label>
      <span className="mem-dim mem-kbd">{tr("web.memory.composer.shortcut")}</span>
      <button type="button" className="button" onClick={onClose}>{tr("web.cancel")}</button>
      <button type="submit" className="button primary" disabled={!ready}>{tr("web.memory.composer.save")}</button>
    </div>
  </form>;
}
