import type { ReactNode } from "react";
import { Check } from "@phosphor-icons/react";
import { present, useT } from "../../i18n";
import type { EffectivePolicy, Grants } from "../../types";
import { ResourceOperations } from "./permissions";

const FILE_OPERATIONS: EffectivePolicy["operation"][] = ["file_read", "file_write"];

function ruleFingerprint(rule: { effect: string; scope: string; target: string }) {
  return `${rule.effect}:${rule.scope}:${rule.target}`;
}

function decisionClass(decision: string) {
  return decision === "allow" ? "allow" : decision === "ask" ? "ask" : decision === "deny" ? "deny" : "unknown";
}

function mergeSessionScope(policies: EffectivePolicy[]) {
  return {
    directories: [...new Set(policies.flatMap((policy) => policy.directories))],
    read_only: [...new Set(policies.flatMap((policy) => policy.read_only))],
  };
}

function visibleRulesForPolicy(
  policy: EffectivePolicy,
  globalDenyKeys: Set<string>,
  savedSessionTargets: Set<string>,
) {
  return policy.rules.filter((rule) => {
    if (rule.effect === "deny" && globalDenyKeys.has(ruleFingerprint(rule))) return false;
    if (rule.effect === "allow" && rule.scope === "session" && savedSessionTargets.has(rule.target)) return false;
    return true;
  });
}

function omittedRuleCounts(
  policy: EffectivePolicy,
  globalDenyKeys: Set<string>,
  savedSessionTargets: Set<string>,
) {
  const omittedDenies = policy.rules.filter((rule) => rule.effect === "deny" && globalDenyKeys.has(ruleFingerprint(rule))).length;
  const omittedAllows = policy.rules.filter((rule) => rule.effect === "allow" && rule.scope === "session" && savedSessionTargets.has(rule.target)).length;
  return { omittedDenies, omittedAllows };
}

function AuditSubLabel({ children }: { children: ReactNode }) {
  return <div className="audit-sub-label">{children}</div>;
}

function AuditCard({ title, children }: { title: string; children: ReactNode }) {
  return <section className="audit-operation-card">
    <h4 className="audit-operation-title">{title}</h4>
    <div className="audit-operation-body">{children}</div>
  </section>;
}

function AuditDefaults({ policy }: { policy: EffectivePolicy }) {
  const tr = useT();
  const rows = [
    { label: tr("web.policy_in_scope"), decision: policy.defaults.in_scope.decision, reason: present(policy.defaults.in_scope.reason_code, policy.defaults.in_scope.reason_params, "") },
    ...(policy.operation !== "shell" ? [{
      label: tr("web.policy_outside_scope"),
      decision: policy.defaults.outside_scope.decision,
      reason: present(policy.defaults.outside_scope.reason_code, policy.defaults.outside_scope.reason_params, ""),
    }] : []),
  ];
  return <ul className="glance glance-compact audit-defaults">
    {rows.map((row) => <li key={row.label} className={`glance-row ${decisionClass(row.decision)}`}>
      <span className="glance-label">{row.label}</span>
      <span className="glance-outcome">
        <span className="glance-decision">{tr(`web.policy_decision.${row.decision}`)}</span>
        {row.reason ? <small className="glance-note">{row.reason}</small> : null}
      </span>
    </li>)}
  </ul>;
}

function AuditRuleRow({ rule, operation }: { rule: EffectivePolicy["rules"][number]; operation?: EffectivePolicy["operation"] }) {
  const tr = useT();
  const commandLike = rule.resource_kind === "shell" || rule.resource_kind === "command" || rule.target.includes("\n") || rule.target.length > 48;
  const operationLabel = operation ? tr(`web.policy_operation.${operation}`) : "";
  return <li className="audit-rule">
    <span className="audit-rule-meta">
      {operationLabel ? `${operationLabel} · ` : ""}
      {tr(`web.policy_decision.${rule.effect}`)} · {tr(`web.lifetime.${rule.scope}`)}{rule.conditional ? ` · ${tr("web.policy_conditional")}` : ""}
    </span>
    {commandLike
      ? <div className="perm-command grant-command"><span className="perm-target">{rule.target}</span></div>
      : <span className="audit-rule-target mono">{rule.target}</span>}
    {(rule.resource_kind !== "file" && rule.resource_kind !== "directory") || rule.tool !== "*"
      ? <small><ResourceOperations kind={rule.resource_kind} operations={rule.operations} tool={rule.tool} /></small>
      : null}
    {rule.description ? <small className="audit-rule-desc">{rule.description}</small> : null}
  </li>;
}

