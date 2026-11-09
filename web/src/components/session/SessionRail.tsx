import { Archive, CaretDown, CaretRight, Check, Circle, Copy, DotsThree, Folder, PencilSimple, Plus, Trash, Warning, X } from "@phosphor-icons/react";
import { useEffect, useState, type ReactNode } from "react";
import { useT } from "../../i18n";
import type { SessionSummary } from "../../types";
import { InlineRename } from "../ui/InlineRename";
import { MenuButton, useContextMenu, type MenuEntry } from "../ui/Menu";
import { formatAgo, isToday, sessionTitle } from "./session";

const TERMINAL = ["completed", "failed", "cancelled", "interrupted"];
// Only these states are worth a word in the sidebar; everything else shows how long ago.
const NOTABLE = ["running", "queued", "waiting_for_input", "cancelling", "failed", "interrupted"];

function RecentMark({ outcome }: { outcome?: string }) {
  if (outcome === "failed" || outcome === "interrupted") return <X className="recent-mark fail" size={12} weight="bold" />;
  if (outcome === "completed") return <Check className="recent-mark ok" size={12} weight="bold" />;
  return <Circle className="recent-mark" size={10} weight="bold" />;
}

type WorkspaceRow = { project_id: string; name: string; selected?: boolean };

type RowActions = {
  onRenameStart?: (session: SessionSummary) => void;
  onArchive?: (session: SessionSummary) => void;
};

function sessionMenu(session: SessionSummary, actions: RowActions, tr: ReturnType<typeof useT>): MenuEntry[] {
  const items: MenuEntry[] = [];
  if (actions.onRenameStart) items.push({ label: tr("web.rename"), icon: <PencilSimple size={13} />, onSelect: () => actions.onRenameStart?.(session) });
  items.push({ label: tr("web.copy_session_id"), icon: <Copy size={13} />, onSelect: () => { navigator.clipboard?.writeText(session.session_id).catch(() => undefined); } });
  if (actions.onArchive) items.push("separator", { label: tr("web.archive"), icon: <Archive size={13} />, onSelect: () => actions.onArchive?.(session) });
  return items;
}

function SessionRow({ session, selected, active, progress, renaming, actions, onSelect, onRenameSubmit, onRenameCancel }: {
  session: SessionSummary;
  selected: boolean;
  active: boolean;
  progress?: string;
  renaming: boolean;
  actions: RowActions;
  onSelect: () => void;
  onRenameSubmit?: (title: string) => void;
  onRenameCancel?: () => void;
}) {
  const tr = useT();
  const title = sessionTitle(session, tr);
  const menuItems = () => sessionMenu(session, actions, tr);
  const context = useContextMenu(menuItems, tr("web.session_actions"));
  const raw = session.execution || session.status || "";
  const execution = raw === "idle" && TERMINAL.includes(session.agent_status ?? "") ? session.agent_status ?? "" : raw;
  const meta = session.pending_interactions
    ? <span className="waiting-copy"><Warning size={11} weight="bold" />{tr("web.awaiting_permission")}</span>
    : selected && progress ? progress
      : active && NOTABLE.includes(execution) ? tr(`web.status.${execution}`)
        : formatAgo(session.saved_at);
  return <div className={`session-entry${selected ? " selected" : ""}`} onContextMenu={renaming ? undefined : context.onContextMenu}>
    {renaming
      ? <div className="session-row renaming">
        {active ? <span className={`status-dot ${execution}`} /> : <RecentMark outcome={session.agent_status || session.status} />}
        <InlineRename value={title} label={tr("web.rename_prompt")} onSubmit={(next) => onRenameSubmit?.(next)} onCancel={() => onRenameCancel?.()} />
      </div>
      : <button type="button" className={`session-row${selected ? " selected" : ""}`} aria-current={selected ? "page" : undefined} onClick={onSelect} onDoubleClick={() => actions.onRenameStart?.(session)} title={title}>
        {active ? <span className={`status-dot ${execution}`} /> : <RecentMark outcome={session.agent_status || session.status} />}
        <span className="session-title">{title}</span>
        {meta && <span className={`session-meta${session.pending_interactions ? " attention" : ""}`}>{meta}</span>}
      </button>}
    {!renaming && <span className="session-actions">
      <MenuButton label={tr("web.session_actions")} items={menuItems}><DotsThree size={14} weight="bold" /></MenuButton>
    </span>}
    {context.menu}
  </div>;
}

