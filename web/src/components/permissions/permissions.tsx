import type React from "react";
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../../api";
import { t, useT } from "../../i18n";
import type { ExecutionMode, Grants, SandboxStatus } from "../../types";
import { commandRuleDetails, commandRuleLabel, networkRuleLabel } from "./permission-resource";

export function useShellReadiness(sessionId: string, enabled: boolean) {
  const [state, setState] = useState<{ sessionId: string; status?: SandboxStatus | null; error?: string; busy?: boolean; checked?: boolean }>({ sessionId });
  const [attempt, setAttempt] = useState(0);
  const initializing = useRef(false);
  const active = useRef(sessionId);
  active.current = sessionId;
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: number;
    const refresh = async () => {
      try {
        const grants = await api.grants(sessionId);
        if (!cancelled) setState((current) => ({ sessionId, status: grants.sandbox, checked: true, busy: initializing.current, error: grants.sandbox?.available ? "" : current.sessionId === sessionId ? current.error : "" }));
      } catch (error) {
        if (!cancelled) setState({ sessionId, checked: true, busy: initializing.current, error: error instanceof Error ? error.message : String(error) });
      }
      if (!cancelled) timer = window.setTimeout(refresh, 1500);
    };
    void refresh();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [sessionId, enabled, attempt]);
  const initialize = async () => {
    if (initializing.current) return;
    initializing.current = true;
    setState((current) => ({ ...current, sessionId, busy: true, error: "" }));
    try {
      const status = await api.sandbox(sessionId, "setup");
      if (active.current === sessionId) setState({ sessionId, status, checked: true });
    } catch (error) {
      if (active.current === sessionId) setState((current) => ({ ...current, busy: false, error: error instanceof Error ? error.message : String(error) }));
    }
    finally { initializing.current = false; }
  };
  return { ...(state.sessionId === sessionId ? state : {}), initialize, refresh: () => { setState({ sessionId }); setAttempt((value) => value + 1); } };
}

export function ResourceOperations({ kind, operations, tool }: { kind: string; operations: string[]; tool?: string }) {
  const tr = useT();
  return <><span>{tr(`web.grant_kind.${kind}`)}{operations.length > 0 && ` · ${operations.map((operation) => tr(`web.operation.${operation}`)).join(", ")}`}</span>
    {tool && tool !== "*" && (kind === "file" || kind === "directory") && <small>{tr("web.tool_restriction", { tool })}</small>}</>;
}

