import { X } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../../api";
import { t, useT } from "../../i18n";
import type { ExecutionMode, Grants, PermissionResource, SandboxStatus } from "../../types";
import { InlineSelect } from "../ui/InlineSelect";
import { SegmentedControl } from "../ui/SegmentedControl";
import { commandArguments, commandRuleDetails, commandRuleLabel, networkRuleLabel } from "./permission-resource";
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

type Grant = Grants["grants"][number];
type RuleEffect = "allow" | "ask" | "deny";
/** A configured ask/deny rule. User and project rows can be removed unless they are built-in. */
export type PolicyRuleRow = { effect: RuleEffect; scope: string; target: string; description: string; conditional: boolean; id: string; locked: boolean };

/**
 * A saved grant as a short pattern, programs named rather than pathed. The
 * full sentence stays in the row's tooltip. Kinds without a natural pattern
 * keep their sentence and are not set in monospace.
 */
function grantPattern(grant: Grant): { text: string; mono: boolean } {
  const resource: PermissionResource = grant.resource ?? grant;
  if (grant.resource_kind === "command") {
    const argv = resource.argv ?? [];
    if (resource.programs?.length) return { text: `${resource.programs.map(programName).join(", ")} *`, mono: true };
    if (resource.command_match === "executable") return { text: `${programName(argv[0] || grant.target)} *`, mono: true };
    return { text: argv.length ? `${commandArguments([programName(argv[0]), ...argv.slice(1)])} *` : `${programName(grant.target)} *`, mono: true };
  }
  if (grant.resource_kind === "shell") return { text: grant.target, mono: true };
  if (grant.resource_kind === "directory") return { text: grant.recursive ? `${grant.target.replace(/[\\/]+$/, "")}/**` : grant.target, mono: true };
  if (grant.resource_kind === "file") return { text: grant.target, mono: true };
  if (grant.resource_kind === "network") {
    if (resource.tool_name === "*") return { text: grant.target, mono: true };
    return { text: `${((resource.methods ?? grant.http_methods) ?? ["GET"]).join(",")} ${grant.target}`, mono: true };
  }
  if (grant.resource_kind === "tool") return { text: grant.tool || grant.target, mono: true };
  return { text: describeGrant(grant), mono: false };
}

/** What a grant covers besides its pattern: where it applies, and which operations. */
function grantDetail(grant: Grant): string {
  const parts = [t(`web.grant_group.${grant.lifetime}`)];
  if (grant.resource_kind === "directory" || grant.resource_kind === "file" || grant.resource_kind === "tool") {
    parts.push(grant.operations.map((operation) => t(`web.operation.${operation}`)).join(", "));
  }
  if (grant.tool && grant.tool !== "*" && grant.resource_kind !== "tool" && grant.resource_kind !== "command" && grant.resource_kind !== "shell") parts.push(t("web.tool_restriction", { tool: grant.tool }));
  return parts.filter(Boolean).join(" · ");
}

/** Everything the short row leaves out, for its tooltip. */
function grantTitle(grant: Grant): string {
  const parts = [describeGrant(grant)];
  if (grant.resource_kind === "command") parts.push(commandRuleDetails(grant.resource ?? grant));
  if (grant.resource_kind === "shell" && grant.cwd) parts.push(t("web.approval.in_folder", { path: grant.cwd }));
  if (grant.resource_kind === "command" || grant.resource_kind === "shell") parts.push(t((grant.resource?.execution_mode ?? "sandbox") === "host" ? "web.execution.host_boundary" : "web.approval.sandbox_boundary"));
  if (grant.lifetime === "user") parts.push(t("web.cross_project"));
  return parts.join(" · ");
}

const GRANT_LIFETIMES = ["session", "project", "user"] as const;

