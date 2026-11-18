import { useEffect, useRef, useState, type ReactNode } from "react";
import { Brain, Clock, MagnifyingGlass, Plus, Scroll } from "@phosphor-icons/react";
import { confirmDialog } from "../ui/dialogs";
import type { MenuEntry } from "../ui/Menu";
import { Select } from "../ui/Select";
import { shortTime } from "../inspector/Inspector";
import { CapabilityPage, EmptyState, Row, Segmented, Switch, Tabs, Tag } from "./capability";
import { RuntimeSettings } from "./RuntimeSettings";
import { INTERVAL_UNITS, describeInterval, splitInterval, toSeconds, type IntervalUnit } from "./interval";
import { api } from "../../api";
import { getLocale, useT } from "../../i18n";

type Notice = { tone: "idle" | "busy" | "ok" | "error"; text: string };

function noteOf(error: unknown): Notice {
  return { tone: "error", text: error instanceof Error ? error.message : String(error) };
}

function PanelNotice({ notice }: { notice: Notice }) {
  if (!notice.text) return null;
  return <p role={notice.tone === "error" ? "alert" : "status"} className={notice.tone === "error" ? "form-error" : "hint"}>{notice.text}</p>;
}

function Panel({ id, title, close, children }: { id: string; title: string; close: () => void; children: ReactNode }) {
  const tr = useT();
  return <div className="dialog-backdrop"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby={id} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); } }}>
    <h2 id={id}>{title}</h2>
    {children}
    <div className="dialog-actions dialog-foot"><button type="button" className="button" onClick={close}>{tr("web.close")}</button></div>
  </section></div>;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="dialog-field"><span>{label}</span>{children}</label>;
}

export function SearchDialog({
  sessionId, projectId, close, openFile, openSession, previewFiles = false,
}: {
  sessionId: string | null;
  projectId: string | null;
  close: () => void;
  openFile: (path: string) => void;
  openSession?: (sessionId: string) => void | Promise<void>;
  previewFiles?: boolean;
}) {
  const tr = useT();
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("file");
  const [results, setResults] = useState<Array<Record<string, unknown>>>([]);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [preview, setPreview] = useState<{ path: string; content: string } | null>(null);
  const [shown, setShown] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setShown(true));
    return () => window.cancelAnimationFrame(frame);
  }, []);
  const typing = useRef<number | undefined>(undefined);
  useEffect(() => () => { generation.current += 1; }, []);
  const run = async () => {
    window.clearTimeout(typing.current);
    if (!query.trim()) { setNotice({ tone: "error", text: tr("web.panel.search_empty") }); return; }
    setNotice({ tone: "busy", text: tr("web.panel.searching") });
    const request = ++generation.current;
    setPreview(null);
    try {
      const found = sessionId
        ? await api.search(sessionId, query.trim(), kind)
        : projectId
          ? await api.workspaceSearch(projectId, query.trim(), kind)
          : null;
      if (request !== generation.current) return;
      if (!found) { setNotice({ tone: "error", text: tr("web.panel.search_no_workspace") }); return; }
      setResults(found.results);
      setNotice({ tone: "ok", text: found.results.length ? tr(found.results.length === 1 ? "web.panel.results_one" : "web.panel.results_many", { count: found.results.length }) : tr("web.panel.no_matches") });
    } catch (error) { if (request === generation.current) setNotice(noteOf(error)); }
  };
  useEffect(() => {
    ++generation.current;
    setResults([]); setPreview(null); setNotice({ tone: "idle", text: "" });
    if (!query.trim()) return;
    typing.current = window.setTimeout(() => { run().catch(() => undefined); }, 250);
    return () => window.clearTimeout(typing.current);
  }, [sessionId, projectId, query, kind]);
  const activate = async (item: Record<string, unknown>) => {
    if (item.kind === "task" && item.session_id && openSession) {
      try { await openSession(String(item.session_id)); }
      catch (error) { setNotice(noteOf(error)); }
      return;
    }
    if (!item.path) return;
    const path = String(item.path);
    if (!previewFiles) { openFile(path); return; }
    if (!projectId) return;
    const request = ++generation.current;
    setPreview(null);
    setNotice({ tone: "busy", text: tr("web.panel.loading_file") });
    try {
      const file = await api.workspaceFile(projectId, path);
      if (request !== generation.current) return;
      setPreview({ path, content: file.binary ? tr("web.panel.binary_file") : file.content });
      setNotice({ tone: "ok", text: "" });
    } catch (error) { if (request === generation.current) setNotice(noteOf(error)); }
  };
  return <div className={`overlay palette-overlay${shown ? " show" : ""}`} role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
    <div className="palette" role="dialog" aria-modal="true" aria-labelledby="search-title">
      <div className="pal-in">
        <MagnifyingGlass size={16} aria-hidden="true" />
        <input autoFocus id="search-title" aria-label={tr("web.search")} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); close(); } if (event.key === "Enter" && !event.nativeEvent.isComposing) run(); }} placeholder={tr("web.search")} />
        <Select className="field-select" ariaLabel="Search kind" value={kind} onChange={setKind}
          options={["file", "content", "task", "symbol"].map((value) => ({ value, label: tr(`web.panel.kind_${value}`) }))} />
      </div>
      <PanelNotice notice={notice} />
      <div className="pal-list change-list">{results.length ? results.map((item, index) => {
        const path = String(item.path ?? item.name ?? item.title ?? item.id ?? index);
        const line = item.line ? `:${item.line}` : "";
        return <button type="button" className={`pal-i${index === 0 ? " sel" : ""}`} key={`${path}-${index}`} onClick={() => activate(item)}>
          <span className="lbl">{item.kind ? `${item.kind} ` : ""}{path}{line}</span>
          {typeof item.text === "string" && <span className="d">{item.text.trim().slice(0, 80)}</span>}
        </button>;
      }) : <p className="pal-empty">{query.trim() ? tr("web.panel.no_matches") : tr("web.panel.search_empty")}</p>}</div>
      <div className="pal-foot">
        <span><kbd>↵</kbd>{tr("web.panel.search")}</span>
        <span><kbd>Esc</kbd>{tr("web.close")}</span>
      </div>
      {preview && <section className="palette-preview" aria-label="File preview"><h3>{preview.path}</h3><pre className="term-body">{preview.content || tr("web.empty_file")}</pre></section>}
    </div>
  </div>;
}

