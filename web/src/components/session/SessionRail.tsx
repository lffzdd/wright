import { Check, Circle, DotsThreeVertical, Folder, Plus, Warning, X } from "@phosphor-icons/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useT } from "../../i18n";
import type { SessionSummary } from "../../types";
import { formatAgo, isToday, sessionTitle, taskCode } from "./session";

function RecentMark({ outcome }: { outcome?: string }) {
  if (outcome === "failed" || outcome === "interrupted") return <X className="recent-mark fail" size={12} weight="bold" />;
  if (outcome === "completed") return <Check className="recent-mark ok" size={12} weight="bold" />;
  return <Circle className="recent-mark" size={10} weight="bold" />;
}

type WorkspaceRow = { project_id: string; name: string; selected?: boolean };

function SessionMenu({ session, onRename, onArchive }: {
  session: SessionSummary;
  onRename?: (session: SessionSummary) => void;
  onArchive?: (session: SessionSummary) => void;
}) {
  const tr = useT();
  const [open, setOpen] = useState(false);
  if (!onRename && !onArchive) return null;
  return <div className="session-actions">
    <button type="button" className="icon-button" aria-label={tr("web.session_actions")} onClick={(event) => { event.stopPropagation(); setOpen((value) => !value); }}>
      <DotsThreeVertical size={12} weight="bold" />
    </button>
    {open && <div className="session-menu" role="menu">
      {onRename && <button type="button" role="menuitem" onClick={(event) => { event.stopPropagation(); setOpen(false); onRename(session); }}>{tr("web.rename")}</button>}
      {onArchive && <button type="button" role="menuitem" onClick={(event) => { event.stopPropagation(); setOpen(false); onArchive(session); }}>{tr("web.archive")}</button>}
    </div>}
  </div>;
}