function useGrantMutate(sessionId: string, update: (value: Grants) => void) {
  const tr = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const changing = useRef(false);
  const revision = useRef(0);
  const mutate = async (operation: () => Promise<Grants>, success: string) => {
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

function RuleRow({ effect, pattern, mono, detail, title, revoke }: { effect: RuleEffect; pattern: string; mono: boolean; detail: string; title?: string; revoke?: { label: string; busy: boolean; run: () => void } }) {
  const tr = useT();
  return <li className={`rule-row ${effect}`} title={title}>
    <span className="status-dot" aria-hidden="true" />
    <span className="rule-row-body">
      <span className={`rule-row-pattern${mono ? " mono" : ""}`}>{pattern}</span>
      {detail ? <small>{detail}</small> : null}
    </span>
    <span className="rule-row-effect">{tr(`web.rule_effect.${effect}`)}</span>
    {revoke ? <button type="button" className="icon-button icon-btn sm rule-row-revoke" disabled={revoke.busy} aria-label={revoke.label} title={tr("web.revoke")} onClick={revoke.run}><X size={12} weight="bold" aria-hidden="true" /></button> : <span className="rule-row-revoke-space" aria-hidden="true" />}
  </li>;
}

export function PermissionGrantManualAdd({ sessionId, grants, update }: { sessionId: string; grants: Grants; update: (value: Grants) => void }) {
  const tr = useT();
  const [path, setPath] = useState("");
  const [kind, setKind] = useState("file");
  const [write, setWrite] = useState(false);
  const [lifetime, setLifetime] = useState("project");
  const { mutate, busy, error, notice } = useGrantMutate(sessionId, update);
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
          void mutate(() => api.addGrant(sessionId, { kind, path: path.trim(), operations: write ? ["file_read", "file_write"] : ["file_read"] }, lifetime, grants.version), "web.grant_added").then((ok) => { if (ok) setPath(""); });
        }}>
          <InlineSelect label={tr("web.scope_label")} value={kind} onChange={setKind} options={[
            { value: "file", label: tr("web.grant_kind.file") },
            { value: "directory", label: tr("web.grant_kind.directory") },
          ]} />
          <label>{tr("web.path")}<input className="grant-path-input" required value={path} onChange={(event) => setPath(event.target.value)} placeholder={tr("web.path")} aria-label={tr("web.path")} /></label>
          <div className="grant-form-row">
            <InlineSelect label={tr("web.operations")} value={write ? "write" : "read"} onChange={(next) => setWrite(next === "write")} options={[
              { value: "read", label: tr("web.read_only") },
              { value: "write", label: tr("web.read_write") },
            ]} />
            <InlineSelect label={tr("web.lifetime")} value={lifetime} onChange={setLifetime} options={[
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
  const { mutate, busy, error, notice } = useGrantMutate(sessionId, update);
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

export function PermissionGrants({ sessionId, grants, update, includeManualAdd = false, showSandbox = true, policyRules = [] }: { sessionId: string; grants: Grants; update: (value: Grants) => void; includeManualAdd?: boolean; showSandbox?: boolean; policyRules?: PolicyRuleRow[] }) {
  const tr = useT();
  const sandbox = grants.sandbox;
  const { mutate, busy, error, notice, revision, changing } = useGrantMutate(sessionId, update);
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
  // Saved grants first (narrowest lifetime first), then the policy's ask and deny rules.
  const ordered = GRANT_LIFETIMES.flatMap((lifetime) => grantItems.filter((grant) => grant.lifetime === lifetime));
  const asks = policyRules.filter((rule) => rule.effect === "ask");
  const denies = policyRules.filter((rule) => rule.effect === "deny");
  const total = ordered.length + asks.length + denies.length;
  const policyRow = (rule: PolicyRuleRow) => {
    const removable = Boolean(rule.id) && !rule.locked && (rule.scope === "user" || rule.scope === "project");
    return <RuleRow key={`${rule.effect}:${rule.scope}:${rule.target}`} effect={rule.effect} pattern={rule.target} mono
      detail={[rule.locked ? tr("web.policy_builtin") : tr(`web.grant_group.${rule.scope}`), rule.conditional && !rule.locked ? tr("web.policy_conditional") : ""].filter(Boolean).join(" · ")} title={rule.description || undefined}
      revoke={removable ? { label: tr("web.revoke_rule", { target: rule.target }), busy, run: () => void mutate(() => api.revokeGrant(sessionId, rule.id, rule.scope, grants.version), "web.grant_revoked") } : undefined} />;
  };
  return <>
  <section className="perm-section permission-manager">
    {error && <p className="form-error" role="alert">{error}</p>}
    {notice && <p className="hint" role="status">{notice}</p>}
    {showSandbox && <SandboxStatusCard sessionId={sessionId} grants={grants} update={update} />}
    <div className="section-label"><span>{tr("web.rules.title")}</span><span className="section-count">{total}</span></div>
    {!total && <p className="empty-small">{tr("web.no_saved_grants")}</p>}
    {total > 0 && <ul className="rule-list">
      {ordered.map((grant) => {
        const pattern = grantPattern(grant);
        return <RuleRow key={`${grant.source}:${grant.id}`} effect="allow" pattern={pattern.text} mono={pattern.mono} detail={grantDetail(grant)} title={grantTitle(grant)}
          revoke={{ label: tr("web.revoke_target", { target: grant.target }), busy, run: () => void mutate(() => api.revokeGrant(sessionId, grant.id, grant.source, grants.version), "web.grant_revoked") }} />;
      })}
      {asks.map(policyRow)}
      {denies.map(policyRow)}
    </ul>}
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
  // One row per rule: the same pattern can be listed under several operations.
  const policyRules = [...new Map((grants?.effective_policy ?? []).flatMap((policy) => policy.rules)
    .filter((rule) => rule.effect === "ask" || rule.effect === "deny")
    .map((rule): [string, PolicyRuleRow] => [`${rule.effect}:${rule.scope}:${rule.target}`, { effect: rule.effect as RuleEffect, scope: rule.scope, target: rule.target, description: rule.description, conditional: rule.conditional, id: typeof rule.rule.id === "string" ? rule.rule.id : "", locked: Boolean(rule.locked || rule.rule.source === "builtin") }])).values()];
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
            {/* Notes sit on their own line so the label and the decision never wrap into three columns. */}
            {row.note || row.inlineNote ? <small className="glance-note">{[row.inlineNote, row.note].filter(Boolean).join(" · ")}</small> : null}
          </li>;
        })}
      </ul>
    </section>
    {grants && <PermissionGrants sessionId={sessionId} grants={grants} update={update} includeManualAdd showSandbox={false} policyRules={policyRules} />}
    {grants && grants.effective_policy.length > 0 && <div className="perm-advanced">
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