function PageNotice({ notice }: { notice: Notice }) {
  if (!notice.text) return null;
  return <div className={`cap-notice ${notice.tone}`}><PanelNotice notice={notice} /></div>;
}

function statusLabel(tr: ReturnType<typeof useT>, status: string): string {
  const key = `web.status.${status}`;
  const label = tr(key);
  return label === key ? status : label;
}

function preview(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function stamp(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

type MemoryTab = "facts" | "episodes" | "commitments" | "changes";
type Item = Record<string, unknown>;
const FACT_KINDS = ["preference", "constraint", "fact", "feedback", "reference"];

function isoStamp(value: unknown): number | null {
  if (typeof value !== "string" || !value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed / 1000 : null;
}

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

export function RulesPage({ projectId }: { projectId: string | null }) {
  const tr = useT();
  const [rules, setRules] = useState<Array<Record<string, unknown>>>([]);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [creating, setCreating] = useState(false);
  const [skillId, setSkillId] = useState("");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState("");
  const [scope, setScope] = useState<"project" | "user">("project");
  const load = async () => {
    if (!projectId) return;
    setNotice({ tone: "busy", text: tr("web.panel.loading") });
    try {
      const result = await api.rules(projectId);
      setRules(result.rules);
      setNotice({ tone: "idle", text: "" });
    } catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { setRules([]); setCreating(false); load().catch(() => undefined); }, [projectId]);
  const save = async () => {
    if (!projectId || !skillId.trim() || !body.trim()) { setNotice({ tone: "error", text: tr("web.panel.rule_required") }); return; }
    if (!await confirmDialog({ title: tr("web.panel.save_rule_confirm", { name: skillId, scope }) })) return;
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await api.saveRule(projectId, skillId.trim(), { scope, description, body, confirm: true }); setSkillId(""); setDescription(""); setBody(""); setCreating(false); await load(); }
    catch (error) { setNotice(noteOf(error)); }
  };
  const remove = async (rule: Record<string, unknown>) => {
    if (!projectId || !await confirmDialog({ title: tr("web.panel.delete_rule_confirm", { name: String(rule.id), scope: String(rule.scope ?? "project") }), danger: true })) return;
    try { await api.deleteRule(projectId, String(rule.id), String(rule.scope ?? "project")); await load(); }
    catch (error) { setNotice(noteOf(error)); }
  };
  const newButton = <button type="button" className="button primary" onClick={() => setCreating(true)}><Plus size={12} weight="bold" />{tr("web.panel.new_rule")}</button>;
  return <CapabilityPage id="rules-title" title={tr("web.panel.rules")} subtitle={projectId ? tr("web.panel.rules_hint") : undefined}
    actions={projectId && !creating ? newButton : undefined} notice={<PageNotice notice={notice} />}>
    {!projectId ? <EmptyState icon={<Scroll size={22} />} title={tr("web.panel.no_project")}>{tr("web.panel.needs_workspace")}</EmptyState> : <div className="cap-stack">
      {creating && <div className="cap-form">
        <h3>{tr("web.panel.add_rule")}</h3>
        <Field label={tr("web.panel.rule_id")}><input aria-label="Rule id" value={skillId} onChange={(event) => setSkillId(event.target.value)} placeholder="e.g. use-uv" autoFocus /></Field>
        <Field label={tr("web.panel.description")}><input aria-label="Rule description" value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
        <Field label={tr("web.panel.rule_body")}><textarea aria-label="Rule body" className="mono" value={body} onChange={(event) => setBody(event.target.value)} rows={8} /></Field>
        <div className="dialog-field"><span>{tr("web.panel.scope")}</span><Segmented label={tr("web.panel.scope")} value={scope} onChange={setScope} options={[{ value: "project", label: tr("web.panel.scope_project") }, { value: "user", label: tr("web.panel.scope_user") }]} /></div>
        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => setCreating(false)}>{tr("web.cancel")}</button>
          <button type="button" className="button primary" onClick={() => { save().catch(() => undefined); }}>{tr("web.panel.save_rule")}</button>
        </div>
      </div>}
      {rules.length ? <ul className="cap-list">{rules.map((rule) => {
        const failed = !rule.id;
        const ruleScope = String(rule.scope ?? "");
        return <Row key={`${ruleScope}-${rule.id ?? rule.error}`} title={failed ? tr("web.panel.rule_error") : String(rule.id)}
          preview={failed ? String(rule.error ?? "") : preview(rule.description)}
          meta={<>{ruleScope && <Tag tone="accent">{tr(`web.panel.scope_tag_${ruleScope}`)}</Tag>}{Boolean(rule.shadowed) && <Tag tone="warning">{tr("web.panel.shadowed")}</Tag>}{failed && <Tag tone="danger">{tr("web.status.failed")}</Tag>}</>}
          menu={failed ? undefined : [{ label: tr("web.panel.delete"), danger: true, onSelect: () => { remove(rule).catch(() => undefined); } }]} />;
      })}</ul> : !creating && notice.tone !== "busy" && <EmptyState icon={<Scroll size={22} />} title={tr("web.panel.no_rules")} />}
    </div>}
  </CapabilityPage>;
}

function runTone(status: string): "ok" | "danger" | "accent" | "neutral" {
  if (status === "completed") return "ok";
  if (status === "failed") return "danger";
  if (status === "running" || status === "dispatched" || status === "queued" || status === "waiting_retry") return "accent";
  return "neutral";
}

export function SchedulesPage({ sessionId, projectId }: { sessionId: string | null; projectId: string | null }) {
  const tr = useT();
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [history, setHistory] = useState<{ id: string; runs: Array<Record<string, unknown>> } | null>(null);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [count, setCount] = useState("1");
  const [unit, setUnit] = useState<IntervalUnit>("hours");
  const [editing, setEditing] = useState<string | null>(null);
  const load = async () => {
    if (!projectId) return;
    setNotice({ tone: "busy", text: tr("web.panel.loading") });
    try { setRows(await api.projectSchedules(projectId)); setNotice({ tone: "idle", text: "" }); }
    catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { setRows([]); setHistory(null); setFormOpen(false); load().catch(() => undefined); }, [projectId]);
  const act = async (id: string, action: string) => {
    if (!projectId) return;
    if (action === "delete" && !await confirmDialog({ title: tr("web.panel.delete_schedule_confirm", { name: id }), danger: true })) return;
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await api.scheduleAction(projectId, id, action, action === "delete" ? { confirm: true } : {}); await load(); }
    catch (error) { setNotice(noteOf(error)); }
  };
  const openForm = (row?: Record<string, unknown>) => {
    const trigger = row?.trigger as { every_seconds?: number } | undefined;
    const split = splitInterval(trigger?.every_seconds ?? 3600);
    setEditing(row ? String(row.id) : null);
    setName(row ? String(row.name ?? "") : "");
    setPrompt(row ? String(row.prompt ?? "") : "");
    setCount(String(split.count)); setUnit(split.unit);
    setFormOpen(true);
  };
  const showHistory = async (id: string) => {
    if (history?.id === id) { setHistory(null); return; }
    if (!sessionId) { setNotice({ tone: "error", text: tr("web.panel.history_needs_task") }); return; }
    try { setHistory({ id, runs: await api.scheduleRuns(sessionId, id) }); }
    catch (error) { setNotice(noteOf(error)); }
  };
  const save = async () => {
    const amount = Number(count);
    if (!name.trim() || !prompt.trim() || !Number.isFinite(amount) || amount <= 0) { setNotice({ tone: "error", text: tr("web.panel.schedule_required") }); return; }
    const trigger = { type: "interval", every_seconds: toSeconds(amount, unit) };
    setNotice({ tone: "busy", text: tr("web.saving") });
    try {
      if (editing) {
        if (!projectId) return;
        await api.scheduleAction(projectId, editing, "update", { name, prompt, trigger });
      } else {
        if (!sessionId) { setNotice({ tone: "error", text: tr("web.panel.schedule_needs_task") }); return; }
        await api.createSchedule(sessionId, { name, prompt, trigger, recovery_policy: "manual" });
      }
      setEditing(null); setFormOpen(false); setName(""); setPrompt(""); await load();
    } catch (error) { setNotice(noteOf(error)); }
  };
  return <CapabilityPage id="schedule-title" title={tr("web.panel.schedules")}
    subtitle={projectId && !sessionId ? tr("web.panel.schedule_needs_task") : undefined}
    actions={projectId && !formOpen ? <button type="button" className="button primary" disabled={!sessionId} onClick={() => openForm()}><Plus size={12} weight="bold" />{tr("web.panel.new_schedule")}</button> : undefined}
    notice={<PageNotice notice={notice} />}>
    {!projectId ? <EmptyState icon={<Clock size={22} />} title={tr("web.panel.no_project")} /> : <div className="cap-stack">
      {formOpen && <div className="cap-form">
        <h3>{tr(editing ? "web.panel.edit_schedule" : "web.panel.add_schedule")}</h3>
        <Field label={tr("web.panel.name")}><input aria-label="Schedule name" value={name} onChange={(event) => setName(event.target.value)} autoFocus /></Field>
        <Field label={tr("web.panel.prompt")}><textarea aria-label="Schedule prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={4} /></Field>
        <div className="dialog-field"><span>{tr("web.panel.run_every")}</span>
          <div className="interval-field">
            <input aria-label={tr("web.panel.run_every")} type="number" min={1} value={count} onChange={(event) => setCount(event.target.value)} />
            <Select className="field-select" ariaLabel={tr("web.panel.interval_unit")} value={unit} onChange={(next) => setUnit(next as IntervalUnit)}
              options={INTERVAL_UNITS.map((value) => ({ value, label: tr(`web.panel.unit.${value}`) }))} />
          </div>
        </div>
        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => { setFormOpen(false); setEditing(null); }}>{tr("web.cancel")}</button>
          <button type="button" className="button primary" onClick={() => { save().catch(() => undefined); }}>{tr(editing ? "web.panel.save_schedule" : "web.panel.create_schedule")}</button>
        </div>
      </div>}
      {rows.length ? <ul className="cap-list">{rows.map((row) => {
        const id = String(row.id);
        const title = String(row.name ?? row.id);
        const status = String(row.status ?? "");
        const trigger = row.trigger as { type?: string; every_seconds?: number } | undefined;
        const next = stamp(row.next_run_at);
        const toggleable = status === "active" || status === "paused";
        return <Row key={id} title={title} preview={preview(row.prompt)}
          meta={<>
            <Tag>{trigger?.every_seconds ? describeInterval(trigger.every_seconds, tr) : tr("web.panel.trigger_other", { type: String(trigger?.type ?? "?") })}</Tag>
            {status && <Tag tone={status === "active" ? "ok" : status === "paused" ? "warning" : "neutral"}>{tr(`web.panel.schedule_status.${status}`)}</Tag>}
            {next && status === "active" && <span className="cap-row-time">{tr("web.panel.next_run", { time: shortTime(next) })}</span>}
          </>}
          side={<Switch label={tr("web.panel.toggle_schedule", { name: title })} checked={status === "active"} disabled={!toggleable} onChange={(on) => { act(id, on ? "resume" : "pause").catch(() => undefined); }} />}
          menu={[
            { label: tr("web.panel.edit"), onSelect: () => openForm(row) },
            { label: tr(history?.id === id ? "web.panel.hide_history" : "web.panel.history"), onSelect: () => { showHistory(id).catch(() => undefined); } },
            "separator",
            { label: tr("web.panel.delete"), danger: true, onSelect: () => { act(id, "delete").catch(() => undefined); } },
          ]}>
          {history?.id === id && <ul className="run-list" aria-label={tr("web.panel.history")}>
            {history.runs.length ? history.runs.map((run) => {
              const runStatus = String(run.status ?? "");
              const at = stamp(run.started_at) ?? stamp(run.created_at);
              return <li key={String(run.id ?? run.run_id)}><Tag tone={runTone(runStatus)}>{statusLabel(tr, runStatus)}</Tag>{at && <span className="cap-row-time">{shortTime(at)}</span>}</li>;
            }) : <li className="empty-small">{tr("web.panel.no_runs")}</li>}
          </ul>}
        </Row>;
      })}</ul> : !formOpen && notice.tone !== "busy" && <EmptyState icon={<Clock size={22} />} title={tr("web.panel.no_schedules")}>{tr("web.panel.schedules_hint")}</EmptyState>}
    </div>}
  </CapabilityPage>;
}

