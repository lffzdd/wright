/**
 * Permission approval card.
 *
 * The person has to answer one question quickly: "may Wright do this?".
 * So the card leads with a plain sentence and the exact target, shows only
 * the facts that change the answer (outside the project, network, deletes,
 * package installs), and offers three decisions in one row:
 *
 *   Allow once · Allow <the narrowest useful rule> for this session · Deny
 *
 * Every other remembered rule the server offers stays one click away under
 * "More options", as a plain list. Choices are always server option IDs;
 * the card never invents an authority the server did not offer.
 */
import { Check, Copy, Globe, ShieldWarning, X } from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { present, t, useT } from "../i18n";
import type { Interaction } from "../types";
import { useShellReadiness } from "./permissions";

type Choice = NonNullable<Interaction["choices"]>[number];
type Action = "shell" | "http" | "file_write" | "file_read" | "tool";

/** Reasons that only restate "this needs approval" add nothing to the card. */
const GENERIC_REASONS = new Set(["permission.needs_approval", ""]);
/** Flags already implied by the action sentence. */
const IMPLIED_FLAGS = new Set(["reads_files", "writes_files", "executes_shell", "network_read"]);

function actionOf(interaction: Interaction): Action {
  const tool = interaction.tool_name || "";
  const operation = interaction.operation || "";
  if (tool === "execute_command") return "shell";
  if (interaction.http_target || tool === "http_request") return "http";
  if (operation.includes("file_write") || ["write_file", "edit_file"].includes(tool)) return "file_write";
  if (operation.includes("file_read") || ["read_file", "grep", "glob", "list_directory"].includes(tool)) return "file_read";
  return "tool";
}

function targetOf(interaction: Interaction, action: Action): string {
  if (action === "shell") return interaction.command || interaction.subject || "";
  if (action === "http" && interaction.http_target) return `${interaction.http_method || "GET"} ${interaction.http_target}`;
  if (interaction.targets?.length) return [...new Set(interaction.targets)].join("\n");
  return interaction.subject || interaction.tool_name || "";
}

/** Show paths inside the project relative to it; keep everything else whole. */
export function displayPath(path: string, root?: string): string {
  if (!root || !path) return path;
  const base = root.replace(/[\\/]+$/, "");
  if (path === base) return ".";
  if (path.startsWith(`${base}/`) || path.startsWith(`${base}\\`)) return path.slice(base.length + 1);
  return path;
}

function operationsLabel(choice: Choice): string {
  const ops = choice.operations ?? [];
  if (ops.includes("file_write")) return t("web.approval.ops_write");
  if (ops.includes("file_read")) return t("web.approval.ops_read");
  return "";
}

/** One readable line for a remembered rule: what, where, and for how long. */
export function describeChoice(choice: Choice, root?: string): string {
  const kind = choice.resource_kind;
  const lifetime = t(`web.approval.lifetime.${choice.lifetime || "session"}`);
  if (kind === "directory") {
    return t("web.approval.rule_directory", { ops: operationsLabel(choice), path: `${displayPath(choice.scope, root)}/`, lifetime });
  }
  if (kind === "file") {
    return t("web.approval.rule_file", { ops: operationsLabel(choice), path: displayPath(choice.scope, root), lifetime });
  }
  if (kind === "shell") {
    const [command] = choice.scope.split(" · ");
    return t("web.approval.rule_shell", { command: command || choice.scope, lifetime });
  }
  if (kind === "network") return t("web.approval.rule_network", { target: choice.scope, lifetime });
  return `${choice.label} · ${lifetime}`;
}

/**
 * The one remembered rule worth a button of its own: session-scoped, and the
 * folder rather than the single file when the file is inside the project.
 */
export function quickChoice(choices: Choice[], outsideScope: boolean): Choice | undefined {
  const session = choices.filter((choice) => choice.lifetime === "session");
  if (!outsideScope) {
    const folder = session.find((choice) => choice.resource_kind === "directory" && choice.operations?.includes("file_write"))
      ?? session.find((choice) => choice.resource_kind === "directory");
    if (folder) return folder;
  }
  return session.find((choice) => choice.resource_kind === "file" && !choice.operations?.includes("file_write"))
    ?? session.find((choice) => choice.resource_kind === "file")
    ?? session[0];
}

const LIFETIME_ORDER: Record<string, number> = { session: 0, project: 1, user: 2 };
const KIND_ORDER: Record<string, number> = { file: 0, directory: 1, shell: 2, network: 3 };

/** Narrow and short-lived first: the list reads from least to most authority. */
function rank(choice: Choice): number {
  const lifetime = LIFETIME_ORDER[choice.lifetime || "session"] ?? 3;
  const kind = KIND_ORDER[choice.resource_kind || ""] ?? 4;
  const writes = choice.operations?.includes("file_write") ? 1 : 0;
  return lifetime * 100 + kind * 10 + writes;
}

