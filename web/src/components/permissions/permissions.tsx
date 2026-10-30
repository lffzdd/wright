import { useEffect, useId, useRef, useState } from "react";
import { ApiError, api } from "../../api";
import { t, useT } from "../../i18n";
import type { ExecutionMode, Grants, SandboxStatus } from "../../types";
import { commandRuleDetails, commandRuleLabel, networkRuleLabel } from "./permission-resource";
import { PolicyAuditDetail } from "./policyAuditDetail";

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

/** Primary label for a saved grant; shell/command/network use terminal styling. */
function GrantPrimary({ grant }: { grant: Grants["grants"][number] }) {
  const tr = useT();
  if (grant.resource_kind === "shell") {
    return <div className="perm-command grant-command"><span className="muted">{tr("web.grant_run")}</span><span className="perm-target">{grant.target}</span></div>;
  }
  if (grant.resource_kind === "command" || grant.resource_kind === "network") {
    return <div className="perm-command grant-command"><span className="perm-target">{describeGrant(grant)}</span></div>;
  }
  return <span className="grant-line">{describeGrant(grant)}</span>;
}

const GRANT_LIST_SCROLL_AT = 5;
const GRANT_LIFETIMES = ["session", "project", "user"] as const;

function useGrantMutate(sessionId: string, grants: Grants, update: (value: Grants) => void) {
  const tr = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const changing = useRef(false);
  const revision = useRef(0);
  const mutate = async (operation: () => Promise<Grants>, success: string, added = false) => {
    if (changing.current) return;
    changing.current = true; revision.current++;
    setBusy(true); setError(""); setNotice("");
    try {
      update(await operation());
      setNotice(tr(success));
      return true;
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        setError(tr("web.permission_conflict"));
        if (caught.latest) update(caught.latest);
        else await api.grants(sessionId).then(update).catch(() => undefined);
      } else {
        setError(caught instanceof Error ? caught.message : String(caught));
        await api.grants(sessionId).then(update).catch(() => undefined);
      }
    } finally {
      revision.current++; changing.current = false; setBusy(false);
    }
    return false;
  };
  return { mutate, busy, error, notice, revision, changing };
}

