import { useEffect, useRef, useState, type ReactNode } from "react";
import { confirmDialog, promptDialog } from "../ui/dialogs";
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
  const generation = useRef(0);
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
  return <Panel id="search-title" title={tr("web.panel.search")} close={close}>
    <div className="search-row">
      <input autoFocus aria-label="Search query" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.nativeEvent.isComposing) run(); }} placeholder={tr("web.search")} />
      <select aria-label="Search kind" value={kind} onChange={(event) => setKind(event.target.value)}>
        <option value="file">{tr("web.panel.kind_file")}</option>
        <option value="content">{tr("web.panel.kind_content")}</option>
        <option value="task">{tr("web.panel.kind_task")}</option>
        <option value="symbol">{tr("web.panel.kind_symbol")}</option>
      </select>
      <button type="button" className="button primary" onClick={() => run()}>{notice.tone === "busy" ? tr("web.panel.searching") : tr("web.panel.search")}</button>
    </div>
    <PanelNotice notice={notice} />
    <div className="change-list">{results.map((item, index) => {
      const path = String(item.path ?? item.name ?? item.title ?? item.id ?? index);
      const line = item.line ? `:${item.line}` : "";
      return <button type="button" key={`${path}-${index}`} onClick={() => activate(item)}>{item.kind ? `${item.kind} ` : ""}{path}{line}{typeof item.text === "string" && <small className="search-snippet">{item.text.trim()}</small>}</button>;
    })}</div>
    {preview && <section aria-label="File preview"><h3>{preview.path}</h3><pre className="term-body">{preview.content || tr("web.empty_file")}</pre></section>}
  </Panel>;
}

