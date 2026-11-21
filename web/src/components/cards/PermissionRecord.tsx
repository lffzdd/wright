import { ShieldCheck, X } from "@phosphor-icons/react";
import { useT } from "../../i18n";
import type { Interaction } from "../../types";

export type PermissionDecision = "allow" | "always" | "deny" | "abort";

const LABEL: Record<PermissionDecision, string> = {
  allow: "web.perm_done.allow",
  always: "web.perm_done.always",
  deny: "web.perm_done.deny",
  abort: "web.perm_done.cancel",
};

export function permissionTarget(interaction: Interaction): string {
  return interaction.command || interaction.subject || interaction.http_target || interaction.targets?.[0] || interaction.tool_name || "";
}

/** Compact record after a permission prompt is answered. Deny is red; the rest is a green shield. */
export function PermissionDoneLine({ command, decision, rule, enter, callId }: {
  command: string;
  decision: PermissionDecision;
  rule?: string;
  enter?: boolean;
  callId?: string;
}) {
  const tr = useT();
  const denied = decision === "deny" || decision === "abort";
  return <div className={`tl-indent${enter ? " enter" : ""}`} data-call-id={callId}>
    <div className={`perm-done${denied ? " deny" : ""}`}>
      {denied ? <X size={14} aria-hidden="true" /> : <ShieldCheck size={14} aria-hidden="true" />}
      {tr(LABEL[decision])} · <code className="mono">{command}</code>
      {rule && <> · {tr("web.perm_done.rule_added")} <code className="mono">{rule}</code></>}
    </div>
  </div>;
}

export function PermissionAutoLine({ rule, enter, callId }: { rule: string; enter?: boolean; callId?: string }) {
  const tr = useT();
  return <div className={`tl-indent${enter ? " enter" : ""}`} data-call-id={callId}>
    <div className="auto-ok"><ShieldCheck size={14} aria-hidden="true" />{tr("web.perm_auto.rule")} <code className="mono">{rule}</code></div>
  </div>;
}
