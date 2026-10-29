import { Check, Circle, DotsThreeVertical, Folder, Warning } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { DiffViewer } from "../../DiffViewer";
import { api } from "../../api";
import { present, useT } from "../../i18n";
import type { Grants, PlanStep, ReviewChange, ViewState, WaitReason } from "../../types";
import { PermissionsPanel, ResourceOperations } from "../permissions/permissions";

type InspectorTab = "task" | "files" | "changes" | "permissions" | "context";

/** A path relative to the project, or null when it lies outside it. */
export function projectRelative(path: string, root?: string): string | null {
  const normalized = path.replace(/\\/g, "/");
  const absolute = normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized);
  if (!absolute) return normalized.split("/").includes("..") ? null : normalized;
  const base = (root ?? "").replace(/\\/g, "/").replace(/\/+$/, "");
  if (base && normalized.startsWith(`${base}/`)) return normalized.slice(base.length + 1);
  return null;
}

export function Inspector({ state, sessionId, open, narrow = false, focusPath = null, close }: { state: ViewState; sessionId: string; open: boolean; narrow?: boolean; focusPath?: string | null; close: () => void }) {
  const tr = useT();
  const [tab, setTab] = useState<InspectorTab>("task");
  const [changes, setChanges] = useState<Array<{ path: string; status: string }> | null>(null);
  const [changesError, setChangesError] = useState("");
  const [warning, setWarning] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [patch, setPatch] = useState("");
  const [wide, setWide] = useState(false);
  const [review, setReview] = useState<ReviewChange[]>([]);
  const [reviewNote, setReviewNote] = useState("");
  const [files, setFiles] = useState<Array<{ name: string; path: string; kind: string }>>([]);
  const [directory, setDirectory] = useState("");
  const [browseWorkspace, setBrowseWorkspace] = useState(false);
  const [recentFileMeta, setRecentFileMeta] = useState<Record<string, { size?: number; additions?: number; deletions?: number }>>({});
  const [filePath, setFilePath] = useState("");
  const [fileText, setFileText] = useState("");
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
  const generation = useRef(0);
  useEffect(() => {
    setSelected(null); setPatch(""); setChanges(null); setChangesError(""); setReview([]);
    setDirectory(""); setBrowseWorkspace(false); setRecentFileMeta({}); setFilePath(""); setFileText(""); setFiles([]); setPanelError("");
    setGrants(null);
  }, [sessionId]);
  const refresh = useCallback(() => {
    const request = ++generation.current;
    const owner = sessionId;
    api.changes(owner).then((result) => {
      if (generation.current !== request) return;
      setChanges(result.changes);
      setWarning(result.local_warning);
      setChangesError("");
    }).catch((error) => {
      if (generation.current !== request) return;
      setChangesError(error instanceof Error ? error.message : String(error));
      setChanges(null);
    });
    api.review(owner).then((result) => {
      if (generation.current !== request) return;
      setReview((result.changes ?? []) as ReviewChange[]);
    }).catch(() => { if (generation.current === request) setReview([]); });
  }, [sessionId]);
  useEffect(() => { refresh(); }, [refresh, state.session.status]);
  useEffect(() => {
    let current = true;
    setPanelError("");
    if (tab === "files") {
      if (browseWorkspace) {
        setFiles([]);
        api.tree(sessionId, directory).then((result) => { if (current) setFiles(result.entries); }).catch((error) => { if (current) setPanelError(String(error)); });
      }
    }
    return () => { current = false; };
  }, [tab, sessionId, directory, browseWorkspace]);
  useEffect(() => {
    let current = true;
    if (tab === "permissions") {
      api.grants(sessionId).then((result) => {
        if (current) { setGrants(result); setPanelError(""); }
      }).catch((error) => { if (current) setPanelError(String(error)); });
    }
    return () => { current = false; };
  }, [tab, sessionId, state.session.permission_mode, state.session.interaction_mode, state.permissions_revision, state.connection, state.pending_interactions]);
  useEffect(() => {
    let current = true;
    // Only files inside the project can be listed or diffed; asking about
    // /etc or another checkout just produces failed requests.
    const accessed = (state.accessed_files ?? []).slice(-8)
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
        const lines = result.patch.split(/\r?\n/);
        return [item.path, {
          additions: lines.filter((line) => line.startsWith("+") && !line.startsWith("+++")).length,
          deletions: lines.filter((line) => line.startsWith("-") && !line.startsWith("---")).length,
        }] as const;
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
  }, [sessionId, JSON.stringify(state.accessed_files ?? []), changes]);
  useEffect(() => {
    if (!focusPath) return;
    setTab("files");
    setFilePath(focusPath);
  }, [focusPath, sessionId]);
  useEffect(() => {
    let current = true;
    setFileText("");
    if (filePath) api.file(sessionId, filePath).then((file) => {
      if (current) setFileText(file.binary ? "Binary file" : file.content);
    }).catch((error) => { if (current) setPanelError(String(error)); });
    return () => { current = false; };
  }, [filePath, sessionId]);
  const openPatch = (path: string) => {
    const request = ++generation.current;
    setSelected(path);
    setPatch("");
    api.patch(sessionId, path).then((result) => {
      if (generation.current !== request) return;
      setPatch(result.patch);
    }).catch((error) => {
      if (generation.current !== request) return;
      setPatch("");
      setChangesError(error instanceof Error ? error.message : String(error));
    });
  };
  const paired = (path: string) => {
    const item = review.find((entry) => entry.path === path);
    return item?.rename_with ? [path, item.rename_with] : [path];
  };
  const act = async (action: "accept" | "revert", paths: string[]) => {
    const unique = [...new Set(paths)];
    if (!unique.length) { setReviewNote(tr("web.nothing_ready")); return; }
    setReviewNote("");
    try {
      const result = await api.reviewAction(sessionId, action, unique);
      if (!result.applied) setReviewNote(result.results.map((item) => `${item.path}: ${item.state} ${item.result}`).join("; ") || result.error || "The server did not apply this review.");
      refresh();
    } catch (error) { setReviewNote(String(error)); }
  };
  const breakdown = state.context_breakdown;
  const tabs: InspectorTab[] = ["task", "files", "changes", "permissions", "context"];
  const steps = state.plan.steps ?? [];
  const accessedFiles = state.accessed_files ?? [];
  const completedSteps = steps.filter((step) => step.status === "completed").length;
  const currentStep = steps.findIndex((step) => ["in_progress", "running", "active"].includes(step.status));
  const changedLines = Object.values(recentFileMeta).reduce((sum, file) => sum + (file.additions ?? 0), 0);
  const tabCount = (item: InspectorTab) => item === "files" ? accessedFiles.length || files.length : item === "changes" ? changedLines || changes?.length || 0 : 0;
  const boundaries = grants?.boundary_codes ?? [];
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
  return <aside className={`inspector ${open ? "" : "is-closed"} ${narrow ? "narrow-open" : ""}`}>
    <div className="inspector-tabs" role="tablist">
      {tabs.map((item) => <button key={item} className={item === "permissions" && state.pending_interactions.length ? "pending-tab" : ""} role="tab" aria-selected={tab === item} onClick={() => setTab(item)}><span>{tr(`web.tab.${item}`)}</span>{tabCount(item) > 0 && <small className={item === "changes" ? "tab-positive" : ""}>{item === "changes" ? `(+${tabCount(item)})` : `(${tabCount(item)})`}</small>}</button>)}
      <button className="icon-button" onClick={close} aria-label={tr("web.close_inspector")}><DotsThreeVertical size={14} weight="bold" /></button>
    </div>
    {tab === "task" && <div className="inspector-body">
      <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.goal")}</span>{steps.length ? <span className="goal-progress-copy">{tr("web.steps_completed", { done: completedSteps, total: steps.length })}</span> : null}</div>
        <p>{state.plan.objective || state.session.user_goal || tr("web.no_plan")}</p>
        {steps.length ? <div className="progress"><i style={{ width: `${Math.round((completedSteps / steps.length) * 100)}%` }} /></div> : null}
      </section>
      {state.plan.wait_reason && <p className="hint plan-wait" role="status">{waitText(state.plan.wait_reason)}</p>}
      <div className="inspector-plan">
        <div className="section-label"><span>{tr("web.execution_plan")} {steps.length ? `(${completedSteps}/${steps.length})` : ""}</span>{currentStep >= 0 && <span>{tr("web.step_active", { step: currentStep + 1 })}</span>}</div>
        <div className="step-list">{steps.map((step) => {
          const metadata = stepMeta(step.status, step.note);
          const timing = stepTiming(step);
          return <article key={step.id} className={`step-card ${step.status}`}>{step.status === "completed" ? <Check size={15} weight="bold" /> : ["in_progress", "running", "active"].includes(step.status) ? <span className="step-spinner" aria-hidden="true" /> : <Circle size={15} />}<div><strong title={step.title}>{step.title}</strong><small title={metadata}>{metadata}</small>{timing && <small>{timing}</small>}{typeof step.ended_at === "number" && <small>{tr("web.step_ended", { time: new Date(step.ended_at * 1000).toLocaleString() })}</small>}{step.wait_reason && <small className="plan-wait" role="status">{waitText(step.wait_reason)}</small>}</div>{["in_progress", "running", "active"].includes(step.status) && <span className="current-step">{tr("web.current_step")}</span>}</article>;
        })}</div>
      </div>
      {!state.plan.steps?.length && <p className="empty-small">{tr("web.no_plan")}</p>}
      <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.accessed_files")}</span><span>{tr("web.files_count", { count: (state.accessed_files ?? []).length })}</span></div>
        {accessedFiles.slice(-3).map((file) => {
          const metadata = recentFileMeta[file.path] ?? {};
          const access = file.access === "write" && metadata.additions !== undefined ? `+${metadata.additions} -${metadata.deletions ?? 0}` : file.access === "write" ? "MOD" : file.access === "read" ? "READ" : (file.access || "TOUCH").toUpperCase();
          return <div className={`file-stat${file.access === "write" ? " changed" : ""}`} key={`${file.call_id}-${file.path}`}><span>{file.path}</span><span>{access}</span></div>;
        })}
      </section>
      <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.subagents")}</span><span>{(state.subagents ?? []).length ? tr("web.spawned_count", { count: (state.subagents ?? []).length }) : 0}</span></div>
        {(state.subagents ?? []).map((agent) => <div className="subagent-row" key={agent.task_id}><span className="status-dot" /><span>{agent.task || agent.task_id}</span><span>{(agent.status || "unknown").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase())}</span></div>)}
      {!(state.subagents ?? []).length && <p className="empty-small">{tr("web.no_subagents")}</p>}
    </section>
    </div>}
    {tab === "files" && <div className="inspector-body">
      <div className="files-panel-head"><div className="section-label"><span>{tr("web.workspace_resources")}</span></div><button type="button" className="text-button" onClick={() => { setFilePath(""); setDirectory(""); setBrowseWorkspace((value) => !value); }}>{browseWorkspace ? tr("web.recent_files") : tr("web.browse_workspace")}</button></div>
      {panelError && <p className="form-error" role="alert">{panelError}</p>}
      {browseWorkspace ? <>
        {directory && <button type="button" className="resource-row" onClick={() => { setFilePath(""); setDirectory(directory.split("/").slice(0, -1).join("/")); }}><Folder size={14} /><span>.. / {directory}</span></button>}
        {files.map((entry) => <button className="resource-row" key={entry.path} onClick={() => { setPanelError(""); if (entry.kind === "dir") { setFilePath(""); setDirectory(entry.path); } else setFilePath(entry.path); }}><b>{entry.kind === "dir" ? "DIR" : "FILE"}</b><span>{entry.path}</span></button>)}
        {!files.length && <p className="empty-small">{tr("web.dir_empty")}</p>}
      </> : <>
        <div className="accessed-file-list">{accessedFiles.slice(-8).map((file) => {
          const metadata = recentFileMeta[file.path] ?? {};
          const label = file.access === "write" ? "MOD" : file.access === "read" ? "READ" : (file.access || "TOUCH").toUpperCase();
          const detail = file.access === "write" && (metadata.additions !== undefined || metadata.deletions !== undefined)
            ? `+${metadata.additions ?? 0} -${metadata.deletions ?? 0}`
            : typeof metadata.size === "number" ? `${(metadata.size / 1024).toFixed(1)} KB` : "";
          return <button type="button" className="accessed-file" key={`${file.call_id}-${file.path}`} onClick={() => { setPanelError(""); setFilePath(file.path); }}>
            <span className={`accessed-kind ${file.access === "write" ? "modified" : file.access === "read" ? "read" : "touched"}`}>{label}</span><span className="accessed-path" title={file.path}>{file.path}</span><span className={`accessed-detail${file.access === "write" ? " modified" : ""}`}>{detail}</span>
          </button>;
        })}</div>
        {!accessedFiles.length && <p className="empty-small">{tr("web.no_accessed_files")}</p>}
      </>}
      {fileText && <pre className="term-body">{fileText}</pre>}
    </div>}
    {tab === "changes" && <div className="inspector-body">
      {warning && <div className="local-warning"><Warning size={14} />{tr("web.local_warning")}</div>}
      {changesError && <p className="form-error" role="alert">{changesError}</p>}
      {reviewNote && <p className="form-error" role="status">{reviewNote}</p>}
      <div className="changes-head">
        <div className="section-label"><span>{tr("web.working_tree")}</span></div>
        <button type="button" className="button accept" onClick={() => act("accept", review.filter((item) => item.state === "task").map((item) => item.path))}>{tr("web.accept_ready")}</button>
        <button type="button" className="button revert" onClick={() => {
          const paths = review.filter((item) => item.state === "task" || item.state === "accepted").map((item) => item.path);
          if (!paths.length) { setReviewNote(tr("web.nothing_to_revert")); return; }
          if (window.confirm(tr("web.revert_confirm", { paths: paths.join(", ") }))) act("revert", paths);
        }}>{tr("web.revert_ready")}</button>
      </div>
      <div className="change-list">{(changes ?? []).map((change) => <button key={change.path} className={selected === change.path ? "change-card selected" : "change-card"} onClick={() => openPatch(change.path)}><b>{change.status}</b><span>{change.path}</span></button>)}</div>
      {review.map((item) => <div className="queue-item" key={`review-${item.path}`}><span>{item.display_kind === "rename" ? `rename ${item.kind === "delete" ? item.path : item.rename_with} → ${item.kind === "delete" ? item.rename_with : item.path}` : item.path} · {item.state}</span>
        <button type="button" className="button" onClick={() => act("accept", paired(item.path))}>{tr("web.accept_review")}</button>
        <button type="button" className="button" onClick={() => { const paths = paired(item.path); if (window.confirm(tr("web.revert_single_confirm", { paths: paths.join(", ") }))) act("revert", paths); }}>{tr("web.revert_change")}</button>
      </div>)}
      {selected && <div className="patch">{patch ? <DiffViewer patch={patch} filename={selected} /> : null}<button type="button" className="button" onClick={() => setWide(true)}>{tr("web.expand_diff")}</button></div>}
      {changes && !changes.length && !changesError && <p className="empty-small">{tr("web.tree_clean")}</p>}
      {wide && selected && <div className="wide-diff"><button type="button" className="button" onClick={() => setWide(false)}>{tr("web.close")}</button><DiffViewer patch={patch} filename={selected} /></div>}
    </div>}
    {tab === "permissions" && <div className="inspector-body">
      {panelError && <p className="form-error" role="alert">{panelError}</p>}
      <PermissionsPanel sessionId={sessionId} grants={grants} update={setGrants}
        interactionMode={grants?.interaction_mode ?? state.session.interaction_mode ?? "agent"}
        permissionMode={state.session.permission_mode ?? "default"}
        executionMode={state.session.execution_mode ?? "sandbox"}
        executionBusy={(state.session.execution !== "idle" && state.session.execution !== undefined) || state.pending_interactions.length > 0}
        detail={<>
      {grants?.effective_policy?.map((policy) => <section className="policy-group" key={policy.operation}>
        <div className="card-kicker"><span>{tr(`web.policy_operation.${policy.operation}`)}</span></div>
        <article className="policy-card effective-policy">
          <div><strong>{tr("web.policy_in_scope")}</strong><span>{tr(`web.policy_decision.${policy.defaults.in_scope.decision}`)}</span></div>
          <small>{present(policy.defaults.in_scope.reason_code, policy.defaults.in_scope.reason_params, "")}</small>
          {policy.operation !== "shell" && <><div><strong>{tr("web.policy_outside_scope")}</strong><span>{tr(`web.policy_decision.${policy.defaults.outside_scope.decision}`)}</span></div><small>{present(policy.defaults.outside_scope.reason_code, policy.defaults.outside_scope.reason_params, "")}</small></>}
          {policy.directories.map((path) => <small className="mono policy-path" key={path}>{path}</small>)}
          {policy.read_only.map((path) => <small className="policy-constraint" key={path}>{tr("web.read_only_boundary", { path })}</small>)}
          {policy.rules.map((rule, index) => <div className="policy-rule" key={index}><span>{tr(`web.policy_decision.${rule.effect}`)} · {tr(`web.lifetime.${rule.scope}`)}{rule.conditional ? ` · ${tr("web.policy_conditional")}` : ""}</span><small className="mono policy-path">{rule.target}</small><small><ResourceOperations kind={rule.resource_kind} operations={rule.operations} tool={rule.tool} /></small></div>)}
          {policy.constraints.map((constraint) => <small className="policy-constraint" key={constraint}>{tr(`web.policy_constraint.${constraint}`)}</small>)}
        </article>
      </section>)}
      {boundaries.length > 0 && <section className="policy-group">
        <div className="card-kicker"><span>{tr("web.enforced_boundaries")}</span></div>
        {boundaries.map((item) => <article className="policy-card policy-boundary" key={item}><span className="policy-check"><Check size={12} weight="bold" /></span><span>{tr(item)}</span></article>)}
      </section>}
        </>} />
    </div>}
    {tab === "context" && <div className="inspector-body">
      <div className="section-label"><span>{tr("web.token_breakdown")}</span></div>
      <section className="stat-card">
        {(breakdown?.categories ?? []).map((item) => <div className="stat-row" key={item.id}><span>{contextCategoryLabel(item.id)}</span><span>{formatTokens(item.tokens)} tok{typeof item.share === "number" ? ` (${Math.round(item.share * 100)}%)` : ""}</span></div>)}
        <div className="stat-total"><span>{tr("web.estimate")}</span><span>{formatTokens(breakdown?.total)} / {formatTokens(breakdown?.limit)}</span></div>
      </section>
      {breakdown?.note && <p className="hint">{breakdown.note}</p>}
      <p className="hint">{tr("web.billing_tokens", { count: String(state.usage.total_tokens ?? tr("web.unknown")) })}</p>
    </div>}
  </aside>;
}