export function PermissionGrants({ sessionId, grants, update }: { sessionId: string; grants: Grants; update: (value: Grants) => void }) {
  const tr = useT();
  const [path, setPath] = useState("");
  const [kind, setKind] = useState("file");
  const [write, setWrite] = useState(false);
  const [lifetime, setLifetime] = useState("project");
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const changing = useRef(false);
  const revision = useRef(0);
  const sandbox = grants.sandbox;
  useEffect(() => {
    let cancelled = false;
    const interval = window.setInterval(() => {
      if (changing.current) return;
      const baseline = revision.current;
      api.grants(sessionId).then((value) => { if (!cancelled && baseline === revision.current) update(value); }).catch(() => undefined);
    }, sandbox?.state === "initializing" ? 1500 : 5000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [sandbox?.state, sessionId, update]);
  const mutate = async (operation: () => Promise<Grants>, success: string, added = false) => {
    if (changing.current) return;
    changing.current = true; revision.current++;
    setBusy(true); setError(""); setNotice("");
    try { update(await operation()); setNotice(tr(success)); if (added) setPath(""); }
    catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setError(tr("web.permission_conflict"));
        if (error.latest) update(error.latest);
        else await api.grants(sessionId).then(update).catch(() => undefined);
      } else {
        setError(error instanceof Error ? error.message : String(error));
        await api.grants(sessionId).then(update).catch(() => undefined);
      }
    }
    finally { revision.current++; changing.current = false; setBusy(false); }
  };
  return <section className="policy-group permission-manager">
    {error && <p className="form-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {sandbox && grants.execution_mode !== "host" && <article className="policy-card">
      <strong>{tr("web.sandbox")}: {sandbox.provider}</strong>
      <p role="status">{tr(`web.sandbox.${sandbox.state}`)}</p>
      {sandbox.detail && <small>{sandbox.detail}</small>}
      {sandbox.platform === "win32" && sandbox.state !== "initializing" && <div className="interaction-actions">
        {!sandbox.available && <button className="button" disabled={busy} onClick={() => mutate(async () => { await api.sandbox(sessionId, "setup"); return api.grants(sessionId); }, "web.sandbox.requested")}>{tr("web.sandbox.initialize")}</button>}
        {sandbox.state !== "setup_required" && <button className="text-button" disabled={busy} onClick={() => mutate(async () => { await api.sandbox(sessionId, "cleanup"); return api.grants(sessionId); }, "web.sandbox.cleaned")}>{tr("web.sandbox.cleanup")}</button>}
      </div>}
    </article>}
    <div className="card-kicker"><span>{tr("web.saved_grants")}</span><span>{grants.grants?.length ?? 0}</span></div>
    {!(grants.grants ?? []).length && <p className="empty-small">{tr("web.no_saved_grants")}</p>}
    {(["session", "project", "user"] as const).map((lifetime) => {
      const items = (grants.grants ?? []).filter((grant) => grant.lifetime === lifetime);
      if (!items.length) return null;
      return <div className="grant-group" key={lifetime}>
        <div className="grant-group-label">{tr(`web.approval.lifetime.${lifetime}`)}</div>
        {items.map((grant) => <article className={`policy-card grant-row ${grant.lifetime === "user" ? "warn" : ""}`} key={`${grant.source}:${grant.id}`}>
          <div className="grant-row-main"><span className="grant-line">{describeGrant(grant)}</span>
            <button type="button" disabled={busy} className="text-button" aria-label={tr("web.revoke_target", { target: grant.target })} onClick={() => mutate(() => api.revokeGrant(sessionId, grant.id, grant.source, grants.version), "web.grant_revoked")}>{tr("web.revoke")}</button></div>
          {grant.cwd && <small className="mono">{tr("web.approval.in_folder", { path: grant.cwd })}</small>}
          {grant.resource_kind === "directory" && grant.recursive && <small>{tr("web.approval.directory_recursive")}</small>}
          {(grant.resource_kind === "command" || grant.resource_kind === "shell") && <small>
            {grant.resource_kind === "command" && `${commandRuleDetails(grant.resource ?? grant)} `}{tr((grant.resource?.execution_mode ?? "sandbox") === "host" ? "web.execution.host_boundary" : "web.approval.sandbox_boundary")}
          </small>}
          {grant.resource_kind === "tool" && <small>{tr("web.approval.tool_operations", { operations: grant.operations.map((operation) => tr(`web.operation.${operation}`)).join(", ") })}</small>}
          {grant.tool && grant.tool !== "*" && <small>{tr("web.tool_restriction", { tool: grant.tool })}</small>}
          {grant.lifetime === "user" && <small>{tr("web.cross_project")}</small>}
        </article>)}
      </div>;
    })}
    <details className="grant-add"><summary>{tr("web.add_rule")}</summary>
    <form className="policy-card grant-form" onSubmit={(event) => { event.preventDefault(); mutate(() => api.addGrant(sessionId, { kind, path: path.trim(), operations: write ? ["file_read", "file_write"] : ["file_read"] }, lifetime, grants.version), "web.grant_added", true); }}>
      <strong>{tr("web.add_grant")}</strong>
      <label>{tr("web.scope_label")}<select value={kind} onChange={(event) => setKind(event.target.value)}><option value="file">{tr("web.grant_kind.file")}</option><option value="directory">{tr("web.grant_kind.directory")}</option></select></label>
      <label>{tr("web.path")}<input required value={path} onChange={(event) => setPath(event.target.value)} placeholder={tr("web.path")} /></label>
      <label>{tr("web.operations")}<select value={write ? "write" : "read"} onChange={(event) => setWrite(event.target.value === "write")}><option value="read">{tr("web.read_only")}</option><option value="write">{tr("web.read_write")}</option></select></label>
      <label>{tr("web.lifetime")}<select value={lifetime} onChange={(event) => setLifetime(event.target.value)}><option value="project">{tr("web.lifetime.project")}</option><option value="session">{tr("web.lifetime.session")}</option>{advanced && <option value="user">{tr("web.lifetime.user")}</option>}</select></label>
      <button type="button" className="text-button" onClick={() => { if (advanced && lifetime === "user") setLifetime("project"); setAdvanced(!advanced); }}>{tr("web.advanced")}</button>
      <p className="grant-summary" aria-live="polite">{path} · {tr(`web.grant_kind.${kind}`)} · {tr(write ? "web.read_write" : "web.read_only")} · {tr(`web.lifetime.${lifetime}`)}</p>
      {lifetime === "user" && <small>{tr("web.cross_project")}</small>}
      <button className="button primary" disabled={busy || !path.trim()}>{tr(busy ? "web.submitting" : "web.add_grant")}</button>
    </form>
    </details>
  </section>;
}

