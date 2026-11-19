import { useEffect, useState } from "react";
import { Brain, Plus } from "@phosphor-icons/react";
import { confirmDialog } from "../ui/dialogs";
import type { MenuEntry } from "../ui/Menu";
import { Select } from "../ui/Select";
import { shortTime } from "../inspector/Inspector";
import { CapabilityPage, EmptyState, Row, Tabs, Tag } from "./capability";
import { Field, PageNotice, isoStamp, noteOf, preview, statusLabel, type Notice } from "./shared";
import { api } from "../../api";
import { useT } from "../../i18n";

type MemoryTab = "facts" | "episodes" | "commitments" | "changes";
type Item = Record<string, unknown>;
const FACT_KINDS = ["preference", "constraint", "fact", "feedback", "reference"];

function spaceLabel(tr: ReturnType<typeof useT>, spaceId: string): string {
  if (spaceId === "global") return tr("web.panel.space_global");
  if (spaceId.startsWith("repo:")) return tr("web.panel.space_project");
  return spaceId.replace(/^personal:/, "");
}

export function MemoryPage({ projectId }: { projectId: string | null }) {
  const tr = useT();
  const [data, setData] = useState<Item | null>(null);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [tab, setTab] = useState<MemoryTab>("facts");
  const [creating, setCreating] = useState(false);
  const [content, setContent] = useState("");
  const [kind, setKind] = useState("fact");
  const [space, setSpace] = useState("current");
  const [personal, setPersonal] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const load = async () => {
    if (!projectId) return;
    setNotice({ tone: "busy", text: tr("web.panel.loading") });
    try {
      setData(await api.memory(projectId));
      setNotice({ tone: "idle", text: "" });
    } catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { setData(null); setEditingId(null); setCreating(false); load().catch(() => undefined); }, [projectId]);
  const facts = (data?.facts as Item[] | undefined) ?? [];
  const episodes = (data?.episodes as Item[] | undefined) ?? [];
  const commitments = (data?.commitments as Item[] | undefined) ?? [];
  const changes = (data?.changes as Item[] | undefined) ?? [];
  const run = async (action: () => Promise<unknown>) => {
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await action(); await load(); }
    catch (error) { setNotice(noteOf(error)); }
  };
  const addFact = async () => {
    if (!projectId) return;
    if (!content.trim()) { setNotice({ tone: "error", text: tr("web.panel.content_required") }); return; }
    const target = personal.trim() || space;
    await run(async () => {
      await api.createFact(projectId, { content, kind, space: target, subject: target === "global" ? "user" : "other" });
      setContent(""); setPersonal(""); setCreating(false);
    });
  };
  const saveFact = async (item: Item) => {
    if (!projectId) return;
    if (!draft.trim()) { setNotice({ tone: "error", text: tr("web.panel.content_required") }); return; }
    await run(async () => { await api.updateFact(projectId, String(item.id), { content: draft, expected_revision: item.revision }); setEditingId(null); });
  };
  const forget = async (item: Item, title: string) => {
    if (!projectId || !await confirmDialog({ title, danger: true })) return;
    await run(() => api.forgetMemory(projectId, String(item.id)));
  };
  return <CapabilityPage id="memory-title" title={tr("web.panel.memory")} subtitle={tr("web.panel.memory_hint")} notice={<PageNotice notice={notice} />}>
    {!projectId ? <EmptyState icon={<Brain size={22} />} title={tr("web.panel.no_project")}>{tr("web.panel.needs_workspace")}</EmptyState> : <>
      <Tabs label={tr("web.panel.memory")} value={tab} onChange={setTab} tabs={[
        { value: "facts", label: tr("web.panel.facts"), count: data ? facts.filter((item) => item.status !== "superseded").length : undefined },
        { value: "episodes", label: tr("web.panel.episodes"), count: data ? episodes.length : undefined },
        { value: "commitments", label: tr("web.panel.commitments"), count: data ? commitments.filter((item) => item.status === "open").length : undefined },
        { value: "changes", label: tr("web.panel.changes") },
      ]} />
      {tab === "facts" && <div className="cap-stack" role="tabpanel">
        <div className="cap-toolbar">
          <p className="hint">{tr("web.panel.facts_hint")}</p>
          {!creating && <button type="button" className="button primary" onClick={() => setCreating(true)}><Plus size={12} weight="bold" />{tr("web.panel.new_fact")}</button>}
        </div>
        {creating && <div className="cap-form">
          <Field label={tr("web.panel.content")}><textarea aria-label="Fact content" value={content} onChange={(event) => setContent(event.target.value)} rows={4} autoFocus /></Field>
          <Field label={tr("web.panel.kind")}>
            <Select className="field-select" ariaLabel="Fact kind" value={kind} onChange={setKind}
              options={FACT_KINDS.map((value) => ({ value, label: value }))} />
          </Field>
          <Field label={tr("web.panel.space")}>
            <Select className="field-select" ariaLabel="Fact space" value={space} onChange={setSpace}
              options={[{ value: "current", label: tr("web.panel.space_project") }, { value: "global", label: tr("web.panel.space_global") }]} />
          </Field>
          <Field label={tr("web.panel.space_personal")}><input aria-label="Personal space" value={personal} onChange={(event) => setPersonal(event.target.value)} /></Field>
          <div className="dialog-actions">
            <button type="button" className="button" onClick={() => setCreating(false)}>{tr("web.cancel")}</button>
            <button type="button" className="button primary" onClick={() => { addFact().catch(() => undefined); }}>{tr("web.panel.add_fact")}</button>
          </div>
        </div>}
        {facts.length ? <ul className="cap-list">{facts.map((item) => {
          const id = String(item.id);
          const status = String(item.status ?? "");
          const tier = String(item.tier ?? "");
          const sensitivity = String(item.sensitivity ?? "none");
          const userOrigin = item.origin === "user_stated" || item.origin === "user_confirmed";
          const menu: MenuEntry[] = [];
          if (status === "quarantined") menu.push({ label: tr("web.panel.confirm_fact"), onSelect: () => { run(() => api.confirmFact(projectId, id)).catch(() => undefined); } });
          if (status === "active") {
            menu.push({ label: tr("web.panel.edit"), onSelect: () => { setEditingId(id); setDraft(String(item.content ?? "")); } });
            if (userOrigin) menu.push({ label: tier === "pinned" ? tr("web.panel.unpin") : tr("web.panel.pin"), onSelect: () => { run(() => api.updateFact(projectId, id, { tier: tier === "pinned" ? "indexed" : "pinned" })).catch(() => undefined); } });
            if (sensitivity !== "none" || String(item.space_id ?? "").startsWith("repo:")) menu.push({ label: item.shareable ? tr("web.panel.disallow_share") : tr("web.panel.allow_share"), onSelect: () => { run(() => api.updateFact(projectId, id, { shareable: !item.shareable })).catch(() => undefined); } });
          }
          menu.push("separator", { label: tr("web.panel.delete"), danger: true, onSelect: () => { forget(item, tr("web.panel.delete_fact_confirm")).catch(() => undefined); } });
          return <Row key={id} title={String(item.content ?? "")} preview={editingId === id ? undefined : (item.why ? String(item.why) : undefined)}
            meta={<>
              <Tag>{spaceLabel(tr, String(item.space_id ?? ""))}</Tag>
              <Tag>{String(item.kind ?? "")}</Tag>
              {status === "active" && <Tag tone={tier === "pinned" ? "ok" : "neutral"}>{tr(`web.panel.tier_${tier}`)}</Tag>}
              {status !== "active" && <Tag tone="warning">{tr(`web.panel.fact_${status}`)}</Tag>}
              {sensitivity !== "none" && <Tag tone={sensitivity === "sensitive" ? "danger" : "neutral"}>{tr(`web.panel.sensitivity_${sensitivity}`)}</Tag>}
              {Boolean(item.shareable) && <Tag>{tr("web.panel.shareable")}</Tag>}
            </>}
            menu={menu}>
            {editingId === id && <div className="cap-row-edit">
              <textarea aria-label="Edit fact" value={draft} onChange={(event) => setDraft(event.target.value)} rows={4} autoFocus />
              <div className="dialog-actions">
                <button type="button" className="button" onClick={() => setEditingId(null)}>{tr("web.cancel")}</button>
                <button type="button" className="button primary" onClick={() => { saveFact(item).catch(() => undefined); }}>{tr("web.panel.save")}</button>
              </div>
            </div>}
          </Row>;
        })}</ul> : !creating && notice.tone !== "busy" && <EmptyState title={tr("web.panel.no_facts")} />}
      </div>}
      {tab === "episodes" && <div className="cap-stack" role="tabpanel">
        <p className="hint">{tr("web.panel.episodes_hint")}</p>
        {episodes.length ? <ul className="cap-list">{episodes.map((item) => {
          const title = String(item.goal || item.id);
          const status = String(item.status ?? "");
          const created = isoStamp(item.created_at);
          return <Row key={String(item.id)} title={title} preview={preview(item.outcome)}
            meta={<>{status && <Tag tone={status === "completed" ? "ok" : status === "failed" ? "danger" : "neutral"}>{statusLabel(tr, status)}</Tag>}{created && <span className="cap-row-time">{shortTime(created)}</span>}</>}
            menu={[{ label: tr("web.panel.delete"), danger: true, onSelect: () => { forget(item, tr("web.panel.delete_episode_confirm", { name: title })).catch(() => undefined); } }]} />;
        })}</ul> : notice.tone !== "busy" && <EmptyState title={tr("web.panel.no_episodes")} />}
      </div>}
      {tab === "commitments" && <div className="cap-stack" role="tabpanel">
        <p className="hint">{tr("web.panel.commitments_hint")}</p>
        {commitments.length ? <ul className="cap-list">{commitments.map((item) => {
          const id = String(item.id);
          const status = String(item.status ?? "");
          const menu: MenuEntry[] = status === "open"
            ? [
              { label: tr("web.panel.mark_done"), onSelect: () => { run(() => api.updateCommitment(projectId, id, { status: "done" })).catch(() => undefined); } },
              { label: tr("web.panel.dismiss"), onSelect: () => { run(() => api.updateCommitment(projectId, id, { status: "dismissed" })).catch(() => undefined); } },
            ]
            : [{ label: tr("web.panel.reopen"), onSelect: () => { run(() => api.updateCommitment(projectId, id, { status: "open" })).catch(() => undefined); } }];
          menu.push("separator", { label: tr("web.panel.delete"), danger: true, onSelect: () => { forget(item, tr("web.panel.delete_commitment_confirm")).catch(() => undefined); } });
          return <Row key={id} title={String(item.content ?? "")}
            meta={<>
              <Tag tone={status === "open" ? "neutral" : status === "done" ? "ok" : "warning"}>{statusLabel(tr, status)}</Tag>
              {Boolean(item.due) && <Tag>{tr("web.panel.due", { date: String(item.due) })}</Tag>}
            </>}
            menu={menu} />;
        })}</ul> : notice.tone !== "busy" && <EmptyState title={tr("web.panel.no_commitments")} />}
      </div>}
      {tab === "changes" && <div className="cap-stack" role="tabpanel">
        <p className="hint">{tr("web.panel.changes_hint")}</p>
        {changes.length ? <ul className="cap-list">{changes.map((item) => {
          const id = Number(item.id);
          const action = String(item.action ?? "");
          const reverted = Boolean(item.reverted);
          const undoable = !reverted && action !== "forget" && action !== "revert";
          const created = isoStamp(item.at);
          return <Row key={id} title={`${action} ${String(item.target_id ?? "")}`} preview={item.note ? String(item.note) : undefined}
            meta={<>
              <Tag>{String(item.actor ?? "")}</Tag>
              {reverted && <Tag tone="warning">{tr("web.panel.reverted")}</Tag>}
              {created && <span className="cap-row-time">{shortTime(created)}</span>}
            </>}
            menu={undoable ? [{ label: tr("web.panel.revert"), onSelect: () => { run(() => api.revertMemoryChange(projectId, id)).catch(() => undefined); } }] : undefined} />;
        })}</ul> : notice.tone !== "busy" && <EmptyState title={tr("web.panel.no_changes")} />}
      </div>}
    </>}
  </CapabilityPage>;
}
