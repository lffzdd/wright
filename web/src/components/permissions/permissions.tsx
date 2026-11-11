import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../../api";
import { t, useT } from "../../i18n";
import type { ExecutionMode, Grants, PermissionResource, SandboxStatus } from "../../types";
import { InlineSelect } from "../ui/InlineSelect";
import { SegmentedControl } from "../ui/SegmentedControl";
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

const programName = (path: string) => path.split(/[\\/]/).filter(Boolean).pop() || path;

/** A command rule named by its program, not its install path; the full rule stays in the tooltip. */
function shortCommandLabel(grant: Grants["grants"][number]): string {
  const resource: PermissionResource = grant.resource ?? grant;
  const argv = resource.argv ?? [];
  return commandRuleLabel({
    ...resource,
    programs: resource.programs?.map(programName),
    argv: argv.length ? [programName(argv[0]), ...argv.slice(1)] : argv,
  }, programName(grant.target));
}

/** Primary label for a saved grant; shell/command/network use terminal styling. */
function GrantPrimary({ grant }: { grant: Grants["grants"][number] }) {
  const tr = useT();
  if (grant.resource_kind === "shell") {
    return <div className="perm-command grant-command"><span className="muted">{tr("web.grant_run")}</span><span className="perm-target">{grant.target}</span></div>;
  }
  if (grant.resource_kind === "command") {
    return <div className="perm-command grant-command"><span className="perm-target">{shortCommandLabel(grant)}</span></div>;
  }
  if (grant.resource_kind === "network") {
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

function GrantItemList({ sessionId, items, grants, busy, mutate }: {
  sessionId: string;
  items: Grants["grants"];
  grants: Grants;
  busy: boolean;
  mutate: ReturnType<typeof useGrantMutate>["mutate"];
}) {
  const tr = useT();
  const scroll = items.length > GRANT_LIST_SCROLL_AT;
  // Program rules all carry the same caveat; it is said once under the list.
  const commandNotes = [...new Set(items.filter((grant) => grant.resource_kind === "command").map((grant) => commandRuleDetails(grant.resource ?? grant)))];
  return <>
    <ul className={`grant-list${scroll ? " grant-list-scroll" : ""}`}>
      {items.map((grant) => <li className={`grant-item ${grant.lifetime === "user" ? "warn" : ""}`} key={`${grant.source}:${grant.id}`} title={grant.resource_kind === "command" || grant.resource_kind === "shell" ? [grant.resource_kind === "command" ? describeGrant(grant) : "", grant.cwd && grant.resource_kind === "shell" ? tr("web.approval.in_folder", { path: grant.cwd }) : "", tr((grant.resource?.execution_mode ?? "sandbox") === "host" ? "web.execution.host_boundary" : "web.approval.sandbox_boundary")].filter(Boolean).join(" · ") : undefined}>
        <div className="grant-item-body">
          <GrantPrimary grant={grant} />
          {grant.resource_kind === "directory" && grant.recursive && <small>{tr("web.approval.directory_recursive")}</small>}
          {grant.resource_kind === "tool" && <small>{tr("web.approval.tool_operations", { operations: grant.operations.map((operation) => tr(`web.operation.${operation}`)).join(", ") })}</small>}
          {grant.tool && grant.tool !== "*" && grant.resource_kind !== "command" && grant.resource_kind !== "shell" && <small>{tr("web.tool_restriction", { tool: grant.tool })}</small>}
          {grant.lifetime === "user" && <small>{tr("web.cross_project")}</small>}
        </div>
        <button type="button" disabled={busy} className="grant-revoke" aria-label={tr("web.revoke_target", { target: grant.target })} onClick={() => mutate(() => api.revokeGrant(sessionId, grant.id, grant.source, grants.version), "web.grant_revoked")}>{tr("web.revoke")}</button>
      </li>)}
    </ul>
    {commandNotes.map((note) => <p className="grant-footnote" key={note}>{note}</p>)}
  </>;
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
          <InlineSelect label={tr("web.scope_label")} value={kind} onChange={setKind} options={[
            { value: "file", label: tr("web.grant_kind.file") },
            { value: "directory", label: tr("web.grant_kind.directory") },
          ]} />
          <label>{tr("web.path")}<input className="grant-path-input" required value={path} onChange={(event) => setPath(event.target.value)} placeholder={tr("web.path")} aria-label={tr("web.path")} /></label>
          <div className="grant-form-row">
            <InlineSelect label={tr("web.operations")} value={write ? "write" : "read"} menuUp onChange={(next) => setWrite(next === "write")} options={[
              { value: "read", label: tr("web.read_only") },
              { value: "write", label: tr("web.read_write") },
            ]} />
            <InlineSelect label={tr("web.lifetime")} value={lifetime} onChange={setLifetime} menuUp options={[
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

/** Which OS sandbox runs Shell, and whether it is ready; Windows can set it up from here. */
export function SandboxStatusCard({ sessionId, grants, update }: { sessionId: string; grants: Grants; update: (value: Grants) => void }) {
  const tr = useT();
  const sandbox = grants.sandbox;
  const { mutate, busy, error, notice } = useGrantMutate(sessionId, grants, update);
  if (!sandbox || grants.execution_mode === "host") return null;
  return <article className="policy-card sandbox-card">
    {error && <p className="form-error" role="alert">{error}</p>}
    {notice && <p className="hint" role="status">{notice}</p>}
    <strong>{tr("web.sandbox")}: {sandbox.provider}</strong>
    <p role="status">{tr(`web.sandbox.${sandbox.state}`)}</p>
    {sandbox.detail && <small>{sandbox.detail}</small>}
    {sandbox.platform === "win32" && sandbox.state !== "initializing" && <div className="interaction-actions">
      {!sandbox.available && <button className="button" disabled={busy} onClick={() => mutate(async () => { await api.sandbox(sessionId, "setup"); return api.grants(sessionId); }, "web.sandbox.requested")}>{tr("web.sandbox.initialize")}</button>}
      {sandbox.state !== "setup_required" && <button className="text-button" disabled={busy} onClick={() => mutate(async () => { await api.sandbox(sessionId, "cleanup"); return api.grants(sessionId); }, "web.sandbox.cleaned")}>{tr("web.sandbox.cleanup")}</button>}
    </div>}
  </article>;
}

export function PermissionGrants({ sessionId, grants, update, includeManualAdd = false, showSandbox = true }: { sessionId: string; grants: Grants; update: (value: Grants) => void; includeManualAdd?: boolean; showSandbox?: boolean }) {
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
    {showSandbox && <SandboxStatusCard sessionId={sessionId} grants={grants} update={update} />}
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

function sandboxNetworkGrants(grants: Grants | null) {
  return (grants?.grants ?? []).filter((grant) => grant.resource_kind === "sandbox_network");
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
  const setExecution = async (next: ExecutionMode) => {
    if (next === execution || saving) return;
    setSaving("execution"); setError("");
    try {
      await api.policy(sessionId, { execution_mode: next });
      update(await api.grants(sessionId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally { setSaving(""); }
  };
  const executionChoices = (["sandbox", "host"] as const).map((item) => ({
    value: item,
    label: tr(`web.execution_short.${item}`),
    title: tr(`web.execution.${item}`),
    ariaLabel: tr(`web.execution.${item}`),
  }));
  const networkGrants = sandboxNetworkGrants(grants);
  const sessionNetwork = networkGrants.find((grant) => grant.lifetime === "session");
  const networkDecision: Decision | null = readOnly ? "deny" : execution === "host" ? "ask" : networkGrants.length ? "allow" : "ask";
  const networkToggle = Boolean(grants) && !readOnly && execution === "sandbox" && (!networkGrants.length || Boolean(sessionNetwork));
  const toggleNetwork = async () => {
    if (!grants || !networkToggle || saving) return;
    setSaving("network"); setError("");
    try {
      if (sessionNetwork) {
        update(await api.revokeGrant(sessionId, sessionNetwork.id, sessionNetwork.source, grants.version));
      } else {
        update(await api.addGrant(sessionId, { kind: "sandbox_network" }, "session", grants.version));
      }
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        setError(tr("web.permission_conflict"));
        if (caught.latest) update(caught.latest);
        else await api.grants(sessionId).then(update).catch(() => undefined);
      } else {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    } finally { setSaving(""); }
  };
  const rows: Array<{ key: string; decision: Decision | null; note?: string; inlineNote?: string; toggle?: boolean }> = [
    { key: "read_inside", decision: decisionOf(grants, "file_read", "in_scope") },
    { key: "edit_inside", decision: readOnly ? "deny" : decisionOf(grants, "file_write", "in_scope") },
    { key: "shell_inside", decision: readOnly ? "deny" : decisionOf(grants, "shell", "in_scope"), inlineNote: tr(execution === "host" ? "web.execution.host" : "web.glance.sandboxed") },
    { key: "outside", decision: decisionOf(grants, "file_write", "outside_scope") ?? decisionOf(grants, "file_read", "outside_scope") },
    { key: "network", decision: networkDecision, note: execution === "host" ? tr("web.execution.host_network") : networkGrants.length && !sessionNetwork ? tr("web.glance.network_saved") : networkToggle ? tr("web.glance.network_enable_hint") : undefined, toggle: networkToggle },
  ];
  const blocked = (grants?.effective_policy ?? []).flatMap((policy) => policy.rules
    .filter((rule) => rule.effect === "deny")
    .map((rule) => ({ ...rule, operation: policy.operation })));
  const modeHelp = tr(mode === "bypass" && execution === "host" ? "web.execution.host_bypass" : `web.perm_help.${mode}`);
  return <div className="permissions-panel">
    <section className="perm-section perm-execution">
      <div className="section-label"><span>{tr("web.execution_mode")}</span></div>
      <SegmentedControl className="perm-execution-segment" value={execution ?? "sandbox"} ariaLabel={tr("web.execution_mode")}
        disabled={executionBusy || Boolean(saving)} options={executionChoices}
        onChange={(next) => void setExecution(next)} />
      <details className="perm-fold perm-execution-help">
        <summary title={tr("web.execution.switch_help")}>{tr("web.execution.help_summary")}</summary>
        <p className="hint">{tr(execution === "host" ? "web.execution.host_help" : "web.execution.sandbox_help")}</p>
      </details>
      {/* The sandbox's state belongs with the choice of where Shell runs. */}
      {grants && <SandboxStatusCard sessionId={sessionId} grants={grants} update={update} />}
    </section>
    <section className="perm-section perm-policy">
      <div className="section-label"><span>{tr("web.glance.title")}</span></div>
      <SegmentedControl value={mode} ariaLabel={tr("web.permission_mode")} disabled={readOnly || Boolean(saving)}
        options={MODES.map((item) => ({ value: item, label: tr(`web.perm_short.${item}`), title: tr(`web.perm_help.${item}`), ariaLabel: tr(`web.perm.${item}`) }))}
        onChange={(item) => void setMode(item)} />
      {/* The table below already says what is automatic; only the broadest mode needs its extra caveats spelled out. */}
      {mode === "bypass" ? <p className="hint perm-mode-help">{modeHelp}</p> : <p className="sr-only">{modeHelp}</p>}
      {readOnly && <p className="hint">{tr("web.perm_locked", { mode: tr(`web.mode.${interactionMode}`) })}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      <ul className="glance">
        {rows.map((row) => {
          const decision = row.decision ? tr(`web.glance.decision.${row.decision}`) : tr("web.unknown");
          return <li key={row.key} className={`glance-row ${row.decision ?? "unknown"}${row.toggle ? " has-toggle" : ""}`}>
            <span className="glance-label">
              {tr(`web.glance.${row.key}`)}
              {row.inlineNote && <small className="glance-inline-note">{row.inlineNote}</small>}
            </span>
            {/* The switch sits where the answer is, since it changes that answer. */}
            <span className="glance-outcome">
              <span className="glance-decision-line">
                <span className="glance-decision">{decision}</span>
                {row.toggle && <button type="button" role="switch" className="switch" aria-checked={row.decision === "allow"}
                  aria-label={tr(`web.glance.${row.key}`)} disabled={Boolean(saving)} onClick={() => void toggleNetwork()}
                  title={tr(sessionNetwork ? "web.glance.network_toggle_off" : "web.glance.network_toggle_on")}><span /></button>}
              </span>
            </span>
            {row.note ? <small className="glance-note">{row.note}</small> : null}
          </li>;
        })}
      </ul>
    </section>
    {grants && <PermissionGrants sessionId={sessionId} grants={grants} update={update} includeManualAdd showSandbox={false} />}
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
  if (grant.resource_kind === "sandbox_network") return t("web.grant_line.sandbox_network");
  if (grant.resource_kind === "network") return networkRuleLabel(grant.resource ?? grant, grant.target, grant.http_methods);
  if (grant.resource_kind === "tool") return t("web.grant_line.tool", { tool: grant.tool || grant.target });
  return grant.target;
}