export function MemoryDialog({ sessionId, close }: { sessionId: string | null; close: () => void }) {
  const tr = useT();
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [section, setSection] = useState("human_profile");
  const [content, setContent] = useState("");
  const [name, setName] = useState("");
  const [memory, setMemory] = useState("");
  const load = async () => {
    if (!sessionId) return;
    setNotice({ tone: "busy", text: tr("web.panel.loading") });
    try {
      setData(await api.memory(sessionId));
      setNotice({ tone: "idle", text: "" });
    } catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { load().catch(() => undefined); }, [sessionId]);
  const core = (data?.core ?? null) as Record<string, unknown> | null;
  const semantic = (data?.semantic as Array<Record<string, unknown>> | undefined) ?? [];
  const episodes = (data?.episodes as Array<Record<string, unknown>> | undefined) ?? [];
  return <Panel id="memory-title" title={tr("web.panel.memory")} close={close}>
    {!sessionId && <p className="empty-small">{tr("web.panel.memory_needs_task")}</p>}
    <PanelNotice notice={notice} />
    {core && <div className="dialog-section">
      <h3>{tr("web.panel.core")}</h3>
      <p className="hint">{tr("web.panel.persona_read_only")}</p>
      <pre className="tool-output">{String(core.persona ?? "")}</pre>
      <Field label={tr("web.panel.section")}><select aria-label="Memory section" value={section} onChange={(event) => setSection(event.target.value)}><option value="human_profile">{tr("web.panel.human_profile")}</option><option value="project_anchor">{tr("web.panel.project_anchor")}</option></select></Field>
      <Field label={tr("web.panel.content")}><textarea aria-label="Core memory" value={content} onChange={(event) => setContent(event.target.value)} rows={3} /></Field>
      <div className="dialog-actions"><button type="button" className="button primary" onClick={async () => {
        if (!sessionId || !content.trim()) { setNotice({ tone: "error", text: tr("web.panel.content_required") }); return; }
        setNotice({ tone: "busy", text: tr("web.saving") });
        try { await api.updateCore(sessionId, { section, content, mode: "replace" }); setContent(""); await load(); }
        catch (error) { setNotice(noteOf(error)); }
      }}>{tr("web.panel.save_core")}</button></div>
    </div>}
    <div className="dialog-section">
      <h3>{tr("web.panel.semantic")}</h3>
      {semantic.map((item) => <div className="queue-item" key={String(item.id)}><span>{String(item.name)} · rev {String(item.revision ?? "")}</span>
        <button type="button" className="button" onClick={async () => {
          if (!sessionId) return;
          const next = await promptDialog({ title: tr("web.panel.replace_memory", { name: String(item.name) }), initial: String(item.content ?? ""), multiline: true });
          if (next == null) return;
          try { await api.updateSemantic(sessionId, String(item.id), { name: item.name, content: next, description: item.description ?? "", type: item.type ?? "project", scope: "project", expected_revision: item.revision }); await load(); }
          catch (error) { setNotice(noteOf(error)); }
        }}>{tr("web.panel.edit")}</button>
        <button type="button" className="button" onClick={async () => {
          if (!sessionId || !await confirmDialog({ title: tr("web.panel.delete_memory_confirm", { name: String(item.name) }), danger: true })) return;
          try { await api.deleteSemantic(sessionId, String(item.id)); await load(); }
          catch (error) { setNotice(noteOf(error)); }
        }}>{tr("web.panel.delete")}</button>
      </div>)}
      {!semantic.length && <p className="empty-small">{tr("web.panel.no_semantic")}</p>}
      <Field label={tr("web.panel.name")}><input aria-label="Semantic name" value={name} onChange={(event) => setName(event.target.value)} /></Field>
      <Field label={tr("web.panel.content")}><textarea aria-label="Semantic content" value={memory} onChange={(event) => setMemory(event.target.value)} rows={3} /></Field>
      <div className="dialog-actions"><button type="button" className="button primary" disabled={!sessionId} onClick={async () => {
        if (!sessionId || !name.trim() || !memory.trim()) { setNotice({ tone: "error", text: tr("web.panel.name_content_required") }); return; }
        setNotice({ tone: "busy", text: tr("web.saving") });
        try { await api.createSemantic(sessionId, { name, content: memory, description: "", type: "project", scope: "project" }); setName(""); setMemory(""); await load(); }
        catch (error) { setNotice(noteOf(error)); }
      }}>{tr("web.panel.add_memory")}</button></div>
    </div>
    <div className="dialog-section">
      <h3>{tr("web.panel.episodes")}</h3>
      {episodes.map((item) => <div className="queue-item" key={String(item.id)}><span>{String(item.title || item.id)}</span>
        <button type="button" className="button" onClick={async () => {
          if (!sessionId || !await confirmDialog({ title: tr("web.panel.delete_episode_confirm", { name: String(item.title || item.id) }), danger: true })) return;
          try { await api.deleteEpisode(sessionId, String(item.id)); await load(); }
          catch (error) { setNotice(noteOf(error)); }
        }}>{tr("web.panel.delete")}</button>
      </div>)}
      {!episodes.length && <p className="empty-small">{tr("web.panel.no_episodes")}</p>}
    </div>
  </Panel>;
}

