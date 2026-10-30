import { CaretDown, CaretRight, Check, Circle, DotsThreeVertical, Folder, Plus, Warning, X } from "@phosphor-icons/react";
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
  const [expandedHistory, setExpandedHistory] = useState<Record<string, boolean>>({});
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false);
  const workspaceMenuRef = useRef<HTMLDivElement>(null);
  const active = sessions.filter((item) => item.active);
  const selectedProject = workspaces.find((item) => item.selected)?.project_id;
  const ownerOf = (session: SessionSummary) => session.project_id ?? selectedProject;
  // A session under a workspace row is not repeated in the top-level list.
  const unowned = sessions.filter((session) => !workspaces.some((item) => item.project_id === ownerOf(session)));
  const unlisted = unowned.filter((session) => session.active);
  const history = unowned.filter((session) => !session.active);
  useEffect(() => {
    const owner = sessions.find((session) => session.session_id === selected)?.project_id;
    if (owner) setCollapsedProjects((current) => current[owner] ? { ...current, [owner]: false } : current);
  }, [selected, sessions]);
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
  const renderActive = (activeItems = active, labeled = true) => <>
    {labeled && <div className="section-label"><span>{tr("web.active_tasks")}</span>{activeItems.some((session) => session.pending_interactions || ["running", "queued", "waiting_for_input", "cancelling"].includes(session.execution || session.status || "")) && <span className="live-dot" />}</div>}
    {activeItems.map((session) => {
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
    {!activeItems.length && <p className="empty-small">{tr("web.no_active")}</p>}
  </>;
  const renderHistory = (historyItems = history, owner = "all", labeled = true) => {
    if (!historyItems.length) return null;
    const showAllHistory = expandedHistory[owner] ?? false;
    const visibleHistory = showAllHistory ? historyItems : historyItems.slice(0, 4);
    return <>
    {labeled && <div className="section-label"><span>{tr("web.recent_tasks")}</span>{historyItems.some((session) => isToday(session.saved_at)) && <span>{tr("web.today")}</span>}</div>}
    {visibleHistory.map((session) => <div className="session-entry" key={session.session_id}>
      <button className={`recent-row ${selected === session.session_id ? "selected" : ""}`} aria-current={selected === session.session_id ? "page" : undefined} onClick={() => onSelect(session)}><RecentMark outcome={session.agent_status || session.status} />{sessionTitle(session, tr)}</button>
      {selected !== session.session_id && <SessionMenu session={session} onRename={onRename} onArchive={onArchive} />}
    </div>)}
    {!showAllHistory && historyItems.length > visibleHistory.length && <button type="button" className="recent-more" onClick={() => setExpandedHistory((current) => ({ ...current, [owner]: true }))}>{tr("web.show_all_tasks", { count: historyItems.length - visibleHistory.length })}</button>}
    {showAllHistory && historyItems.length > 4 && <button type="button" className="recent-more" onClick={() => setExpandedHistory((current) => ({ ...current, [owner]: false }))}>{tr("web.show_recent_tasks")}</button>}
  </>;
  };
  return <aside className={`sidebar ${open ? "responsive-open" : ""}`}>
    <div className="sidebar-scroll">
      <button className="primary-action" onClick={onCreate} aria-label={tr("web.new_session")}><span className="primary-action-label"><Plus size={14} weight="bold" /><span>{tr("web.new_task")}</span></span><kbd>{tr("web.alt_shortcut_key", { key: "N" })}</kbd></button>
      {(!workspaces.length || unlisted.length > 0) && <div>{renderActive(unlisted)}</div>}
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
        {workspaces.map((workspace) => {
          const owned = sessions.filter((session) => ownerOf(session) === workspace.project_id);
          const activeItems = owned.filter((session) => session.active);
          const recentItems = owned.filter((session) => !session.active);
          const collapsed = Boolean(collapsedProjects[workspace.project_id]);
          return <section className="workspace-block" key={workspace.project_id} aria-label={workspace.name}>
            <div className={`workspace-project-row ${workspace.selected ? "selected" : ""}`}>
              <button
                type="button"
                className="workspace-caret"
                aria-label={tr("web.toggle_project_sessions", { name: workspace.name })}
                aria-expanded={!collapsed}
                aria-controls={`project-sessions-${workspace.project_id}`}
                onClick={() => setCollapsedProjects((current) => ({ ...current, [workspace.project_id]: !current[workspace.project_id] }))}
              >
                {collapsed ? <CaretRight size={12} weight="bold" /> : <CaretDown size={12} weight="bold" />}
              </button>
              <button type="button" className="nav-row workspace-project-name" aria-current={workspace.selected ? "true" : undefined} onClick={() => onSelectWorkspace?.(workspace.project_id)}>
                <Folder size={14} />{workspace.name}{workspace.selected && <span className="workspace-selected-dot" aria-hidden="true" />}
              </button>
            </div>
            {<div className="workspace-sessions" id={`project-sessions-${workspace.project_id}`} hidden={collapsed}>
              {activeItems.length > 0 && <div>{renderActive(activeItems, false)}</div>}
              {recentItems.length > 0 && <div>{renderHistory(recentItems, workspace.project_id, false)}</div>}
              {!owned.length && <p className="empty-small">{tr("web.no_project_sessions")}</p>}
            </div>}
          </section>;
        })}
      </div>}
      {children}
    </div>
    {footer && <div className="sidebar-foot"><span className="profile-avatar">W</span><span className="profile-copy"><strong>Wright</strong><small>{footer}</small></span><span className={`connection-latency${connectionLatency == null ? " offline" : ""}`} title={connectionLatency == null ? tr("web.connection.disconnected") : tr("web.connection.connected")} aria-label={connectionLatency == null ? tr("web.connection.disconnected") : `${connectionLatency} ms`}><i />{connectionLatency != null && <span>{connectionLatency}ms</span>}</span></div>}
  </aside>;
}