const MODES = ["default", "acceptEdits", "bypass"] as const;

type Decision = "allow" | "ask" | "deny";

function decisionOf(grants: Grants | null, operation: "file_read" | "file_write" | "shell", where: "in_scope" | "outside_scope"): Decision | null {
  const policy = grants?.effective_policy?.find((item) => item.operation === operation);
  const value = policy?.defaults?.[where]?.decision;
  return value === "allow" || value === "ask" || value === "deny" ? value : null;
}

/**
 * The Permissions tab answers three questions in this order: what happens
 * without asking, which rules I saved, and what is always blocked. The full
 * decision matrix stays available, folded, for auditing.
 */
export function PermissionsPanel({ sessionId, grants, update, interactionMode, permissionMode, executionMode = "sandbox", executionBusy = false, detail }: {
  sessionId: string;
  grants: Grants | null;
  update: (value: Grants) => void;
  interactionMode: string;
  permissionMode: string;
  executionMode?: ExecutionMode;
  executionBusy?: boolean;
  detail?: React.ReactNode;
}) {
  const tr = useT();
  const [saving, setSaving] = useState("");
  const [error, setError] = useState("");
  const readOnly = interactionMode !== "agent";
  const execution = grants?.execution_mode ?? executionMode;
  const mode = grants?.permission_mode ?? permissionMode ?? "default";
  const setMode = async (next: string) => {
    if (next === mode || saving) return;
    setSaving(next); setError("");
    try {
      await api.policy(sessionId, { permission_mode: next });
      update(await api.grants(sessionId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally { setSaving(""); }
  };
  const rows: Array<{ key: string; decision: Decision | null; note?: string }> = [
    { key: "read_inside", decision: decisionOf(grants, "file_read", "in_scope") },
    { key: "edit_inside", decision: readOnly ? "deny" : decisionOf(grants, "file_write", "in_scope") },
    { key: "shell_inside", decision: readOnly ? "deny" : decisionOf(grants, "shell", "in_scope"), note: tr(execution === "host" ? "web.execution.host" : "web.glance.sandboxed") },
    { key: "outside", decision: decisionOf(grants, "file_write", "outside_scope") ?? decisionOf(grants, "file_read", "outside_scope") },
    { key: "network", decision: readOnly ? "deny" : "ask", note: execution === "host" ? tr("web.execution.host_network") : undefined },
  ];
  const blocked = (grants?.effective_policy ?? []).flatMap((policy) => policy.rules
    .filter((rule) => rule.effect === "deny")
    .map((rule) => ({ ...rule, operation: policy.operation })));
  return <div className="permissions-panel">
    <section className="perm-section">
      <label>{tr("web.execution_mode")}
        <select className="select" aria-label={tr("web.execution_mode")} value={execution} disabled={executionBusy || Boolean(saving)} onChange={async (event) => {
          const next = event.target.value as ExecutionMode;
          setSaving("execution"); setError("");
          try { await api.policy(sessionId, { execution_mode: next }); update(await api.grants(sessionId)); }
          catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
          finally { setSaving(""); }
        }}>
          <option value="sandbox">{tr("web.execution.sandbox")}</option>
          <option value="host">{tr("web.execution.host")}</option>
        </select>
      </label>
      <p className="hint">{tr(execution === "host" ? "web.execution.host_help" : "web.execution.sandbox_help")}</p>
      <p className="hint">{tr("web.execution.switch_help")}</p>
    </section>
    <section className="perm-section">
      <div className="section-label"><span>{tr("web.glance.title")}</span></div>
      <div className="perm-modes" role="radiogroup" aria-label={tr("web.permission_mode")}>
        {MODES.map((item) => <button key={item} type="button" role="radio" aria-checked={mode === item}
          className={`perm-mode ${mode === item ? "active" : ""}`} disabled={readOnly || Boolean(saving)} onClick={() => void setMode(item)}>
          <strong>{tr(`web.perm.${item}`)}</strong>
          <small>{tr(item === "bypass" && execution === "host" ? "web.execution.host_bypass" : `web.perm_help.${item}`)}</small>
        </button>)}
      </div>
      {readOnly && <p className="hint">{tr("web.perm_locked", { mode: tr(`web.mode.${interactionMode}`) })}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <ul className="glance">
        {rows.map((row) => <li key={row.key} className={`glance-row ${row.decision ?? "unknown"}`}>
          <span>{tr(`web.glance.${row.key}`)}</span>
          <span className="glance-decision">{row.decision ? tr(`web.glance.decision.${row.decision}`) : tr("web.unknown")}{row.note ? ` · ${row.note}` : ""}</span>
        </li>)}
      </ul>
    </section>
    {grants && <PermissionGrants sessionId={sessionId} grants={grants} update={update} />}
    {blocked.length > 0 && <details className="perm-section perm-fold">
      <summary>{tr("web.glance.always_blocked", { count: blocked.length })}</summary>
      <ul className="blocked-list">
        {blocked.map((rule, index) => <li key={`${rule.operation}:${index}`}><code>{rule.target}</code><small>{tr(`web.lifetime.${rule.scope}`)}{rule.conditional ? ` · ${tr("web.policy_conditional")}` : ""}</small></li>)}
      </ul>
    </details>}
    {detail && <details className="perm-section perm-fold">
      <summary>{tr("web.glance.how_decided")}</summary>
      {detail}
    </details>}
  </div>;
}

/** A saved rule as a sentence: what may happen, and where. */
export function describeGrant(grant: Grants["grants"][number]): string {
  const writes = grant.operations.includes("file_write");
  if (grant.resource_kind === "directory") return t(writes ? "web.grant_line.dir_write" : "web.grant_line.dir_read", { path: grant.target });
  if (grant.resource_kind === "file") return t(writes ? "web.grant_line.file_write" : "web.grant_line.file_read", { path: grant.target });
  if (grant.resource_kind === "shell") return t("web.grant_line.shell", { command: grant.target });
  if (grant.resource_kind === "command") return commandRuleLabel(grant.resource ?? grant, grant.target);
  if (grant.resource_kind === "network") return networkRuleLabel(grant.resource ?? grant, grant.target, grant.http_methods);
  if (grant.resource_kind === "tool") return t("web.grant_line.tool", { tool: grant.tool || grant.target });
  return grant.target;
}