export function PolicyAuditDetail({ grants }: { grants: Grants }) {
  const tr = useT();
  const policies = grants.effective_policy ?? [];
  const readPolicy = policies.find((item) => item.operation === "file_read");
  const writePolicy = policies.find((item) => item.operation === "file_write");
  const shellPolicy = policies.find((item) => item.operation === "shell");
  const scopePolicies = [readPolicy, writePolicy, shellPolicy].filter(Boolean) as EffectivePolicy[];
  const scope = mergeSessionScope(scopePolicies.length > 0 ? scopePolicies : policies);
  const blocked = policies.flatMap((policy) => policy.rules.filter((rule) => rule.effect === "deny").map((rule) => ({ ...rule, operation: policy.operation })));
  const globalDenyKeys = new Set(blocked.filter((rule) => rule.scope === "user").map((rule) => ruleFingerprint(rule)));
  const savedSessionTargets = new Set((grants.grants ?? []).filter((grant) => grant.lifetime === "session").map((grant) => grant.target));
  const constraints = [...new Set(policies.flatMap((policy) => policy.constraints))];
  const boundaries = grants.boundary_codes ?? [];

  let totalOmittedDenies = 0;
  let totalOmittedAllows = 0;
  for (const policy of policies) {
    const counts = omittedRuleCounts(policy, globalDenyKeys, savedSessionTargets);
    totalOmittedDenies += counts.omittedDenies;
    totalOmittedAllows += counts.omittedAllows;
  }
  const crossRefs = [
    ...(totalOmittedDenies > 0 ? [tr("web.audit.denies_listed_below", { count: totalOmittedDenies })] : []),
    ...(totalOmittedAllows > 0 ? [tr("web.audit.allows_in_saved", { count: totalOmittedAllows })] : []),
  ];

  const fileRules = FILE_OPERATIONS.flatMap((operation) => {
    const policy = policies.find((item) => item.operation === operation);
    if (!policy) return [];
    return visibleRulesForPolicy(policy, globalDenyKeys, savedSessionTargets).map((rule) => ({ rule, operation }));
  });
  const shellRules = shellPolicy ? visibleRulesForPolicy(shellPolicy, globalDenyKeys, savedSessionTargets) : [];
  const hasScope = scope.directories.length > 0 || scope.read_only.length > 0;
  const showFileAccess = Boolean(readPolicy || writePolicy);
  const showSessionPolicy = hasScope || showFileAccess || Boolean(shellPolicy);
  const showEngine = crossRefs.length > 0 || constraints.length > 0 || boundaries.length > 0;

  return <div className="policy-audit">
    {showSessionPolicy ? <AuditCard title={tr("web.audit.session_policy")}>
      {hasScope ? <div className="audit-major-section">
        <AuditSubLabel>{tr("web.audit.session_scope")}</AuditSubLabel>
        <p className="hint audit-scope-hint">{tr("web.audit.scope_hint")}</p>
        <ul className="audit-scope-list">
          {scope.directories.map((path) => <li key={path}><span className="mono">{path}</span></li>)}
          {scope.read_only.map((path) => <li key={path} className="audit-scope-readonly">{tr("web.read_only_boundary", { path })}</li>)}
        </ul>
      </div> : null}
      {showFileAccess ? <div className="audit-major-section">
        <AuditSubLabel>{tr("web.audit.file_access")}</AuditSubLabel>
        <div className="audit-file-policies">
          {readPolicy ? <div className="audit-file-policy-block">
            <AuditSubLabel>{tr("web.policy_operation.file_read")}</AuditSubLabel>
            <AuditDefaults policy={readPolicy} />
          </div> : null}
          {writePolicy ? <div className="audit-file-policy-block">
            <AuditSubLabel>{tr("web.policy_operation.file_write")}</AuditSubLabel>
            <AuditDefaults policy={writePolicy} />
          </div> : null}
        </div>
        {fileRules.length > 0 ? <div className="audit-subsection">
          <AuditSubLabel>{tr("web.audit.matching_rules")}</AuditSubLabel>
          <ul className="audit-rule-list">
            {fileRules.map(({ rule, operation }, index) => <AuditRuleRow rule={rule} operation={operation} key={`${operation}:${rule.target}:${index}`} />)}
          </ul>
        </div> : null}
      </div> : null}
      {shellPolicy ? <div className="audit-major-section">
        <AuditSubLabel>{tr("web.policy_operation.shell")}</AuditSubLabel>
        <AuditDefaults policy={shellPolicy} />
        {shellRules.length > 0 ? <div className="audit-subsection">
          <AuditSubLabel>{tr("web.audit.matching_rules")}</AuditSubLabel>
          <ul className="audit-rule-list">{shellRules.map((rule, index) => <AuditRuleRow rule={rule} key={`${rule.target}:${index}`} />)}</ul>
        </div> : null}
      </div> : null}
    </AuditCard> : null}
    {showEngine ? <AuditCard title={tr("web.audit.policy_engine")}>
      {crossRefs.length > 0 ? <ul className="audit-xref-list">
        {crossRefs.map((text) => <li key={text}>{text}</li>)}
      </ul> : null}
      {constraints.length > 0 ? <div className="audit-subsection">
        <AuditSubLabel>{tr("web.audit.evaluation_order")}</AuditSubLabel>
        <ol className="audit-steps-list">
          {constraints.map((constraint) => <li key={constraint}>{tr(`web.policy_constraint.${constraint}`)}</li>)}
        </ol>
      </div> : null}
      {boundaries.length > 0 ? <div className="audit-subsection">
        <AuditSubLabel>{tr("web.enforced_boundaries")}</AuditSubLabel>
        <ul className="boundary-list">
          {boundaries.map((item) => <li key={item}><span className="policy-check" aria-hidden="true"><Check size={12} weight="bold" /></span><span>{tr(item)}</span></li>)}
        </ul>
      </div> : null}
    </AuditCard> : null}
  </div>;
}
