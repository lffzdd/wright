import { useEffect, useState } from "react";
import { api } from "../api";
import { getLocale, useT } from "../i18n";

type Notice = { tone: "idle" | "busy" | "ok" | "error"; text: string };

function noteOf(error: unknown): Notice {
  return { tone: "error", text: error instanceof Error ? error.message : String(error) };
}

export function SearchDialog({
  sessionId, projectId, close, openFile,
}: {
  sessionId: string | null;
  projectId: string | null;
  close: () => void;
  openFile: (path: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("file");
  const [results, setResults] = useState<Array<Record<string, unknown>>>([]);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const run = async () => {
    if (!query.trim()) { setNotice({ tone: "error", text: "Enter a search query." }); return; }
    setNotice({ tone: "busy", text: "Searching…" });
    try {
      const found = sessionId
        ? await api.search(sessionId, query.trim(), kind)
        : projectId
          ? await api.workspaceSearch(projectId, query.trim(), kind)
          : null;
      if (!found) { setNotice({ tone: "error", text: "Register a workspace before searching." }); return; }
      setResults(found.results);
      setNotice({ tone: "ok", text: found.results.length ? `${found.results.length} results` : "No matches." });
    } catch (error) { setNotice(noteOf(error)); }
  };
  return <div className="dialog-backdrop"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="search-title">
    <h2 id="search-title">Search</h2>
    <div className="row-actions">
      <input aria-label="Search query" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") run(); if (event.key === "Escape") close(); }} />
      <select aria-label="Search kind" value={kind} onChange={(event) => setKind(event.target.value)}>
        <option value="file">Files</option>
        <option value="task">Tasks</option>
        <option value="symbol">Symbols</option>
      </select>
      <button type="button" className="button primary" onClick={() => run()}>{notice.tone === "busy" ? "Searching…" : "Search"}</button>
      <button type="button" className="button" onClick={close}>Close</button>
    </div>
    {notice.text && <p role={notice.tone === "error" ? "alert" : "status"} className={notice.tone === "error" ? "form-error" : "hint"}>{notice.text}</p>}
    <div className="change-list">{results.map((item, index) => {
      const path = String(item.path ?? item.name ?? item.title ?? item.id ?? index);
      const line = item.line ? `:${item.line}` : "";
      return <button type="button" key={`${path}-${index}`} onClick={() => { if (item.path) openFile(String(item.path)); }}>{item.kind ? `${item.kind} ` : ""}{path}{line}</button>;
    })}</div>
  </section></div>;
}

export function MemoryDialog({ sessionId, close }: { sessionId: string | null; close: () => void }) {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [section, setSection] = useState("human_profile");
  const [content, setContent] = useState("");
  const [name, setName] = useState("");
  const [memory, setMemory] = useState("");
  const load = async () => {
    if (!sessionId) return;
    setNotice({ tone: "busy", text: "Loading memory…" });
    try {
      setData(await api.memory(sessionId));
      setNotice({ tone: "ok", text: "Memory loaded." });
    } catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { load().catch(() => undefined); }, [sessionId]);
  const core = (data?.core ?? null) as Record<string, unknown> | null;
  const semantic = (data?.semantic as Array<Record<string, unknown>> | undefined) ?? [];
  const episodes = (data?.episodes as Array<Record<string, unknown>> | undefined) ?? [];
  return <div className="dialog-backdrop"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="memory-title">
    <h2 id="memory-title">Memory</h2>
    {!sessionId && <p className="empty-small">Open a task to view and edit memory for that project.</p>}
    {notice.text && <p role={notice.tone === "error" ? "alert" : "status"} className={notice.tone === "error" ? "form-error" : "hint"}>{notice.text}</p>}
    {core && <div>
      <p>Persona is read-only.</p>
      <pre className="tool-output">{String(core.persona ?? "")}</pre>
      <label>Section<select aria-label="Memory section" value={section} onChange={(event) => setSection(event.target.value)}><option value="human_profile">human_profile</option><option value="project_anchor">project_anchor</option></select></label>
      <textarea aria-label="Core memory" value={content} onChange={(event) => setContent(event.target.value)} rows={3} />
      <button type="button" className="button primary" onClick={async () => {
        if (!sessionId || !content.trim()) { setNotice({ tone: "error", text: "Content is required." }); return; }
        setNotice({ tone: "busy", text: "Saving core memory…" });
        try { await api.updateCore(sessionId, { section, content, mode: "replace" }); setContent(""); await load(); }
        catch (error) { setNotice(noteOf(error)); }
      }}>Save core</button>
    </div>}
    <h3>Semantic</h3>
    {semantic.map((item) => <div className="queue-item" key={String(item.id)}><span>{String(item.name)} · rev {String(item.revision ?? "")}</span>
      <button type="button" className="button" onClick={async () => {
        if (!sessionId) return;
        const next = window.prompt(`Replace ${item.name}`, String(item.content ?? ""));
        if (next == null) return;
        try { await api.updateSemantic(sessionId, String(item.id), { name: item.name, content: next, description: item.description ?? "", type: item.type ?? "project", scope: "project", expected_revision: item.revision }); await load(); }
        catch (error) { setNotice(noteOf(error)); }
      }}>Edit</button>
      <button type="button" className="button" onClick={async () => {
        if (!sessionId || !window.confirm(`Delete semantic memory ${item.name}?`)) return;
        try { await api.deleteSemantic(sessionId, String(item.id)); await load(); }
        catch (error) { setNotice(noteOf(error)); }
      }}>Delete</button>
    </div>)}
    {!semantic.length && <p className="empty-small">No semantic memories.</p>}
    <input aria-label="Semantic name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Name" />
    <textarea aria-label="Semantic content" value={memory} onChange={(event) => setMemory(event.target.value)} rows={3} placeholder="Content" />
    <button type="button" className="button primary" disabled={!sessionId} onClick={async () => {
      if (!sessionId || !name.trim() || !memory.trim()) { setNotice({ tone: "error", text: "Name and content are required." }); return; }
      setNotice({ tone: "busy", text: "Creating semantic memory…" });
      try { await api.createSemantic(sessionId, { name, content: memory, description: "", type: "project", scope: "project" }); setName(""); setMemory(""); await load(); }
      catch (error) { setNotice(noteOf(error)); }
    }}>Add semantic memory</button>
    <h3>Episodes</h3>
    {episodes.map((item) => <div className="queue-item" key={String(item.id)}><span>{String(item.title || item.id)}</span>
      <button type="button" className="button" onClick={async () => {
        if (!sessionId || !window.confirm(`Delete episode ${item.title || item.id}?`)) return;
        try { await api.deleteEpisode(sessionId, String(item.id)); await load(); }
        catch (error) { setNotice(noteOf(error)); }
      }}>Delete</button>
    </div>)}
    {!episodes.length && <p className="empty-small">No episodes.</p>}
    <button type="button" className="button" onClick={close}>Close</button>
  </section></div>;
}

export function RulesDialog({ sessionId, close }: { sessionId: string | null; close: () => void }) {
  const [rules, setRules] = useState<Array<Record<string, unknown>>>([]);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [skillId, setSkillId] = useState("");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState("");
  const [scope, setScope] = useState("project");
  const load = async () => {
    if (!sessionId) return;
    setNotice({ tone: "busy", text: "Loading rules…" });
    try {
      const result = await api.rules(sessionId);
      setRules(result.rules);
      setNotice({ tone: "ok", text: "Rules loaded. allowed-tools does not grant permission." });
    } catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { load().catch(() => undefined); }, [sessionId]);
  return <div className="dialog-backdrop"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="rules-title">
    <h2 id="rules-title">Rules</h2>
    {!sessionId && <p className="empty-small">Open a task to manage project and user rules.</p>}
    {notice.text && <p role={notice.tone === "error" ? "alert" : "status"} className={notice.tone === "error" ? "form-error" : "hint"}>{notice.text}</p>}
    {rules.map((rule) => <div className="queue-item" key={`${rule.scope}-${rule.id ?? rule.error}`}>
      <span>{String(rule.id ?? rule.error)} · {String(rule.scope ?? "")}{rule.shadowed ? " · shadowed" : ""}</span>
      {Boolean(rule.id) && <button type="button" className="button" onClick={async () => {
        if (!sessionId || !window.confirm(`Delete rule ${rule.id} in ${rule.scope}?`)) return;
        try { await api.deleteRule(sessionId, String(rule.id), String(rule.scope ?? "project")); await load(); }
        catch (error) { setNotice(noteOf(error)); }
      }}>Delete</button>}
    </div>)}
    {!rules.length && sessionId && <p className="empty-small">No rules.</p>}
    <input aria-label="Rule id" value={skillId} onChange={(event) => setSkillId(event.target.value)} placeholder="skill id" />
    <input aria-label="Rule description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="description" />
    <textarea aria-label="Rule body" value={body} onChange={(event) => setBody(event.target.value)} rows={4} placeholder="rule body" />
    <select aria-label="Rule scope" value={scope} onChange={(event) => setScope(event.target.value)}><option value="project">project</option><option value="user">user</option></select>
    <button type="button" className="button primary" disabled={!sessionId} onClick={async () => {
      if (!sessionId || !skillId.trim() || !body.trim()) { setNotice({ tone: "error", text: "Rule id and body are required." }); return; }
      if (!window.confirm(`Save rule ${skillId} in ${scope}? Replacing an existing file requires confirmation.`)) return;
      setNotice({ tone: "busy", text: "Saving rule…" });
      try { await api.saveRule(sessionId, skillId.trim(), { scope, description, body, confirm: true }); setSkillId(""); setDescription(""); setBody(""); await load(); }
      catch (error) { setNotice(noteOf(error)); }
    }}>Save rule</button>
    <button type="button" className="button" onClick={close}>Close</button>
  </section></div>;
}

export function SchedulesDialog({ sessionId, projectId, close }: { sessionId: string | null; projectId: string | null; close: () => void }) {
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [runs, setRuns] = useState<Array<Record<string, unknown>>>([]);
  const [notice, setNotice] = useState<Notice>({ tone: "idle", text: "" });
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [every, setEvery] = useState("3600");
  const [editing, setEditing] = useState<string | null>(null);
  const load = async () => {
    if (!projectId) return;
    setNotice({ tone: "busy", text: "Loading schedules…" });
    try { setRows(await api.projectSchedules(projectId)); setNotice({ tone: "ok", text: "Schedules loaded." }); }
    catch (error) { setNotice(noteOf(error)); }
  };
  useEffect(() => { load().catch(() => undefined); }, [projectId]);
  const act = async (id: string, action: string) => {
    if (!projectId) return;
    if (action === "delete" && !window.confirm(`Cancel schedule ${id}? History is kept when runs exist.`)) return;
    setNotice({ tone: "busy", text: `${action}…` });
    try { await api.scheduleAction(projectId, id, action, action === "delete" ? { confirm: true } : {}); await load(); }
    catch (error) { setNotice(noteOf(error)); }
  };
  return <div className="dialog-backdrop"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="schedule-title">
    <h2 id="schedule-title">Schedules</h2>
    {!projectId && <p className="empty-small">No project is selected.</p>}
    {notice.text && <p role={notice.tone === "error" ? "alert" : "status"} className={notice.tone === "error" ? "form-error" : "hint"}>{notice.text}</p>}
    {rows.map((row) => <div className="queue-item" key={String(row.id)}>
      <span>{String(row.name ?? row.id)} · {String(row.status ?? "")}</span>
      <button type="button" className="button" onClick={() => { setEditing(String(row.id)); setName(String(row.name ?? "")); setPrompt(String(row.prompt ?? "")); const trigger = row.trigger as { every_seconds?: number } | undefined; if (trigger?.every_seconds) setEvery(String(trigger.every_seconds)); }}>Edit</button>
      <button type="button" className="button" onClick={() => act(String(row.id), "pause")}>Pause</button>
      <button type="button" className="button" onClick={() => act(String(row.id), "resume")}>Resume</button>
      <button type="button" className="button" onClick={async () => {
        if (!sessionId) { setNotice({ tone: "error", text: "Open the schedule's task to read its run history." }); return; }
        try { setRuns(await api.scheduleRuns(sessionId, String(row.id))); }
        catch (error) { setNotice(noteOf(error)); }
      }}>History</button>
      <button type="button" className="button" onClick={() => act(String(row.id), "delete")}>Delete</button>
    </div>)}
    {!rows.length && <p className="empty-small">No schedules.</p>}
    {runs.map((run) => <p key={String(run.id ?? run.run_id)} className="hint">{String(run.status ?? "")} {String(run.id ?? run.run_id ?? "")}</p>)}
    <input aria-label="Schedule name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Name" />
    <textarea aria-label="Schedule prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={3} placeholder="Prompt" />
    <input aria-label="Interval seconds" value={every} onChange={(event) => setEvery(event.target.value)} />
    <button type="button" className="button primary" onClick={async () => {
      const seconds = Number(every);
      if (!name.trim() || !prompt.trim() || !Number.isFinite(seconds) || seconds <= 0) { setNotice({ tone: "error", text: "Name, prompt, and a positive interval are required." }); return; }
      setNotice({ tone: "busy", text: editing ? "Saving schedule…" : "Creating schedule…" });
      try {
        if (editing) {
          if (!projectId) return;
          await api.scheduleAction(projectId, editing, "update", { name, prompt, trigger: { type: "interval", every_seconds: seconds } });
          setEditing(null);
        } else {
          if (!sessionId) { setNotice({ tone: "error", text: "Open a task before creating a schedule. Pause, edit, and delete still work for this project." }); return; }
          await api.createSchedule(sessionId, { name, prompt, trigger: { type: "interval", every_seconds: seconds }, recovery_policy: "manual" });
        }
        setName(""); setPrompt(""); await load();
      } catch (error) { setNotice(noteOf(error)); }
    }}>{editing ? "Save schedule" : "Create schedule"}</button>
    <button type="button" className="button" onClick={close}>Close</button>
  </section></div>;
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
  return <div className="dialog-backdrop"><section className="dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
    <h2 id="settings-title">{tr("web.settings")}</h2>
    {notice.text && <p role={notice.tone === "error" ? "alert" : "status"} className={notice.tone === "error" ? "form-error" : "hint"}>{notice.text}</p>}
    <label>{tr("web.theme")}<select aria-label={tr("web.theme")} value={theme} onChange={(event) => apply({ theme: event.target.value as "dark" | "light" })}><option value="dark">{tr("web.theme.dark")}</option><option value="light">{tr("web.theme.light")}</option></select></label>
    <label>{tr("web.language_label")}<select aria-label={tr("web.language_label")} title={tr("web.language_note")} value={getLocale()} onChange={(event) => apply({ interface_language: event.target.value })}><option value="en">EN</option><option value="zh-CN">中文</option></select></label>
    <label><input type="checkbox" checked={inspectorOpen} onChange={(event) => apply({ inspector_open: event.target.checked })} /> {tr("web.inspector_open")}</label>
    <p className="hint">{tr("web.settings_hint")}</p>
    <button type="button" className="button" onClick={close}>{tr("web.close")}</button>
  </section></div>;
}