function sentence(text: string): string {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

function isMac(): boolean {
  return typeof navigator !== "undefined" && /Mac/i.test(navigator.platform);
}

export function PermissionApproval({ sessionId, interaction, respond, root }: {
  sessionId: string;
  interaction: Interaction;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
  root?: string;
}) {
  const tr = useT();
  const [submitting, setSubmitting] = useState("");
  const [error, setError] = useState("");
  const [more, setMore] = useState(false);
  const [showGlobal, setShowGlobal] = useState(false);
  const [picked, setPicked] = useState("");
  const [network, setNetwork] = useState(false);
  const [copied, setCopied] = useState(false);
  const sending = useRef(false);
  const card = useRef<HTMLElement>(null);

  const action = actionOf(interaction);
  const target = targetOf(interaction, action);
  const shownTarget = action === "file_read" || action === "file_write"
    ? target.split("\n").map((line) => displayPath(line, root)).join("\n")
    : target;
  const risks = interaction.risk_flags ?? [];
  const outsideScope = risks.includes("path_outside_scope") || interaction.reason_code === "permission.outside_scope";
  const elevated = interaction.risk_level === "elevated";
  const shell = useShellReadiness(sessionId, action === "shell");
  const shellBlocked = action === "shell" && shell.status?.available !== true;

  const choices = interaction.choices ?? [];
  const deny = choices.find((choice) => choice.id === "deny");
  const once = choices.find((choice) => choice.id === "allow_once");
  const onceNetwork = choices.find((choice) => choice.id === "allow_once_network");
  const remembered = useMemo(
    () => choices.filter((choice) => !["deny", "allow_once", "allow_once_network"].includes(choice.id)),
    [choices],
  );
  const quick = quickChoice(remembered, outsideScope);
  const listed = remembered
    .filter((choice) => showGlobal || choice.lifetime !== "user")
    .sort((a, b) => rank(a) - rank(b));
  const hasGlobal = remembered.some((choice) => choice.lifetime === "user");
  const defaultPick = remembered.find((choice) => choice.resource_kind === "file" && choice.lifetime === "project")
    ?? remembered.find((choice) => choice.lifetime === "project")
    ?? remembered[0];
  const selected = remembered.find((choice) => choice.id === picked) ?? defaultPick;

  const reasonCode = interaction.reason_code || "";
  // The "outside this project" badge already says what outside_scope would.
  const reason = risks.includes("recursive_delete")
    ? t("web.scope_note.recursive_delete")
    : GENERIC_REASONS.has(reasonCode) || (outsideScope && reasonCode === "permission.outside_scope")
      ? "" : present(reasonCode, interaction.reason_params, interaction.reason ?? "");
  const badges = [
    ...(outsideScope ? ["path_outside_scope"] : []),
    ...risks.filter((flag) => !IMPLIED_FLAGS.has(flag) && flag !== "path_outside_scope"),
  ];

  useEffect(() => { card.current?.focus({ preventScroll: true }); }, [interaction.request_id]);

  const choose = (choiceId: string) => {
    if (sending.current) return;
    if (shellBlocked && choiceId !== "deny") return;
    sending.current = true;
    setSubmitting(choiceId);
    setError("");
    const failed = () => {
      sending.current = false;
      setSubmitting("");
      const kept = choiceId === "deny" ? t("permission.choice.deny") : choiceId === "allow_once" ? t("web.allow_once") : describeChoice(choices.find((choice) => choice.id === choiceId) ?? { id: choiceId, label: choiceId, scope: "", persistence: "" }, root);
      setError(`${t("web.decision_failed")}${t("web.kept_choice", { choice: kept })}`);
    };
    Promise.resolve(respond(interaction.request_id, choiceId)).then((accepted) => {
      if (accepted === false) failed();
    }).catch(failed);
  };
  const onceId = network && onceNetwork ? onceNetwork.id : once?.id;
  const busy = Boolean(submitting);
  const label = (id: string | undefined, text: string) => submitting && submitting === id ? tr("web.submitting") : text;

  return <div className="tl-indent">
    <section
      ref={card}
      className={`approval-card ${elevated ? "elevated" : ""}`}
      role="alert"
      aria-labelledby={`approval-${interaction.request_id}`}
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.target instanceof HTMLElement && event.target.closest("input,select,textarea")) return;
        if (event.key === "Escape" && deny) { event.preventDefault(); choose(deny.id); }
        if (event.altKey && event.key === "Enter" && onceId) { event.preventDefault(); choose(onceId); }
      }}
    >
      <header className="approval-head">
        <ShieldWarning size={16} weight="fill" className="approval-icon" aria-hidden="true" />
        <h3 id={`approval-${interaction.request_id}`}>{tr(`web.approval.ask.${action}`, { tool: interaction.tool_name || "" })}</h3>
        <span className="approval-waiting"><span className="dot pulse" />{tr("web.approval.waiting")}</span>
      </header>

      <div className="approval-body">
        <div className={`approval-target ${action === "shell" ? "is-command" : ""}`}>
          {action === "shell" && <span className="approval-prompt" aria-hidden="true">$</span>}
          <code className="perm-target" title={target}>{shownTarget}</code>
          <button type="button" className="icon-button approval-copy" aria-label={tr("web.copy_target")} onClick={() => {
            navigator.clipboard?.writeText(target).then(() => { setCopied(true); window.setTimeout(() => setCopied(false), 1200); }).catch(() => undefined);
          }}>{copied ? <Check size={12} weight="bold" /> : <Copy size={12} />}</button>
        </div>
        {action === "shell" && interaction.cwd && displayPath(interaction.cwd, root) !== "." && <p className="approval-meta">{tr("web.approval.in_folder", { path: displayPath(interaction.cwd, root) })}</p>}

        {interaction.preview && <details className="permission-preview approval-preview" open={action === "file_write"}>
          <summary>{tr("web.content_preview")}</summary>
          <pre>{interaction.preview}</pre>
          {interaction.preview_truncated && <p role="status" className="hint">{tr("web.preview_truncated")}</p>}
        </details>}

        {(badges.length > 0 || reason) && <div className="approval-facts">
          {badges.map((flag) => <span key={flag} className={`approval-badge ${flag === "path_outside_scope" || flag.includes("delete") ? "warn" : ""}`}>{tr(`web.risk_flag.${flag}`)}</span>)}
          {reason && <span className="approval-reason">{reason}</span>}
        </div>}

        {action === "shell" && shellBlocked && <div className="shell-readiness" role="status">
          <strong>{shell.status ? tr(`web.sandbox.${shell.status.state}`) : tr(shell.checked ? "web.sandbox.unavailable" : "web.sandbox.checking")}</strong>
          {shell.status?.detail && <small>{shell.status.detail}</small>}
          {shell.error && <p className="form-error" role="alert">{shell.error}</p>}
          <p>{tr("web.sandbox.approval_hint")}</p>
          {shell.status?.platform === "win32" && shell.status.state !== "initializing" && <button type="button" className="button" disabled={Boolean(shell.busy) || busy} onClick={() => void shell.initialize()}>{tr("web.sandbox.initialize")}</button>}
          {shell.error && <button type="button" className="text-button" disabled={Boolean(shell.busy)} onClick={shell.refresh}>{tr("web.sandbox.refresh")}</button>}
        </div>}

        {onceNetwork && <label className="approval-toggle">
          <input type="checkbox" checked={network} onChange={(event) => setNetwork(event.target.checked)} disabled={busy || shellBlocked} />
          <Globe size={13} aria-hidden="true" />
          <span>{tr("web.approval.with_network")}<small>{tr("web.approval.with_network_hint")}</small></span>
        </label>}

        {error && <p className="form-error" role="alert">{error}</p>}
        <small className="audit-note">{tr("web.audit_recorded")}</small>
      </div>

      <footer className="approval-actions">
        {once && <button type="button" className="perm-choice primary" disabled={busy || shellBlocked} onClick={() => onceId && choose(onceId)}>
          <Check size={13} weight="bold" />{label(onceId, tr(network ? "web.allow_once_network" : "web.allow_once"))}<kbd>{isMac() ? "⌥↵" : "Alt+Enter"}</kbd>
        </button>}
        {quick && !network && <button type="button" className="perm-choice session" disabled={busy || shellBlocked} onClick={() => choose(quick.id)} title={describeChoice(quick, root)}>
          {label(quick.id, tr("web.approval.allow_quick", { rule: describeChoice(quick, root) }))}
        </button>}
        {deny && <button type="button" className="perm-choice deny" disabled={busy} onClick={() => choose(deny.id)}>
          <X size={12} weight="bold" />{label(deny.id, tr("permission.choice.deny"))}<kbd>Esc</kbd>
        </button>}
        {remembered.length > 0 && <button type="button" className="text-button perm-more" aria-expanded={more} onClick={() => setMore((value) => !value)}>
          {tr(more ? "web.approval.fewer_options" : "web.approval.more_options")}
        </button>}
      </footer>

      {more && <div className="approval-more">
        {network && <p className="hint" role="status">{tr("web.network_once_only")}</p>}
        <fieldset disabled={busy || network}>
          <legend>{tr("web.approval.remember_legend")}</legend>
          {listed.map((choice) => <label key={choice.id} className={`approval-option ${choice.lifetime === "user" ? "global" : ""}`}>
            <input type="radio" name={`remember-${interaction.request_id}`} checked={selected?.id === choice.id} onChange={() => setPicked(choice.id)} />
            <span>{sentence(describeChoice(choice, root))}</span>
          </label>)}
        </fieldset>
        <div className="approval-more-actions">
          {hasGlobal && <button type="button" className="text-button" onClick={() => {
            if (showGlobal && selected?.lifetime === "user") setPicked("");
            setShowGlobal(!showGlobal);
          }}>{tr(showGlobal ? "web.approval.hide_global" : "web.approval.show_global")}</button>}
          {selected?.lifetime === "user" && <small className="approval-warn">{tr("web.cross_project")}</small>}
          <button type="button" className="button primary" disabled={busy || shellBlocked || network || !selected} onClick={() => selected && choose(selected.id)}>
            {tr("web.remember_allow")}
          </button>
        </div>
      </div>}
    </section>
  </div>;
}