export function SessionRail({ sessions, selected, onSelect, onCreate, open, children, footer = "", connectionLatency = null, selectedProgress, workspaces = [], onSelectWorkspace, onRegisterWorkspace, onUnregisterWorkspace, onRename, onArchive }: {
  sessions: SessionSummary[];
  selected: string | null;
  onSelect: (session: SessionSummary) => void;
  onCreate: () => void;
  open: boolean;
  children?: ReactNode;
  footer?: string;
  connectionLatency?: number | null;
  selectedProgress?: string;
  workspaces?: WorkspaceRow[];
  onSelectWorkspace?: (projectId: string) => void;
  onRegisterWorkspace?: () => void;
  onUnregisterWorkspace?: () => void;
  onRename?: (session: SessionSummary) => void;
  onArchive?: (session: SessionSummary) => void;
}) {
  const tr = useT();
  const [showAllHistory, setShowAllHistory] = useState(false);
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false);
  const workspaceMenuRef = useRef<HTMLDivElement>(null);
  const active = sessions.filter((item) => item.active);
  const history = sessions.filter((item) => !item.active);
  const visibleHistory = showAllHistory ? history : history.slice(0, 4);
  useEffect(() => {
    if (!workspaceMenuOpen) return;
    const closeOnOutsidePress = (event: PointerEvent) => {
      if (!workspaceMenuRef.current?.contains(event.target as Node)) setWorkspaceMenuOpen(false);
    };
    window.addEventListener("pointerdown", closeOnOutsidePress);
    return () => window.removeEventListener("pointerdown", closeOnOutsidePress);
  }, [workspaceMenuOpen]);
  const statusLabel = (session: SessionSummary) => {
    const execution = session.execution || session.status;
    if (session.pending_interactions) return tr("web.awaiting_permission");
    if (execution === "idle" && ["completed", "failed", "cancelled", "interrupted"].includes(session.agent_status ?? "")) return tr(`web.status.${session.agent_status}`);
    if (session.active && execution === "idle") return tr("web.status.paused");
    return tr(`web.status.${execution}`);
  };
  const renderActive = () => <>
    <div className="section-label"><span>{tr("web.active_tasks")}</span>{active.length > 0 && <span className="live-dot" />}</div>
    {active.map((session) => {
      const title = sessionTitle(session, tr);
      const current = selected === session.session_id;
      const execution = (session.execution || session.status) === "idle" && ["completed", "failed", "cancelled", "interrupted"].includes(session.agent_status ?? "")
        ? session.agent_status : session.execution || session.status;
      const when = formatAgo(session.saved_at) || (session.environment === "worktree" ? tr("web.isolated_option") : "");
      return <div className="session-entry" key={session.session_id}>
        <button className={current ? "task-card" : "task-row"} onClick={() => onSelect(session)}>
          {current ? <>
            <span className="task-card-top"><span><span className={`status-dot ${execution}`} /><span className="task-id">{taskCode(session.session_id)}</span></span><span className="task-step">{selectedProgress || tr(`web.status.${execution}`)}</span></span>
            <span className="task-title">{title}</span>
            <span className="task-meta"><span className={session.pending_interactions ? "waiting-copy" : ""}>{session.pending_interactions ? <><Warning size={11} weight="bold" />{tr("web.awaiting_permission")}</> : statusLabel(session)}</span><span>{when}</span></span>
          </> : <>
            <span className={`status-dot ${execution}`} /><span className="task-id">{taskCode(session.session_id, true)}</span><span className="task-title">{title}</span><span className="task-meta">{statusLabel(session)}</span>
          </>}
        </button>
        {!current && <SessionMenu session={session} onRename={onRename} onArchive={onArchive} />}
      </div>;
    })}
    {!active.length && <p className="empty-small">{tr("web.no_active")}</p>}
  </>;
  const renderHistory = () => <>
    <div className="section-label"><span>{tr("web.recent_tasks")}</span>{history.some((session) => isToday(session.saved_at)) && <span>{tr("web.today")}</span>}</div>
    {visibleHistory.map((session) => <div className="session-entry" key={session.session_id}>
      <button className="recent-row" onClick={() => onSelect(session)}><RecentMark outcome={session.agent_status || session.status} />{sessionTitle(session, tr)}</button>
      {selected !== session.session_id && <SessionMenu session={session} onRename={onRename} onArchive={onArchive} />}
    </div>)}
    {!showAllHistory && history.length > visibleHistory.length && <button type="button" className="recent-more" onClick={() => setShowAllHistory(true)}>{tr("web.show_all_tasks", { count: history.length - visibleHistory.length })}</button>}
    {showAllHistory && history.length > 4 && <button type="button" className="recent-more" onClick={() => setShowAllHistory(false)}>{tr("web.show_recent_tasks")}</button>}
  </>;
  return <aside className={`sidebar ${open ? "responsive-open" : ""}`}>
    <div className="sidebar-scroll">
      <button className="primary-action" onClick={onCreate} aria-label={tr("web.new_session")}><span className="primary-action-label"><Plus size={14} weight="bold" /><span>{tr("web.new_task")}</span></span><kbd>{tr("web.alt_shortcut_key", { key: "N" })}</kbd></button>
      <div>{renderActive()}</div>
      <div>{renderHistory()}</div>
      {workspaces.length > 0 && <div>
        <div className="section-label workspace-heading"><span>{tr("web.workspaces")}</span>
          <div className="workspace-actions" ref={workspaceMenuRef}>
            <button type="button" className="icon-button workspace-actions-trigger" aria-label={tr("web.workspaces")} aria-expanded={workspaceMenuOpen} onClick={() => setWorkspaceMenuOpen((value) => !value)}><DotsThreeVertical size={13} weight="bold" /></button>
            {workspaceMenuOpen && <div className="session-menu workspace-actions-menu" role="menu">
              {onRegisterWorkspace && <button type="button" role="menuitem" onClick={() => { setWorkspaceMenuOpen(false); onRegisterWorkspace(); }}>{tr("web.register_workspace")}</button>}
              {onUnregisterWorkspace && <button type="button" role="menuitem" onClick={() => { setWorkspaceMenuOpen(false); onUnregisterWorkspace(); }}>{tr("web.unregister_selected")}</button>}
            </div>}
          </div>
        </div>
        {workspaces.map((workspace) => <div className="workspace-block" key={workspace.project_id}>
          <button type="button" className={`nav-row ${workspace.selected ? "selected" : ""}`} onClick={() => onSelectWorkspace?.(workspace.project_id)}>
            <Folder size={14} />{workspace.name}{workspace.selected && <span className="workspace-selected-dot" aria-hidden="true" />}
          </button>
        </div>)}
      </div>}
      {children}
    </div>
    {footer && <div className="sidebar-foot"><span className="profile-avatar">W</span><span className="profile-copy"><strong>Wright</strong><small>{footer}</small></span><span className={`connection-latency${connectionLatency == null ? " offline" : ""}`} title={connectionLatency == null ? tr("web.connection.disconnected") : tr("web.connection.connected")} aria-label={connectionLatency == null ? tr("web.connection.disconnected") : `${connectionLatency} ms`}><i />{connectionLatency != null && <span>{connectionLatency}ms</span>}</span></div>}
  </aside>;
}