function GrantFormSelect({ label, value, options, onChange, disabled = false, menuUp = false }: {
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
  disabled?: boolean;
  menuUp?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const current = options.find((option) => option.value === value)?.label ?? value;
  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return <div className={`grant-form-select${menuUp ? " grant-form-select-up" : ""}`} ref={rootRef}>
    <span className="grant-form-select-label" id={listId}>{label}</span>
    <button type="button" className="grant-form-select-trigger" disabled={disabled}
      aria-labelledby={listId} aria-haspopup="listbox" aria-expanded={open}
      onClick={() => setOpen((currentOpen) => !currentOpen)}>
      <span className="grant-form-select-value">{current}</span>
    </button>
    {open ? <ul className="grant-form-select-menu" role="listbox" aria-labelledby={listId}>
      {options.map((option) => <li key={option.value}>
        <button type="button" role="option" aria-selected={value === option.value}
          className={value === option.value ? "active" : ""}
          onClick={() => { onChange(option.value); setOpen(false); }}>
          {option.label}
        </button>
      </li>)}
    </ul> : null}
  </div>;
}

function GrantItemList({ sessionId, items, grants, busy, mutate }: {
  sessionId: string;
  items: Grants["grants"];
  grants: Grants;
  busy: boolean;
  mutate: ReturnType<typeof useGrantMutate>["mutate"];
}) {
  const tr = useT();
  const scroll = items.length > GRANT_LIST_SCROLL_AT;
  return (
    <ul className={`grant-list${scroll ? " grant-list-scroll" : ""}`}>
      {items.map((grant) => <li className={`grant-item ${grant.lifetime === "user" ? "warn" : ""}`} key={`${grant.source}:${grant.id}`} title={grant.resource_kind === "command" || grant.resource_kind === "shell" ? [grant.cwd && grant.resource_kind === "shell" ? tr("web.approval.in_folder", { path: grant.cwd }) : "", tr((grant.resource?.execution_mode ?? "sandbox") === "host" ? "web.execution.host_boundary" : "web.approval.sandbox_boundary")].filter(Boolean).join(" · ") : undefined}>
        <div className="grant-item-body">
          <GrantPrimary grant={grant} />
          {grant.resource_kind === "directory" && grant.recursive && <small>{tr("web.approval.directory_recursive")}</small>}
          {grant.resource_kind === "command" && <small>{commandRuleDetails(grant.resource ?? grant)}</small>}
          {grant.resource_kind === "tool" && <small>{tr("web.approval.tool_operations", { operations: grant.operations.map((operation) => tr(`web.operation.${operation}`)).join(", ") })}</small>}
          {grant.tool && grant.tool !== "*" && grant.resource_kind !== "command" && grant.resource_kind !== "shell" && <small>{tr("web.tool_restriction", { tool: grant.tool })}</small>}
          {grant.lifetime === "user" && <small>{tr("web.cross_project")}</small>}
        </div>
        <button type="button" disabled={busy} className="grant-revoke" aria-label={tr("web.revoke_target", { target: grant.target })} onClick={() => mutate(() => api.revokeGrant(sessionId, grant.id, grant.source, grants.version), "web.grant_revoked")}>{tr("web.revoke")}</button>
      </li>)}
    </ul>
  );
}

export function PermissionGrantManualAdd({ sessionId, grants, update }: { sessionId: string; grants: Grants; update: (value: Grants) => void }) {
  const tr = useT();
  const [path, setPath] = useState("");
  const [kind, setKind] = useState("file");
  const [write, setWrite] = useState(false);
  const [lifetime, setLifetime] = useState("project");
  const { mutate, busy, error, notice } = useGrantMutate(sessionId, grants, update);
  return <div className="grant-add-section" aria-label={tr("web.add_rule")}>
    <details className="grant-add-card">
      <summary className="grant-add-summary">
        <span className="grant-add-summary-copy">
          <span className="grant-add-title">{tr("web.add_rule")}</span>
          <span className="grant-add-hint">{tr("web.add_rule_hint")}</span>
        </span>
      </summary>
      <div className="grant-add-body">
        {error && <p className="form-error" role="alert">{error}</p>}
        {notice && <p className="hint" role="status">{notice}</p>}
        <form className="grant-form" onSubmit={(event) => {
          event.preventDefault();
          void mutate(() => api.addGrant(sessionId, { kind, path: path.trim(), operations: write ? ["file_read", "file_write"] : ["file_read"] }, lifetime, grants.version), "web.grant_added", true).then((ok) => { if (ok) setPath(""); });
        }}>
          <GrantFormSelect label={tr("web.scope_label")} value={kind} onChange={setKind} options={[
            { value: "file", label: tr("web.grant_kind.file") },
            { value: "directory", label: tr("web.grant_kind.directory") },
          ]} />
          <label>{tr("web.path")}<input className="grant-path-input" required value={path} onChange={(event) => setPath(event.target.value)} placeholder={tr("web.path")} aria-label={tr("web.path")} /></label>
          <div className="grant-form-row">
            <GrantFormSelect label={tr("web.operations")} value={write ? "write" : "read"} menuUp onChange={(next) => setWrite(next === "write")} options={[
              { value: "read", label: tr("web.read_only") },
              { value: "write", label: tr("web.read_write") },
            ]} />
            <GrantFormSelect label={tr("web.lifetime")} value={lifetime} onChange={setLifetime} menuUp options={[
              { value: "project", label: tr("web.lifetime.project") },
              { value: "session", label: tr("web.lifetime.session") },
              { value: "user", label: tr("web.lifetime.user") },
            ]} />
          </div>
          <div className="grant-form-foot">
            <button className="button primary grant-form-submit" disabled={busy || !path.trim()}>{tr(busy ? "web.submitting" : "web.add_grant")}</button>
          </div>
          {lifetime === "user" && <p className="hint">{tr("web.cross_project")}</p>}
          {path.trim() ? <p className="grant-summary" aria-live="polite">{path} · {tr(`web.grant_kind.${kind}`)} · {tr(write ? "web.read_write" : "web.read_only")} · {tr(`web.lifetime.${lifetime}`)}</p> : null}
        </form>
      </div>
    </details>
  </div>;
}

export function PermissionGrants({ sessionId, grants, update, includeManualAdd = false }: { sessionId: string; grants: Grants; update: (value: Grants) => void; includeManualAdd?: boolean }) {
  const tr = useT();
  const sandbox = grants.sandbox;
  const { mutate, busy, error, notice, revision, changing } = useGrantMutate(sessionId, grants, update);
  useEffect(() => {
    let cancelled = false;
    const interval = window.setInterval(() => {
      if (changing.current) return;
      const baseline = revision.current;
      api.grants(sessionId).then((value) => { if (!cancelled && baseline === revision.current) update(value); }).catch(() => undefined);
    }, sandbox?.state === "initializing" ? 1500 : 5000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [sandbox?.state, sessionId, update, revision, changing]);
  const grantItems = grants.grants ?? [];
  const filledGroups = GRANT_LIFETIMES.filter((lifetime) => grantItems.some((grant) => grant.lifetime === lifetime));
  const showGroupLabels = filledGroups.length > 1;
  return <>
  <section className="perm-section permission-manager">
    {error && <p className="form-error" role="alert">{error}</p>}
    {notice && <p className="hint" role="status">{notice}</p>}
    {sandbox && grants.execution_mode !== "host" && <article className="policy-card sandbox-card">
      <strong>{tr("web.sandbox")}: {sandbox.provider}</strong>
      <p role="status">{tr(`web.sandbox.${sandbox.state}`)}</p>
      {sandbox.detail && <small>{sandbox.detail}</small>}
      {sandbox.platform === "win32" && sandbox.state !== "initializing" && <div className="interaction-actions">
        {!sandbox.available && <button className="button" disabled={busy} onClick={() => mutate(async () => { await api.sandbox(sessionId, "setup"); return api.grants(sessionId); }, "web.sandbox.requested")}>{tr("web.sandbox.initialize")}</button>}
        {sandbox.state !== "setup_required" && <button className="text-button" disabled={busy} onClick={() => mutate(async () => { await api.sandbox(sessionId, "cleanup"); return api.grants(sessionId); }, "web.sandbox.cleaned")}>{tr("web.sandbox.cleanup")}</button>}
      </div>}
    </article>}
    <div className="section-label"><span>{tr("web.saved_grants")}</span><span className="section-count">{grants.grants?.length ?? 0}</span></div>
    {!(grants.grants ?? []).length && <p className="empty-small">{tr("web.no_saved_grants")}</p>}
    {GRANT_LIFETIMES.map((lifetime) => {
      const items = grantItems.filter((grant) => grant.lifetime === lifetime);
      if (!items.length) return null;
      return <div className="grant-group" key={lifetime}>
        {showGroupLabels ? <div className="grant-group-label">{tr(`web.grant_group.${lifetime}`)}</div> : null}
        <GrantItemList sessionId={sessionId} items={items} grants={grants} busy={busy} mutate={mutate} />
      </div>;
    })}
    {includeManualAdd ? <PermissionGrantManualAdd sessionId={sessionId} grants={grants} update={update} /> : null}
  </section>
  </>;
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
export function PermissionsPanel({ sessionId, grants, update, interactionMode, permissionMode, executionMode = "sandbox", executionBusy = false }: {
  sessionId: string;
  grants: Grants | null;
  update: (value: Grants) => void;
  interactionMode: string;
  permissionMode: string;
  executionMode?: ExecutionMode;
  executionBusy?: boolean;
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
  const modeHelp = tr(mode === "bypass" && execution === "host" ? "web.execution.host_bypass" : `web.perm_help.${mode}`);
  return <div className="permissions-panel">
    <section className="perm-section perm-execution">
      <div className="section-label"><span>{tr("web.execution_mode")}</span></div>
      <select className="select perm-execution-select" aria-label={tr("web.execution_mode")} value={execution} disabled={executionBusy || Boolean(saving)} onChange={async (event) => {
        const next = event.target.value as ExecutionMode;
        setSaving("execution"); setError("");
        try { await api.policy(sessionId, { execution_mode: next }); update(await api.grants(sessionId)); }
        catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
        finally { setSaving(""); }
      }}>
        <option value="sandbox">{tr("web.execution.sandbox")}</option>
        <option value="host">{tr("web.execution.host")}</option>
      </select>
      <details className="perm-fold perm-execution-help">
        <summary title={tr("web.execution.switch_help")}>{tr("web.execution.help_summary")}</summary>
        <p className="hint">{tr(execution === "host" ? "web.execution.host_help" : "web.execution.sandbox_help")}</p>
      </details>
    </section>
    <section className="perm-section perm-policy">
      <div className="section-label"><span>{tr("web.glance.title")}</span></div>
      <div className="perm-mode-segment" role="radiogroup" aria-label={tr("web.permission_mode")}>
        {MODES.map((item) => <button key={item} type="button" role="radio" aria-checked={mode === item}
          className={`perm-segment ${mode === item ? "active" : ""}`} disabled={readOnly || Boolean(saving)} onClick={() => void setMode(item)}
          title={tr(`web.perm.${item}`)} aria-label={tr(`web.perm.${item}`)}>
          {tr(`web.perm_short.${item}`)}
        </button>)}
      </div>
      <p className="hint perm-mode-help">{modeHelp}</p>
      {readOnly && <p className="hint">{tr("web.perm_locked", { mode: tr(`web.mode.${interactionMode}`) })}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <ul className="glance">
        {rows.map((row) => <li key={row.key} className={`glance-row ${row.decision ?? "unknown"}`}>
          <span className="glance-label">{tr(`web.glance.${row.key}`)}</span>
          <span className="glance-outcome">
            <span className="glance-decision">{row.decision ? tr(`web.glance.decision.${row.decision}`) : tr("web.unknown")}</span>
            {row.note ? <small className="glance-note">{row.note}</small> : null}
          </span>
        </li>)}
      </ul>
    </section>
    {grants && <PermissionGrants sessionId={sessionId} grants={grants} update={update} includeManualAdd />}
    {grants && (blocked.length > 0 || grants.effective_policy.length > 0) && <div className="perm-advanced">
      {blocked.length > 0 && <details className="perm-fold" open>
        <summary>{tr("web.glance.always_blocked", { count: blocked.length })}</summary>
        <ul className="grant-list policy-blocked-list">
          {blocked.map((rule, index) => <li className="grant-item policy-blocked-item" key={`${rule.operation}:${index}`}>
            <div className="grant-item-body">
              <div className="perm-command grant-command"><span className="perm-target">{rule.target}</span></div>
              <small>{tr(`web.lifetime.${rule.scope}`)}{rule.conditional ? ` · ${tr("web.policy_conditional")}` : ""}</small>
            </div>
          </li>)}
        </ul>
      </details>}
      {grants.effective_policy.length > 0 && <details className="perm-fold">
        <summary>{tr("web.glance.how_decided")}</summary>
        <PolicyAuditDetail grants={grants} />
      </details>}
    </div>}
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