function WorkspaceHeader({ workspace, collapsed, onToggle, onSelect, onUnregister }: {
  workspace: WorkspaceRow;
  collapsed: boolean;
  onToggle: () => void;
  onSelect?: () => void;
  onUnregister?: () => void;
}) {
  const tr = useT();
  const items = (): MenuEntry[] => onUnregister ? [{ label: tr("web.unregister_workspace"), icon: <Trash size={13} />, danger: true, onSelect: onUnregister }] : [];
  const context = useContextMenu(items, tr("web.workspace_actions", { name: workspace.name }));
  return <div className={`workspace-project-row ${workspace.selected ? "selected" : ""}`} onContextMenu={onUnregister ? context.onContextMenu : undefined}>
    <button
      type="button"
      className="workspace-caret"
      aria-label={tr("web.toggle_project_sessions", { name: workspace.name })}
      aria-expanded={!collapsed}
      aria-controls={`project-sessions-${workspace.project_id}`}
      onClick={onToggle}
    >
      {collapsed ? <CaretRight size={12} weight="bold" /> : <CaretDown size={12} weight="bold" />}
    </button>
    <button type="button" className="nav-row workspace-project-name" aria-current={workspace.selected ? "true" : undefined} onClick={onSelect}>
      <Folder size={14} />{workspace.name}
    </button>
    {onUnregister && <span className="session-actions">
      <MenuButton label={tr("web.workspace_actions", { name: workspace.name })} items={items}><DotsThree size={14} weight="bold" /></MenuButton>
    </span>}
    {context.menu}
  </div>;
}

