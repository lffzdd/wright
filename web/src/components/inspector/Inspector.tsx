import { Check, Circle, Folder, X } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { parseDiff } from "../../diff";
import { useT } from "../../i18n";
import type { Grants, PlanStep, ViewState, WaitReason } from "../../types";
import { PermissionsPanel } from "../permissions/permissions";
import { ChangesPanel } from "./ChangesPanel";

type InspectorTab = "task" | "files" | "changes" | "permissions" | "context";

/** A path relative to the project, or null when it lies outside it. */
/** Time of day for today, otherwise a short date and time; the full stamp stays in the tooltip. */
export function shortTime(seconds: number, now = new Date()): string {
  const date = new Date(seconds * 1000);
  const sameDay = date.toDateString() === now.toDateString();
  return sameDay
    ? date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function projectRelative(path: string, root?: string): string | null {
  const normalized = path.replace(/\\/g, "/");
  const absolute = normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized);
  if (!absolute) return normalized.split("/").includes("..") ? null : normalized;
  const base = (root ?? "").replace(/\\/g, "/").replace(/\/+$/, "");
  if (base && normalized.startsWith(`${base}/`)) return normalized.slice(base.length + 1);
  return null;
}

export function Inspector({ state, sessionId, open, narrow = false, focusPath = null, focusRequest = 0, close, onResume, onFeedback }: { state: ViewState; sessionId: string; open: boolean; narrow?: boolean; focusPath?: string | null; focusRequest?: number; close: () => void; onResume?: () => Promise<unknown>; onFeedback?: (feedback: string) => void }) {
  const tr = useT();
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open || !narrow) return;
    const previous = document.activeElement as HTMLElement | null;
    panelRef.current?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')?.focus();
    return () => {
      if (previous?.isConnected) previous.focus();
      else document.querySelector<HTMLButtonElement>('button[aria-controls="workspace-inspector"]')?.focus();
    };
  }, [open, narrow]);
  const [tab, setTab] = useState<InspectorTab>("task");
  const [changes, setChanges] = useState<Array<{ path: string; status: string }> | null>(null);
  const [changeCount, setChangeCount] = useState(0);
  const [files, setFiles] = useState<Array<{ name: string; path: string; kind: string }>>([]);
  const [directory, setDirectory] = useState("");
  const [browseWorkspace, setBrowseWorkspace] = useState(false);
  const [recentFileMeta, setRecentFileMeta] = useState<Record<string, { size?: number; additions?: number; deletions?: number }>>({});
  const [filePath, setFilePath] = useState("");
  const previewRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!filePath || tab !== "files") return;
    const body = panelRef.current?.querySelector<HTMLElement>(".inspector-body");
    if (body) body.scrollTop = 0;
    previewRef.current?.focus();
  }, [filePath, tab, focusRequest]);
  const [fileText, setFileText] = useState<string | null>(null);
  const [filesLoading, setFilesLoading] = useState(false);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState("");
  const [permissionsError, setPermissionsError] = useState("");
  const historyOnly = !state.session.active;
  const [resumeError, setResumeError] = useState("");
  const [resumeAttempt, setResumeAttempt] = useState(0);
  const needsRuntime = historyOnly && (tab === "changes" || tab === "permissions") && state.session.recoverable !== false;
  useEffect(() => {
    if (!needsRuntime || !onResume) return;
    setResumeError("");
    onResume().catch((error) => setResumeError(error instanceof Error ? error.message : String(error)));
  }, [needsRuntime, resumeAttempt]);
  const resumeStatus = onResume && state.session.recoverable !== false ? <>
    {resumeError ? <><p className="form-error" role="alert">{resumeError}</p><button type="button" className="text-button" onClick={() => setResumeAttempt((value) => value + 1)}>{tr("web.retry")}</button></>
      : <p className="hint" role="status">{tr("web.resuming_session")}</p>}
  </> : null;
  const [historyWorkspace, setHistoryWorkspace] = useState<{ sessionId: string; root: string; projectId: string } | null>(null);
  const historyProjectId = historyWorkspace?.sessionId === sessionId && historyWorkspace.root === state.session.execution_root ? historyWorkspace.projectId : null;
  const canReadFiles = !historyOnly || !!historyProjectId;
  useEffect(() => {
    let current = true;
    setHistoryWorkspace(null);
    const root = state.session.execution_root;
    if (historyOnly && root) {
      api.workspaces().then((result) => {
        if (!current) return;
        const project = result.projects.find((item) => item.exists !== false && item.root.replace(/\\/g, "/").replace(/\/+$/, "") === root.replace(/\\/g, "/").replace(/\/+$/, ""));
        if (project) setHistoryWorkspace({ sessionId, root, projectId: project.project_id });
      }).catch(() => undefined);
    }
    return () => { current = false; };
  }, [sessionId, historyOnly, state.session.execution_root]);
  const readTree = useCallback((path: string) => historyOnly && historyProjectId ? api.workspaceTree(historyProjectId, path) : api.tree(sessionId, path), [sessionId, historyOnly, historyProjectId]);
  const readFile = useCallback((path: string) => historyOnly && historyProjectId ? api.workspaceFile(historyProjectId, path) : api.file(sessionId, path), [sessionId, historyOnly, historyProjectId]);
  const [grants, setGrants] = useState<Grants | null>(null);
  const [clockNow, setClockNow] = useState(Date.now);
  const execution = state.session.execution ?? state.session.status;
  const livePlan = state.session.active && state.connection === "connected" && ["running", "waiting_for_input"].includes(execution);
  useEffect(() => {
    setClockNow(Date.now());
    if (!livePlan || !state.plan.observed_at || !state.plan.steps?.some((step) => step.started_at !== null && step.ended_at === null)) return;
    const timer = window.setInterval(() => setClockNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [livePlan, state.plan]);
  const [panelError, setPanelError] = useState("");
  const changesGeneration = useRef(0);
  useEffect(() => {
    ++changesGeneration.current;
    setChanges(null); setChangeCount(0); setPermissionsError(""); setFileError("");
    setDirectory(""); setBrowseWorkspace(false); setRecentFileMeta({}); setFilePath(""); setFileText(null); setFileLoading(false); setFilesLoading(false); setFiles([]); setPanelError("");
    setGrants(null);
    return () => { ++changesGeneration.current; };
  }, [sessionId, historyOnly]);
  const refresh = useCallback(() => {
    if (historyOnly) return;
    const request = ++changesGeneration.current;
    api.changes(sessionId).then((result) => {
      if (changesGeneration.current !== request) return;
      setChanges(result.changes); setChangeCount(result.changes.length);
    }).catch(() => { if (changesGeneration.current === request) { setChanges(null); setChangeCount(0); } });
  }, [sessionId, historyOnly]);
  useEffect(() => { if (tab !== "changes") refresh(); else ++changesGeneration.current; }, [refresh, state.session.status, state.connection, tab]);
  useEffect(() => {
    let current = true;
    setPanelError("");
    if (tab === "files" && canReadFiles) {
      if (browseWorkspace) {
        setFilesLoading(true);
        setFiles([]);
        readTree(directory).then((result) => { if (current) { setFiles(result.entries); setFilesLoading(false); } }).catch((error) => { if (current) { setPanelError(String(error)); setFilesLoading(false); } });
      }
    }
    return () => { current = false; };
  }, [tab, sessionId, directory, browseWorkspace, canReadFiles, readTree]);
  useEffect(() => {
    let current = true;
    if (tab === "permissions" && !historyOnly) {
      setPermissionsError("");
      api.grants(sessionId).then((result) => {
        if (current) { setGrants(result); setPermissionsError(""); }
      }).catch((error) => { if (current) setPermissionsError(String(error)); });
    }
    return () => { current = false; };
  }, [tab, sessionId, historyOnly, state.session.permission_mode, state.session.interaction_mode, state.permissions_revision, state.connection, state.pending_interactions]);
  useEffect(() => {
    let current = true;
    if (historyOnly) return;
    // Only files inside the project can be listed or diffed; asking about
    // /etc or another checkout just produces failed requests.
    const accessed = (state.accessed_files ?? []).filter((item) => item.access !== "list").slice(-8)
      .map((item) => ({ ...item, path: projectRelative(item.path, state.session.execution_root) }))
      .filter((item): item is typeof item & { path: string } => item.path !== null);
    const folders = [...new Set(accessed.map((item) => {
      const path = item.path;
      return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    }))];
    const writePaths = new Set((changes ?? []).map((item) => item.path));
    const sizeRequest = Promise.all(folders.map((folder) => api.tree(sessionId, folder).catch(() => null)));
    const diffRequest = Promise.all(accessed.filter((item) => item.access === "write" && writePaths.has(item.path)).map(async (item) => {
      try {
        const result = await api.patch(sessionId, item.path);
        const { additions, deletions } = parseDiff(result.patch);
        return [item.path, { additions, deletions }] as const;
      } catch { return [item.path, {}] as const; }
    }));
    void Promise.all([sizeRequest, diffRequest]).then(([treeResults, diffResults]) => {
      if (!current) return;
      const metadata: Record<string, { size?: number; additions?: number; deletions?: number }> = {};
      for (const tree of treeResults) for (const entry of tree?.entries ?? []) {
        if (entry.kind === "file" && typeof entry.size === "number") metadata[entry.path] = { size: entry.size };
      }
      for (const [path, diff] of diffResults) metadata[path] = { ...metadata[path], ...diff };
      setRecentFileMeta(metadata);
    });
    return () => { current = false; };
  }, [sessionId, historyOnly, state.session.execution_root, JSON.stringify(state.accessed_files ?? []), changes]);
  useEffect(() => {
    if (!focusPath) return;
    setTab("files");
    setFilePath(projectRelative(focusPath, state.session.execution_root) ?? focusPath);
  }, [focusPath, sessionId, focusRequest]);
  useEffect(() => {
    let current = true;
    setFileText(null); setFileError(""); setFileLoading(false);
    if (filePath && canReadFiles) {
      const relative = projectRelative(filePath, state.session.execution_root);
      if (relative === null) { setFileError(tr("web.file_outside_workspace")); return; }
      setFileLoading(true);
      readFile(relative).then((file) => {
        if (current) { setFileText(file.binary ? tr("web.binary_file") : file.content); setFileLoading(false); }
      }).catch((error) => { if (current) { setFileError(String(error)); setFileLoading(false); } });
    }
    return () => { current = false; };
  }, [filePath, sessionId, canReadFiles, state.session.execution_root, readFile, tr]);
  const breakdown = state.context_breakdown;
  const tabs: InspectorTab[] = ["task", "files", "changes", "permissions", "context"];
  const steps = state.plan.steps ?? [];
  const accessedFiles = [...new Map((state.accessed_files ?? []).map((file) => [projectRelative(file.path, state.session.execution_root) ?? file.path, file])).values()];
  const completedSteps = steps.filter((step) => step.status === "completed").length;
  const currentStep = steps.findIndex((step) => ["in_progress", "running", "active"].includes(step.status));
  const tabCount = (item: InspectorTab) => item === "files" ? accessedFiles.length : item === "changes" ? changeCount : 0;
  const fileMeta = (path: string) => recentFileMeta[projectRelative(path, state.session.execution_root) ?? path] ?? {};
  const formatTokens = (value: number | null | undefined) => value == null ? tr("web.unknown") : new Intl.NumberFormat().format(value);
  const contextCategoryLabel = (id: string) => {
    const key = `web.context_category.${id}`;
    const translated = tr(key);
    return translated === key ? id.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()) : translated;
  };
  const stepMeta = (status: string, note?: string) => {
    const label = status.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
    const detail = note?.trim();
    if (!detail) return label;
    if (detail.toLowerCase().startsWith(label.toLowerCase())) return detail[0].toUpperCase() + detail.slice(1);
    return `${label} · ${detail}`;
  };
  const waitText = (wait: WaitReason) => {
    const label = tr(`web.plan_wait.${wait.kind}`);
    return wait.detail ? `${label} · ${wait.detail}` : label;
  };
  const stepTiming = (step: PlanStep) => {
    if (typeof step.elapsed_ms !== "number") return "";
    const extra = livePlan && step.ended_at === null && typeof state.plan.observed_at === "number"
      ? Math.max(0, clockNow - state.plan.observed_at * 1000) : 0;
    return tr("web.step_elapsed", { duration: `${((step.elapsed_ms + extra) / 1000).toFixed(1)}s` });
  };
  return <aside id="workspace-inspector" ref={panelRef} hidden={!open} role={narrow ? "dialog" : undefined} aria-modal={narrow || undefined} aria-label={tr("web.inspector")} className={`inspector ${open ? "" : "is-closed"} ${narrow ? "narrow-open" : ""}`} onKeyDown={(event) => {
    if (!open || !narrow || event.key !== "Tab") return;
    const controls = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]):not([tabindex="-1"]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]') ?? []);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}>
    <div className="inspector-tabs">
      <div className="inspector-tablist" role="tablist" aria-label={tr("web.inspector")}>
      {tabs.map((item, index) => <button key={item} id={`inspector-tab-${item}`} aria-controls={`inspector-panel-${item}`} tabIndex={tab === item ? 0 : -1} className={item === "permissions" && state.pending_interactions.length ? "pending-tab" : ""} role="tab" aria-selected={tab === item} onClick={() => setTab(item)} onKeyDown={(event) => {
        const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
        if (next === null) return;
        event.preventDefault(); setTab(tabs[next]);
        const button = panelRef.current?.querySelector<HTMLButtonElement>(`#inspector-tab-${tabs[next]}`);
        button?.focus(); button?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
      }}><span>{tr(`web.tab.${item}`)}</span>{tabCount(item) > 0 && <small>{`(${tabCount(item)})`}</small>}</button>)}
      </div>
      <button className="icon-button" onClick={close} aria-label={tr("web.close_inspector")} title={tr("web.close_inspector")}><X size={14} weight="bold" /></button>
    </div>
    {tab === "task" && <div className="inspector-body" role="tabpanel" id="inspector-panel-task" aria-labelledby="inspector-tab-task">
      <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.goal")}</span>{steps.length ? <span className="goal-progress-copy">{tr("web.steps_completed", { done: completedSteps, total: steps.length })}</span> : null}</div>
        {state.plan.objective || state.session.user_goal
          ? <p>{state.plan.objective || state.session.user_goal}</p>
          : <p className="empty-small">{tr("web.no_goal")}</p>}
        {steps.length ? <div className="progress"><i style={{ width: `${Math.round((completedSteps / steps.length) * 100)}%` }} /></div> : null}
      </section>
      {state.plan.wait_reason && <p className="hint plan-wait" role="status">{waitText(state.plan.wait_reason)}</p>}
      {steps.length > 0 && <section className="goal-card inspector-plan">
        <div className="card-kicker"><span>{tr("web.execution_plan")}</span>{steps.length ? <span>{currentStep >= 0 ? tr("web.step_active", { step: currentStep + 1 }) : `${completedSteps}/${steps.length}`}</span> : null}</div>
        <div className="step-list">{steps.map((step) => {
          const metadata = stepMeta(step.status, step.note);
          const timing = stepTiming(step);
          return <article key={step.id} className={`step-card ${step.status}`}>{step.status === "completed" ? <Check size={15} weight="bold" /> : ["in_progress", "running", "active"].includes(step.status) ? <span className="step-spinner" aria-hidden="true" /> : <Circle size={15} />}<div><strong title={step.title}>{step.title}</strong><small title={metadata}>{metadata}</small>{timing && <small>{timing}</small>}{typeof step.ended_at === "number" && <small title={new Date(step.ended_at * 1000).toLocaleString()}>{tr("web.step_ended", { time: shortTime(step.ended_at) })}</small>}{step.wait_reason && <small className="plan-wait" role="status">{waitText(step.wait_reason)}</small>}</div>{["in_progress", "running", "active"].includes(step.status) && <span className="current-step">{tr("web.current_step")}</span>}</article>;
        })}</div>
      </section>}
      {accessedFiles.length > 0 && <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.accessed_files")}</span>{accessedFiles.length ? <span>{tr("web.files_count", { count: accessedFiles.length })}</span> : null}</div>
        {accessedFiles.slice(-3).map((file) => {
          const metadata = fileMeta(file.path);
          const access = file.access === "write" && metadata.additions !== undefined ? `+${metadata.additions} -${metadata.deletions ?? 0}` : file.access === "write" ? "MOD" : file.access === "read" ? "READ" : (file.access || "TOUCH").toUpperCase();
          return <div className={`file-stat${file.access === "write" ? " changed" : ""}`} key={`${file.call_id}-${file.path}`}><span>{file.path}</span><span>{access}</span></div>;
        })}
      </section>}
      {(state.subagents ?? []).length > 0 && <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.subagents")}</span>{(state.subagents ?? []).length ? <span>{tr("web.spawned_count", { count: (state.subagents ?? []).length })}</span> : null}</div>
        {(state.subagents ?? []).map((agent) => <div className="subagent-row" key={agent.task_id}><span className="status-dot" /><span>{agent.task || agent.task_id}</span><span>{(agent.status || "unknown").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase())}</span></div>)}
      </section>}
      {!steps.length && !accessedFiles.length && !(state.subagents ?? []).length && <p className="hint inspector-task-empty">{tr("web.inspector_task_empty")}</p>}
    </div>}
    {tab === "files" && <div className="inspector-body" role="tabpanel" id="inspector-panel-files" aria-labelledby="inspector-tab-files">
      <div className="files-panel-head"><div className="section-label"><span>{tr("web.workspace_resources")}</span></div><button type="button" className="text-button" disabled={!canReadFiles} onClick={() => { if (filePath) { setFilePath(""); return; } setDirectory(""); setBrowseWorkspace((value) => !value); }}>{filePath ? tr("web.back_to_files") : browseWorkspace ? tr("web.recent_files") : tr("web.browse_workspace")}</button></div>
      {historyOnly && <p className="hint">{tr(canReadFiles ? "web.inspector_history_files_current" : "web.inspector_history_files")}</p>}
      {panelError && <p className="form-error" role="alert">{panelError}</p>}
      {!filePath && (browseWorkspace ? <>
        {directory && <button type="button" className="resource-row" onClick={() => { setFilePath(""); setDirectory(directory.split("/").slice(0, -1).join("/")); }}><Folder size={14} /><span>.. / {directory}</span></button>}
        {files.map((entry) => <button className="resource-row" key={entry.path} onClick={() => { setPanelError(""); if (entry.kind === "dir") { setFilePath(""); setDirectory(entry.path); } else setFilePath(entry.path); }}><b>{entry.kind === "dir" ? "DIR" : "FILE"}</b><span>{entry.path}</span></button>)}
        {filesLoading && <p className="hint" role="status">{tr("web.inspector_loading")}</p>}
        {!filesLoading && !panelError && !files.length && <p className="empty-small">{tr("web.dir_empty")}</p>}
      </> : <>
        <div className="accessed-file-list">{accessedFiles.map((file) => {
          const metadata = fileMeta(file.path);
          const label = file.access === "write" ? "MOD" : file.access === "read" ? "READ" : (file.access || "TOUCH").toUpperCase();
          const detail = file.access === "write" && (metadata.additions !== undefined || metadata.deletions !== undefined)
            ? `+${metadata.additions ?? 0} -${metadata.deletions ?? 0}`
            : typeof metadata.size === "number" ? `${(metadata.size / 1024).toFixed(1)} KB` : "";
          return <button type="button" className="accessed-file" disabled={!canReadFiles || file.access === "list"} key={`${file.call_id}-${file.path}`} onClick={() => { setPanelError(""); setFilePath(file.path); }}>
            <span className={`accessed-kind ${file.access === "write" ? "modified" : file.access === "read" ? "read" : "touched"}`}>{label}</span><span className="accessed-path" title={file.path}>{file.path}</span><span className={`accessed-detail${file.access === "write" ? " modified" : ""}`}>{detail}</span>
          </button>;
        })}</div>
        {!accessedFiles.length && <p className="empty-small">{tr("web.no_accessed_files")}</p>}
      </>)}
      {filePath && <section ref={previewRef} tabIndex={-1} className="inspector-file-preview" aria-label={tr("web.content_preview")}>
        <div className="files-panel-head"><strong className="mono" title={filePath}>{filePath}</strong><button type="button" className="icon-button" onClick={() => setFilePath("")} aria-label={`${tr("web.close")} ${filePath}`}><X size={14} /></button></div>
        {fileError && <p className="form-error" role="alert">{fileError}</p>}
        {fileLoading && <p className="hint" role="status">{tr("web.inspector_loading")}</p>}
        {fileText !== null && <pre className="term-body">{fileText || tr("web.empty_file")}</pre>}
      </section>}
    </div>}
    {tab === "changes" && <div className="inspector-body" role="tabpanel" id="inspector-panel-changes" aria-labelledby="inspector-tab-changes">
      {historyOnly ? resumeStatus ?? <p className="hint">{tr("web.inspector_history_changes")}</p> : <ChangesPanel sessionId={sessionId} state={state} onCount={setChangeCount} onWorkingChanges={setChanges} onFeedback={onFeedback} />}

    </div>}
    {tab === "permissions" && <div className="inspector-body" role="tabpanel" id="inspector-panel-permissions" aria-labelledby="inspector-tab-permissions">
      {historyOnly ? resumeStatus ?? <p className="hint">{tr("web.inspector_history_permissions")}</p> : <>
      {permissionsError && <p className="form-error" role="alert">{permissionsError}</p>}
      <PermissionsPanel sessionId={sessionId} grants={grants} update={setGrants}
        interactionMode={grants?.interaction_mode ?? state.session.interaction_mode ?? "agent"}
        permissionMode={state.session.permission_mode ?? "default"}
        executionMode={state.session.execution_mode ?? "sandbox"}
        executionBusy={(state.session.execution !== "idle" && state.session.execution !== undefined) || state.pending_interactions.length > 0}
      />
      </>}
    </div>}
    {tab === "context" && <div className="inspector-body" role="tabpanel" id="inspector-panel-context" aria-labelledby="inspector-tab-context">
      {!breakdown && state.usage.total_tokens == null ? <p className="empty-small">{tr(historyOnly ? "web.inspector_history_context" : "web.inspector_empty_context")}</p> : <>
      <div className="section-label"><span>{tr("web.token_breakdown")}</span></div>
      <section className="stat-card">
        {(breakdown?.categories ?? []).map((item) => <div className="stat-row" key={item.id}><span>{contextCategoryLabel(item.id)}</span><span>{formatTokens(item.tokens)} tok{typeof item.share === "number" ? ` (${Math.round(item.share * 100)}%)` : ""}</span></div>)}
        <div className="stat-total"><span>{tr("web.estimate")}</span><span>{formatTokens(breakdown?.total)} / {formatTokens(breakdown?.limit)}</span></div>
      </section>
      {breakdown?.note && <p className="hint">{breakdown.note}</p>}
      {state.usage.total_tokens != null && <p className="hint">{tr("web.billing_tokens", { count: String(state.usage.total_tokens) })}</p>}
      </>}
    </div>}
  </aside>;
}