export function RulesDialog({ sessionId, close }: { sessionId: string | null; close: () => void }) {
  const tr = useT();
  const [rules, setRules] = useState<Array<Record<string, unknown>>>([]);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [skillId, setSkillId] = useState("");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState("");
  const [scope, setScope] = useState("project");
  const load = async () => {
    if (!sessionId) return;
    setNotice({ tone: "busy", text: tr("web.panel.loading") });
    try {
      const result = await api.rules(sessionId);
      setRules(result.rules);
      setNotice({ tone: "idle", text: "" });
    } catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { load().catch(() => undefined); }, [sessionId]);
  return <Panel id="rules-title" title={tr("web.panel.rules")} close={close}>
    <p className="hint">{sessionId ? tr("web.panel.rules_hint") : tr("web.panel.rules_needs_task")}</p>
    <PanelNotice notice={notice} />
    {Boolean(sessionId) && <div className="dialog-section">
      <h3>{tr("web.panel.saved_rules")}</h3>
      {rules.map((rule) => <div className="queue-item" key={`${rule.scope}-${rule.id ?? rule.error}`}>
        <span>{String(rule.id ?? rule.error)} · {String(rule.scope ?? "")}{rule.shadowed ? ` · ${tr("web.panel.shadowed")}` : ""}</span>
        {Boolean(rule.id) && <button type="button" className="button" onClick={async () => {
          if (!sessionId || !await confirmDialog({ title: tr("web.panel.delete_rule_confirm", { name: String(rule.id), scope: String(rule.scope ?? "project") }), danger: true })) return;
          try { await api.deleteRule(sessionId, String(rule.id), String(rule.scope ?? "project")); await load(); }
          catch (error) { setNotice(noteOf(error)); }
        }}>{tr("web.panel.delete")}</button>}
      </div>)}
      {!rules.length && <p className="empty-small">{tr("web.panel.no_rules")}</p>}
    </div>}
    <div className="dialog-section">
      <h3>{tr("web.panel.add_rule")}</h3>
      <Field label={tr("web.panel.rule_id")}><input aria-label="Rule id" value={skillId} onChange={(event) => setSkillId(event.target.value)} /></Field>
      <Field label={tr("web.panel.description")}><input aria-label="Rule description" value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
      <Field label={tr("web.panel.rule_body")}><textarea aria-label="Rule body" value={body} onChange={(event) => setBody(event.target.value)} rows={4} /></Field>
      <Field label={tr("web.panel.scope")}><select aria-label="Rule scope" value={scope} onChange={(event) => setScope(event.target.value)}><option value="project">{tr("web.panel.scope_project")}</option><option value="user">{tr("web.panel.scope_user")}</option></select></Field>
      <div className="dialog-actions"><button type="button" className="button primary" disabled={!sessionId} onClick={async () => {
        if (!sessionId || !skillId.trim() || !body.trim()) { setNotice({ tone: "error", text: tr("web.panel.rule_required") }); return; }
        if (!await confirmDialog({ title: tr("web.panel.save_rule_confirm", { name: skillId, scope }) })) return;
        setNotice({ tone: "busy", text: tr("web.saving") });
        try { await api.saveRule(sessionId, skillId.trim(), { scope, description, body, confirm: true }); setSkillId(""); setDescription(""); setBody(""); await load(); }
        catch (error) { setNotice(noteOf(error)); }
      }}>{tr("web.panel.save_rule")}</button></div>
    </div>
  </Panel>;
}