export function SessionRail({ sessions, selected, onSelect, onCreate, open, children, footer = "", connectionLatency = null, selectedProgress, workspaces = [], onSelectWorkspace, onRegisterWorkspace, onUnregisterWorkspace, renaming = null, onRenameStart, onRenameSubmit, onRenameCancel, onArchive }: {
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
  onUnregisterWorkspace?: (workspace: WorkspaceRow) => void;
  renaming?: string | null;
  onRenameStart?: (session: SessionSummary) => void;
  onRenameSubmit?: (session: SessionSummary, title: string) => void;
  onRenameCancel?: () => void;
  onArchive?: (session: SessionSummary) => void;
}) {
  const tr = useT();
  const [expandedHistory, setExpandedHistory] = useState<Record<string, boolean>>({});
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const actions: RowActions = { onRenameStart, onArchive };
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
  const row = (session: SessionSummary, active: boolean) => <SessionRow
    key={session.session_id}
    session={session}
    active={active}
    selected={selected === session.session_id}
    progress={selectedProgress}
    renaming={renaming === session.session_id}
    actions={actions}
    onSelect={() => onSelect(session)}
    onRenameSubmit={(title) => onRenameSubmit?.(session, title)}
    onRenameCancel={onRenameCancel}
  />;
  const renderActive = (activeItems: SessionSummary[], labeled = true) => <>
    {labeled && <div className="section-label"><span>{tr("web.active_tasks")}</span>{activeItems.some((session) => session.pending_interactions || ["running", "queued", "waiting_for_input", "cancelling"].includes(session.execution || session.status || "")) && <span className="live-dot" />}</div>}
    {activeItems.map((session) => row(session, true))}
    {labeled && !activeItems.length && <p className="empty-small">{tr("web.no_active")}</p>}
  </>;
  const renderHistory = (historyItems: SessionSummary[], owner = "all", labeled = true) => {
    if (!historyItems.length) return null;
    const showAllHistory = expandedHistory[owner] ?? false;
    const visibleHistory = showAllHistory ? historyItems : historyItems.slice(0, 4);
    return <>
      {labeled && <div className="section-label"><span>{tr("web.recent_tasks")}</span>{historyItems.some((session) => isToday(session.saved_at)) && <span>{tr("web.today")}</span>}</div>}
      {visibleHistory.map((session) => row(session, false))}
      {!showAllHistory && historyItems.length > visibleHistory.length && <button type="button" className="recent-more" onClick={() => setExpandedHistory((current) => ({ ...current, [owner]: true }))}>{tr("web.show_all_tasks", { count: historyItems.length - visibleHistory.length })}</button>}
      {showAllHistory && historyItems.length > 4 && <button type="button" className="recent-more" onClick={() => setExpandedHistory((current) => ({ ...current, [owner]: false }))}>{tr("web.show_recent_tasks")}</button>}
    </>;
  };
  return <aside className={`sidebar ${open ? "responsive-open" : ""}`}>
    <div className="sidebar-scroll">
      <button className="primary-action" onClick={onCreate} aria-label={tr("web.new_session")}><span className="primary-action-label"><Plus size={14} weight="bold" /><span>{tr("web.new_task")}</span></span><kbd>{tr("web.alt_shortcut_key", { key: "N" })}</kbd></button>
      {(!workspaces.length || unlisted.length > 0) && <div className="session-group">{renderActive(unlisted)}</div>}
      {history.length > 0 && <div className="session-group">{renderHistory(history)}</div>}
      <div>
        <div className="section-label workspace-heading"><span>{tr("web.workspaces")}</span>
          {onRegisterWorkspace && <button type="button" className="icon-button section-add" aria-label={tr("web.register_workspace")} title={tr("web.register_workspace")} onClick={onRegisterWorkspace}><Plus size={12} weight="bold" /></button>}
        </div>
        {workspaces.map((workspace) => {
          const owned = sessions.filter((session) => ownerOf(session) === workspace.project_id);
          const activeItems = owned.filter((session) => session.active);
          const recentItems = owned.filter((session) => !session.active);
          const collapsed = Boolean(collapsedProjects[workspace.project_id]);
          return <section className="workspace-block" key={workspace.project_id} aria-label={workspace.name}>
            <WorkspaceHeader
              workspace={workspace}
              collapsed={collapsed}
              onToggle={() => setCollapsedProjects((current) => ({ ...current, [workspace.project_id]: !current[workspace.project_id] }))}
              onSelect={() => onSelectWorkspace?.(workspace.project_id)}
              onUnregister={onUnregisterWorkspace ? () => onUnregisterWorkspace(workspace) : undefined}
            />
            <div className="workspace-sessions" id={`project-sessions-${workspace.project_id}`} hidden={collapsed}>
              {activeItems.length > 0 && renderActive(activeItems, false)}
              {recentItems.length > 0 && renderHistory(recentItems, workspace.project_id, false)}
              {!owned.length && <p className="empty-small">{tr("web.no_project_sessions")}</p>}
            </div>
          </section>;
        })}
      </div>
      {children}
    </div>
    {footer && <div className="sidebar-foot"><span className="profile-avatar">W</span><span className="profile-copy"><strong>Wright</strong></span><span className={`connection-latency${connectionLatency == null ? " offline" : ""}`} title={connectionLatency == null ? tr("web.connection.disconnected") : tr("web.connection.connected")} aria-label={connectionLatency == null ? tr("web.connection.disconnected") : `${connectionLatency} ms`}><i />{connectionLatency != null && <span>{connectionLatency}ms</span>}</span></div>}
  </aside>;
}
