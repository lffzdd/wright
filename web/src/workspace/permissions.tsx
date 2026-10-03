import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { useT } from "../i18n";
import type { Grants, SandboxStatus } from "../types";

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
    {sandbox && <article className="policy-card">
      <strong>{tr("web.sandbox")}: {sandbox.provider}</strong>
      <p role="status">{tr(`web.sandbox.${sandbox.state}`)}</p>
      {sandbox.detail && <small>{sandbox.detail}</small>}
      {sandbox.platform === "win32" && sandbox.state !== "initializing" && <div className="interaction-actions">
        {!sandbox.available && <button className="button" disabled={busy} onClick={() => mutate(async () => { await api.sandbox(sessionId, "setup"); return api.grants(sessionId); }, "web.sandbox.requested")}>{tr("web.sandbox.initialize")}</button>}
        {sandbox.state !== "setup_required" && <button className="text-button" disabled={busy} onClick={() => mutate(async () => { await api.sandbox(sessionId, "cleanup"); return api.grants(sessionId); }, "web.sandbox.cleaned")}>{tr("web.sandbox.cleanup")}</button>}
      </div>}
    </article>}
    <div className="card-kicker"><span>{tr("web.saved_grants")}</span><span>{grants.grants?.length ?? 0}</span></div>
    {(grants.grants ?? []).map((grant) => <article className={`policy-card ${grant.lifetime === "user" ? "warn" : ""}`} key={`${grant.source}:${grant.id}`}>
      <div><strong className="mono policy-path">{grant.target}</strong><button type="button" disabled={busy} className="text-button" aria-label={tr("web.revoke_target", { target: grant.target })} onClick={() => mutate(() => api.revokeGrant(sessionId, grant.id, grant.source, grants.version), "web.grant_revoked")}>{tr("web.revoke")}</button></div>
      <small><ResourceOperations kind={grant.resource_kind} operations={grant.operations} tool={grant.tool} /> · {tr(`web.lifetime.${grant.lifetime}`)}</small>
      {grant.http_methods?.length ? <small className="mono">{grant.http_methods.join(", ")}</small> : null}
      {grant.cwd && <small className="mono">{grant.cwd}</small>}
      {grant.lifetime === "user" && <small>{tr("web.cross_project")}</small>}
    </article>)}
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
  </section>;
}