export function SchedulesDialog({ sessionId, projectId, close }: { sessionId: string | null; projectId: string | null; close: () => void }) {
  const tr = useT();
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [runs, setRuns] = useState<Array<Record<string, unknown>>>([]);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [every, setEvery] = useState("3600");
  const [editing, setEditing] = useState<string | null>(null);
  const load = async () => {
    if (!projectId) return;
    setNotice({ tone: "busy", text: tr("web.panel.loading") });
    try { setRows(await api.projectSchedules(projectId)); setNotice({ tone: "idle", text: "" }); }
    catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { load().catch(() => undefined); }, [projectId]);
  const act = async (id: string, action: string) => {
    if (!projectId) return;
    if (action === "delete" && !await confirmDialog({ title: tr("web.panel.delete_schedule_confirm", { name: id }), danger: true })) return;
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await api.scheduleAction(projectId, id, action, action === "delete" ? { confirm: true } : {}); await load(); }
    catch (error) { setNotice(noteOf(error)); }
  };
  return <Panel id="schedule-title" title={tr("web.panel.schedules")} close={close}>
    {!projectId && <p className="empty-small">{tr("web.panel.no_project")}</p>}
    <PanelNotice notice={notice} />
    <div className="dialog-section">
      {rows.map((row) => <div className="queue-item" key={String(row.id)}>
        <span>{String(row.name ?? row.id)} · {String(row.status ?? "")}</span>
        <button type="button" className="button" onClick={() => { setEditing(String(row.id)); setName(String(row.name ?? "")); setPrompt(String(row.prompt ?? "")); const trigger = row.trigger as { every_seconds?: number } | undefined; if (trigger?.every_seconds) setEvery(String(trigger.every_seconds)); }}>{tr("web.panel.edit")}</button>
        <button type="button" className="button" onClick={() => act(String(row.id), "pause")}>{tr("web.panel.pause")}</button>
        <button type="button" className="button" onClick={() => act(String(row.id), "resume")}>{tr("web.panel.resume")}</button>
        <button type="button" className="button" onClick={async () => {
          if (!sessionId) { setNotice({ tone: "error", text: tr("web.panel.history_needs_task") }); return; }
          try { setRuns(await api.scheduleRuns(sessionId, String(row.id))); }
          catch (error) { setNotice(noteOf(error)); }
        }}>{tr("web.panel.history")}</button>
        <button type="button" className="button" onClick={() => act(String(row.id), "delete")}>{tr("web.panel.delete")}</button>
      </div>)}
      {!rows.length && <p className="empty-small">{tr("web.panel.no_schedules")}</p>}
      {runs.map((run) => <p key={String(run.id ?? run.run_id)} className="hint">{String(run.status ?? "")} {String(run.id ?? run.run_id ?? "")}</p>)}
    </div>
    <div className="dialog-section">
      <h3>{tr(editing ? "web.panel.edit_schedule" : "web.panel.add_schedule")}</h3>
      <Field label={tr("web.panel.name")}><input aria-label="Schedule name" value={name} onChange={(event) => setName(event.target.value)} /></Field>
      <Field label={tr("web.panel.prompt")}><textarea aria-label="Schedule prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={3} /></Field>
      <Field label={tr("web.panel.interval")}><input aria-label="Interval seconds" type="number" min={1} value={every} onChange={(event) => setEvery(event.target.value)} /></Field>
      <div className="dialog-actions"><button type="button" className="button primary" onClick={async () => {
        const seconds = Number(every);
        if (!name.trim() || !prompt.trim() || !Number.isFinite(seconds) || seconds <= 0) { setNotice({ tone: "error", text: tr("web.panel.schedule_required") }); return; }
        setNotice({ tone: "busy", text: tr("web.saving") });
        try {
          if (editing) {
            if (!projectId) return;
            await api.scheduleAction(projectId, editing, "update", { name, prompt, trigger: { type: "interval", every_seconds: seconds } });
            setEditing(null);
          } else {
            if (!sessionId) { setNotice({ tone: "error", text: tr("web.panel.schedule_needs_task") }); return; }
            await api.createSchedule(sessionId, { name, prompt, trigger: { type: "interval", every_seconds: seconds }, recovery_policy: "manual" });
          }
          setName(""); setPrompt(""); await load();
        } catch (error) { setNotice(noteOf(error)); }
      }}>{tr(editing ? "web.panel.save_schedule" : "web.panel.create_schedule")}</button></div>
    </div>
  </Panel>;
}

export function SettingsDialog({
  theme, inspectorOpen, close, save,
}: {
  theme: "dark" | "light";
  inspectorOpen: boolean;
  close: () => void;
  save: (patch: { theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string }) => Promise<void>;
}) {
  const tr = useT();
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const apply = async (patch: { theme?: "dark" | "light"; inspector_open?: boolean; interface_language?: string }) => {
    setNotice({ tone: "busy", text: tr("web.saving") });
    try { await save(patch); setNotice({ tone: "ok", text: tr("web.saved") }); }
    catch (error) { setNotice(noteOf(error)); }
  };
  return <Panel id="settings-title" title={tr("web.settings")} close={close}>
    <PanelNotice notice={notice} />
    <div className="dialog-section">
      <Field label={tr("web.theme")}><select aria-label={tr("web.theme")} value={theme} onChange={(event) => apply({ theme: event.target.value as "dark" | "light" })}><option value="dark">{tr("web.theme.dark")}</option><option value="light">{tr("web.theme.light")}</option></select></Field>
      <Field label={tr("web.language_label")}><select aria-label={tr("web.language_label")} title={tr("web.language_note")} value={getLocale()} onChange={(event) => apply({ interface_language: event.target.value })}><option value="en">EN</option><option value="zh-CN">中文</option></select></Field>
      <label className="dialog-check"><input type="checkbox" checked={inspectorOpen} onChange={(event) => apply({ inspector_open: event.target.checked })} /><span>{tr("web.inspector_open")}</span></label>
      <p className="hint">{tr("web.settings_hint")}</p>
    </div>
  </Panel>;
}
