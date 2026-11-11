import { CaretRight, Check, Gauge, ShieldCheck, X } from "@phosphor-icons/react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../../api";
import { parseDiff } from "../../diff";
import { useT } from "../../i18n";
import type { Grants, PlanStep, ViewState, WaitReason } from "../../types";
import { PermissionsPanel } from "../permissions/permissions";
import { ChangesPanel } from "./ChangesPanel";
import { ContextPanel } from "./ContextPanel";
import { FilesPanel } from "./FilesPanel";

export type InspectorTab = "task" | "files" | "changes" | "permissions" | "context";
/** Settings-like tabs sit at the end as icons, so the working tabs always fit. */
const ACTIVE_STEP = ["in_progress", "running", "active"];
/** Notes longer than this clamp to two lines and expand on click. */
const LONG_STEP_NOTE = 80;
const ICON_TABS: Partial<Record<InspectorTab, typeof Gauge>> = { permissions: ShieldCheck, context: Gauge };

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

export function Inspector({ state, sessionId, open, narrow = false, focusPath = null, focusLine, focusRequest = 0, tabRequest, close, onResume, onFeedback, showCall }: { state: ViewState; sessionId: string; open: boolean; narrow?: boolean; focusPath?: string | null; focusLine?: number; focusRequest?: number; tabRequest?: { tab: InspectorTab; request: number }; close: () => void; onResume?: () => Promise<unknown>; onFeedback?: (feedback: string) => void; showCall?: (callId: string) => void }) {
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
  // Files and Changes stay mounted once opened, so their trees, previews and scroll survive tab switches.
  // Other parts of the window (the header's context meter) can ask for a tab.
  useEffect(() => { if (tabRequest) setTab(tabRequest.tab); }, [tabRequest?.request]);
  const [filesVisited, setFilesVisited] = useState(false);
  const [changesVisited, setChangesVisited] = useState(false);
  useEffect(() => { if (tab === "files") setFilesVisited(true); if (tab === "changes") setChangesVisited(true); }, [tab]);
  // One highlight slides between tabs instead of each tab drawing its own box.
  const tablistRef = useRef<HTMLDivElement>(null);
  const [indicator, setIndicator] = useState<{ left: number; width: number; animate: boolean } | null>(null);
  useLayoutEffect(() => {
    const list = tablistRef.current;
    const selected = list?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]');
    if (!list || !selected) return;
    const measure = () => setIndicator((current) => {
      const next = { left: selected.offsetLeft, width: selected.offsetWidth };
      if (current && current.left === next.left && current.width === next.width) return current;
      // The first placement and resizes jump; only a tab change slides.
      return { ...next, animate: current !== null && current.left !== next.left };
    });
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(selected);
    observer.observe(list);
    return () => observer.disconnect();
  }, [tab]);
  const [changes, setChanges] = useState<Array<{ path: string; status: string }> | null>(null);
  const [changeCount, setChangeCount] = useState(0);
  const [changesFocus, setChangesFocus] = useState<{ path: string; request: number } | null>(null);
  const [recentFileMeta, setRecentFileMeta] = useState<Record<string, { size?: number; additions?: number; deletions?: number }>>({});
  const [expandedSteps, setExpandedSteps] = useState<ReadonlySet<string>>(new Set());
  const [filePath, setFilePath] = useState("");
  useEffect(() => {
    if (!filePath || tab !== "files") return;
    const body = panelRef.current?.querySelector<HTMLElement>("#inspector-panel-files");
    if (body) body.scrollTop = 0;
  }, [filePath, tab, focusRequest]);
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
  const readFile = useCallback((path: string) => {
    const relative = projectRelative(path, state.session.execution_root);
    if (relative === null) return Promise.reject(new Error(tr("web.file_outside_workspace")));
    return historyOnly && historyProjectId ? api.workspaceFile(historyProjectId, relative) : api.file(sessionId, relative);
  }, [sessionId, historyOnly, historyProjectId, state.session.execution_root, tr]);
  const searchFiles = useCallback(async (query: string) => {
    const result = historyOnly && historyProjectId ? await api.workspaceSearch(historyProjectId, query, "file") : await api.search(sessionId, query, "file");
    return result.results.filter((item): item is { path: string } => typeof item.path === "string");
  }, [sessionId, historyOnly, historyProjectId]);
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
  const changesGeneration = useRef(0);
  useEffect(() => {
    ++changesGeneration.current;
    setChanges(null); setChangeCount(0); setPermissionsError("");
    setRecentFileMeta({}); setFilePath(""); setChangesFocus(null);
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
  const breakdown = state.context_breakdown;
  const tabs: InspectorTab[] = ["task", "files", "changes", "permissions", "context"];
  const steps = state.plan.steps ?? [];
  // Shown relative to the project when they are inside it. An absolute and a relative
  // spelling of one file merge: the later access wins, but a write and tool counts carry over.
  const accessedByPath = new Map<string, NonNullable<typeof state.accessed_files>[number]>();
  for (const file of state.accessed_files ?? []) {
    const path = projectRelative(file.path, state.session.execution_root) ?? file.path;
    const previous = accessedByPath.get(path);
    const tools = { ...(previous?.tools ?? {}) };
    for (const [name, count] of Object.entries(file.tools ?? {})) tools[name] = (tools[name] ?? 0) + count;
    accessedByPath.delete(path);
    const lastCalls = { ...(previous?.last_calls ?? {}) };
    for (const [name, callId] of Object.entries(file.last_calls ?? {})) { delete lastCalls[name]; lastCalls[name] = callId; }
    accessedByPath.set(path, { ...file, path, tools, last_calls: lastCalls, access: previous?.access === "write" ? "write" : file.access });
  }
  const accessedFiles = [...accessedByPath.values()];
  const accessCounts: Record<string, number> = {};
  for (const file of state.accessed_files ?? []) {
    const path = projectRelative(file.path, state.session.execution_root) ?? file.path;
    accessCounts[path] = (accessCounts[path] ?? 0) + (file.count ?? 1);
  }
  const projectName = (state.session.execution_root ?? "").replace(/\\/g, "/").replace(/\/+$/, "").split("/").pop() ?? "";
  const completedSteps = steps.filter((step) => step.status === "completed").length;
  const currentStep = steps.findIndex((step) => ACTIVE_STEP.includes(step.status));
  const tabCount = (item: InspectorTab) => item === "files" ? accessedFiles.length : item === "changes" ? changeCount : 0;
  const fileMeta = (path: string) => recentFileMeta[projectRelative(path, state.session.execution_root) ?? path] ?? {};
  // The icon already says done, doing or to do; only other states (blocked, skipped…) are spelled out.
  const stepMeta = (status: string, note?: string) => {
    const label = status.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
    const detail = note?.trim();
    if (["completed", "pending", ...ACTIVE_STEP].includes(status)) return detail ?? "";
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
    return `${((step.elapsed_ms + extra) / 1000).toFixed(1)}s`;
  };
  const contextLimit = breakdown?.limit ?? state.usage.context_limit ?? null;
  const contextTotal = breakdown?.total ?? state.usage.context_tokens ?? null;
  const contextShare = contextLimit && typeof contextTotal === "number" ? Math.min(100, Math.round((contextTotal / contextLimit) * 100)) : null;
  const permissionMode = state.session.permission_mode ?? "default";
  const pendingCount = state.pending_interactions.length;
  const modeLabel = tr(`web.perm_short.${permissionMode}`) === `web.perm_short.${permissionMode}` ? permissionMode : tr(`web.perm_short.${permissionMode}`);
  const overviewRows: Array<{ tab: InspectorTab; label: string; value: string; alert?: boolean }> = [
    { tab: "files", label: tr("web.accessed_files"), value: accessedFiles.length ? tr("web.files_count", { count: accessedFiles.length }) : tr("web.overview.none") },
    { tab: "changes", label: tr("web.overview.changes"), value: changeCount ? tr("web.files_count", { count: changeCount }) : tr("web.overview.none") },
    { tab: "context", label: tr("web.tab.context"), value: contextShare === null ? "—" : tr("web.overview.context_used", { percent: contextShare }) },
    { tab: "permissions", label: tr("web.tab.permissions"), alert: pendingCount > 0,
      value: pendingCount ? tr("web.overview.waiting", { count: pendingCount })
        : `${modeLabel} · ${tr(state.session.execution_mode === "host" ? "web.overview.host" : "web.overview.sandbox")}` },
  ];
  return <aside id="workspace-inspector" ref={panelRef} inert={!open} aria-hidden={!open || undefined} role={narrow ? "dialog" : undefined} aria-modal={narrow || undefined} aria-label={tr("web.inspector")} className={`inspector ${open ? "" : "is-closed"} ${narrow ? "narrow-open" : ""}`} onKeyDown={(event) => {
    if (!open || !narrow || event.key !== "Tab") return;
    const controls = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]):not([tabindex="-1"]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]') ?? []);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}>
    <div className="inspector-inner">
    <div className="inspector-tabs">
      <div className="inspector-tablist" role="tablist" aria-label={tr("web.inspector")} ref={tablistRef}>
      {indicator && <span className={`inspector-tab-indicator${indicator.animate ? " animate" : ""}`} aria-hidden="true" style={{ width: indicator.width, transform: `translateX(${indicator.left}px)` }} />}
      {/* Tabs switch on press, as editor tabs do; click still covers keyboard and assistive activation. */}
      {tabs.map((item, index) => <button key={item} id={`inspector-tab-${item}`} aria-controls={`inspector-panel-${item}`} tabIndex={tab === item ? 0 : -1} className={`${ICON_TABS[item] ? "icon-tab" : ""}${item === "permissions" && state.pending_interactions.length ? " pending-tab" : ""}`.trim() || undefined} role="tab" aria-selected={tab === item}
        onMouseDown={(event) => { if (event.button === 0) setTab(item); }} onClick={() => setTab(item)} onKeyDown={(event) => {
        const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
        if (next === null) return;
        event.preventDefault(); setTab(tabs[next]);
        const button = panelRef.current?.querySelector<HTMLButtonElement>(`#inspector-tab-${tabs[next]}`);
        button?.focus(); button?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
      }} aria-label={ICON_TABS[item] ? tr(`web.tab.${item}`) : undefined} title={ICON_TABS[item] ? tr(`web.tab.${item}`) : undefined}>
        {(() => { const Icon = ICON_TABS[item]; return Icon
          ? <><Icon size={15} aria-hidden="true" />{item === "permissions" && state.pending_interactions.length > 0 && <span className="tab-alert" aria-hidden="true" />}</>
          : <><span>{tr(`web.tab.${item}`)}</span>{tabCount(item) > 0 && <>{" "}<small>{tabCount(item)}</small></>}</>; })()}
      </button>)}
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
        <ol className="step-list">{steps.map((step, index) => {
          const active = ACTIVE_STEP.includes(step.status);
          const note = stepMeta(step.status, step.note);
          const duration = stepTiming(step);
          const ended = typeof step.ended_at === "number" ? step.ended_at : null;
          // The row shows how long; the full wording and end time sit in the tooltip.
          const timeTitle = [duration && tr("web.step_elapsed", { duration }), ended !== null && tr("web.step_ended", { time: new Date(ended * 1000).toLocaleString() })].filter(Boolean).join(" · ");
          const timeText = duration || (ended !== null ? shortTime(ended) : "");
          const expanded = expandedSteps.has(step.id);
          const toggleNote = () => setExpandedSteps((current) => {
            const next = new Set(current);
            if (!next.delete(step.id)) next.add(step.id);
            return next;
          });
          const statusKey = `web.plan.status.${step.status}`;
          const statusLabel = tr(statusKey) === statusKey ? step.status : tr(statusKey);
          // A timeline node: done shows a check, the current step a live dot, the rest their number.
          return <li key={step.id} className={`step-card ${step.status}`}>
            <span className="step-node" aria-hidden="true">{step.status === "completed" ? <Check size={10} weight="bold" /> : active ? <i /> : index + 1}</span>
            <span className="sr-only">{statusLabel}: </span>
            <div className="step-body">
              <div className="step-head">
                <strong title={step.title}>{step.title}</strong>
                {active && <span className="current-step">{tr("web.current_step")}</span>}
                {timeText && <span className="step-time" title={timeTitle} aria-label={timeTitle}>{timeText}</span>}
              </div>
              {note && (note.length > LONG_STEP_NOTE
                ? <button type="button" className={`step-note-toggle${expanded ? " expanded" : ""}`} aria-expanded={expanded} title={expanded ? undefined : note} onClick={toggleNote}><span className="step-note">{note}</span></button>
                : <small className="step-note">{note}</small>)}
              {step.wait_reason && <small className="plan-wait" role="status">{waitText(step.wait_reason)}</small>}
            </div>
          </li>;
        })}</ol>
      </section>}
      {/* Where things stand, one row per detail tab; each row opens its tab. */}
      <section className="goal-card overview-glance" aria-label={tr("web.overview.session")}>
        <div className="card-kicker"><span>{tr("web.overview.session")}</span></div>
        <div className="overview-rows">
          {overviewRows.map((row) => <button key={row.tab} type="button" className={`overview-row${row.alert ? " alert" : ""}`} onClick={() => setTab(row.tab)}>
            <span className="overview-label">{row.label}</span>
            <span className="overview-value">{row.value}</span>
            <CaretRight size={11} weight="bold" aria-hidden="true" />
          </button>)}
        </div>
      </section>
      {(state.subagents ?? []).length > 0 && <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.subagents")}</span>{(state.subagents ?? []).length ? <span>{tr("web.spawned_count", { count: (state.subagents ?? []).length })}</span> : null}</div>
        {(state.subagents ?? []).map((agent) => <div className="subagent-row" key={agent.task_id}><span className="status-dot" /><span>{agent.task || agent.task_id}</span><span>{(agent.status || "unknown").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase())}</span></div>)}
      </section>}
      {!steps.length && !(state.subagents ?? []).length && <p className="hint inspector-task-empty">{tr("web.inspector_task_empty")}</p>}
    </div>}
    {/* Kept mounted once opened, so the tree and preview survive tab switches. */}
    {(tab === "files" || filesVisited) && <div className="inspector-body" role="tabpanel" id="inspector-panel-files" aria-labelledby="inspector-tab-files" hidden={tab !== "files"}>
      <FilesPanel sessionKey={`${sessionId}:${historyOnly}:${historyProjectId ?? ""}`} canReadFiles={canReadFiles} historyOnly={historyOnly}
        accessedFiles={accessedFiles} fileMeta={fileMeta} readTree={readTree} readFile={readFile} searchFiles={searchFiles}
        filePath={filePath} focusLine={focusPath && filePath === (projectRelative(focusPath, state.session.execution_root) ?? focusPath) ? focusLine : undefined} openFile={setFilePath} changes={changes ?? []} accessCounts={accessCounts} projectName={projectName} projectRoot={state.session.execution_root ?? undefined}
        openChanges={(path) => { setChangesFocus((current) => ({ path, request: (current?.request ?? 0) + 1 })); setTab("changes"); }} showCall={showCall} />
    </div>}
    {(tab === "changes" || changesVisited) && <div className="inspector-body" role="tabpanel" id="inspector-panel-changes" aria-labelledby="inspector-tab-changes" hidden={tab !== "changes"}>
      {historyOnly ? resumeStatus ?? <p className="hint">{tr("web.inspector_history_changes")}</p> : <ChangesPanel sessionId={sessionId} state={state} onCount={setChangeCount} onWorkingChanges={setChanges} onFeedback={onFeedback} focus={changesFocus} />}

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
      <ContextPanel breakdown={breakdown} usage={state.usage} historyOnly={historyOnly} />
    </div>}
    </div>
  </aside>;
}