export function SettingsPage({
  theme, inspectorOpen, save, projectId = null,
}: {
  theme: "dark" | "light";
  inspectorOpen: boolean;
  projectId?: string | null;
  save: (patch: { theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string }) => Promise<void>;
}) {
  const tr = useT();
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const apply = async (patch: { theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string }) => {
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await save(patch); setNotice({ tone: "ok", text: tr("web.saved") }); }
    catch (error) { setNotice(noteOf(error)); }
  };
  return <CapabilityPage id="settings-title" title={tr("web.settings")} subtitle={tr("web.settings_hint")} notice={<PageNotice notice={notice} />}>
    <section className="settings-group">
      <h3>{tr("web.panel.appearance")}</h3>
      <div className="settings-row">
        <span className="settings-label">{tr("web.theme")}</span>
        <Segmented label={tr("web.theme")} value={theme} onChange={(value) => { apply({ theme: value }).catch(() => undefined); }} options={[{ value: "light", label: tr("web.theme.light") }, { value: "dark", label: tr("web.theme.dark") }]} />
      </div>
      <div className="settings-row">
        <div><span className="settings-label">{tr("web.language_label")}</span><p className="hint">{tr("web.language_note")}</p></div>
        <Segmented label={tr("web.language_label")} value={getLocale()} onChange={(value) => { apply({ interface_language: value }).catch(() => undefined); }} options={[{ value: "en", label: "English" }, { value: "zh-CN", label: "中文" }]} />
      </div>
    </section>
    <section className="settings-group">
      <h3>{tr("web.panel.layout")}</h3>
      <div className="settings-row">
        <span className="settings-label">{tr("web.inspector_open")}</span>
        <Switch label={tr("web.inspector_open")} checked={inspectorOpen} onChange={(next) => { apply({ inspector_open: next }).catch(() => undefined); }} />
      </div>
    </section>
    <RuntimeSettings projectId={projectId} />
  </CapabilityPage>;
}
