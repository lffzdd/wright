import { Archive, CaretDown, CaretRight, Copy, DotsThree, Folder, PencilSimple, Plus, Trash, Warning, X } from "@phosphor-icons/react";
import { useEffect, useState, type ReactNode } from "react";
import { useT } from "../../i18n";
import type { SessionSummary } from "../../types";
import { InlineRename } from "../ui/InlineRename";
import { MenuButton, useContextMenu, type MenuEntry } from "../ui/Menu";
import { formatAgo, sessionTitle } from "./session";

// States that are still moving: they get a coloured dot and a word instead of a time.
const LIVE = ["running", "queued", "waiting_for_input", "cancelling"];
const RECENT_LIMIT = 5;

/** What a row should say about itself: a live state, a bad outcome, or nothing. */
function rowState(session: SessionSummary): string {
  const execution = session.execution || "";
  if (session.active && LIVE.includes(execution)) return execution;
  const outcome = session.agent_status || (session.active ? execution : session.status) || "";
  return outcome === "failed" || outcome === "interrupted" ? outcome : "";
}

function isWorking(session: SessionSummary): boolean {
  return Boolean(session.pending_interactions) || LIVE.includes(rowState(session));
}

/** Newest first; a session that has never been saved is newer than any saved one. */
function byRecency(sessions: SessionSummary[]): SessionSummary[] {
  const time = (session: SessionSummary) => session.saved_at ? Date.parse(session.saved_at) || 0 : Number.POSITIVE_INFINITY;
  return [...sessions].sort((a, b) => time(b) - time(a));
}

// Finished sessions carry no mark: only states that need a second look get one.
function StateMark({ state }: { state: string }) {
  if (LIVE.includes(state)) return <span className="recent-mark"><span className={`status-dot ${state}`} /></span>;
  if (state === "failed") return <X className="recent-mark fail" size={11} weight="bold" />;
  if (state === "interrupted") return <X className="recent-mark" size={11} weight="bold" />;
  return <span className="recent-mark" aria-hidden="true" />;
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

function SessionRow({ session, selected, progress, renaming, actions, onSelect, onRenameSubmit, onRenameCancel }: {
  session: SessionSummary;
  selected: boolean;
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
  const state = rowState(session);
  const meta = session.pending_interactions
    ? <span className="waiting-copy"><Warning size={11} weight="bold" />{tr("web.awaiting_permission")}</span>
    : selected && progress ? progress
      : LIVE.includes(state) ? tr(`web.status.${state}`)
        : formatAgo(session.saved_at);
  return <div className={`session-entry${selected ? " selected" : ""}`} onContextMenu={renaming ? undefined : context.onContextMenu}>
    {renaming
      ? <div className="session-row renaming">
        <StateMark state={state} />
        <InlineRename value={title} label={tr("web.rename_prompt")} onSubmit={(next) => onRenameSubmit?.(next)} onCancel={() => onRenameCancel?.()} />
      </div>
      : <button type="button" className={`session-row${selected ? " selected" : ""}`} aria-current={selected ? "page" : undefined} onClick={onSelect} onDoubleClick={() => actions.onRenameStart?.(session)} title={title}>
        <StateMark state={state} />
        <span className="session-title">{title}</span>
        {meta && <span className={`session-meta${session.pending_interactions ? " attention" : ""}`}>{meta}</span>}
      </button>}
    {!renaming && <span className="session-actions">
      <MenuButton label={tr("web.session_actions")} items={menuItems}><DotsThree size={14} weight="bold" /></MenuButton>
    </span>}
    {context.menu}
  </div>;
}

function WorkspaceHeader({ workspace, collapsed, controls, onToggle, onCreate, onUnregister }: {
  workspace: WorkspaceRow;
  collapsed: boolean;
  controls: string;
  onToggle: () => void;
  onCreate?: () => void;
  onUnregister?: () => void;
}) {
  const tr = useT();
  const items = (): MenuEntry[] => onUnregister ? [{ label: tr("web.unregister_workspace"), icon: <Trash size={13} />, danger: true, onSelect: onUnregister }] : [];
  const context = useContextMenu(items, tr("web.workspace_actions", { name: workspace.name }));
  return <div className={`workspace-project-row ${workspace.selected ? "selected" : ""}`} onContextMenu={onUnregister ? context.onContextMenu : undefined}>
    <button
      type="button"
      className="workspace-project-name"
      aria-expanded={!collapsed}
      aria-controls={controls}
      aria-current={workspace.selected ? "true" : undefined}
      title={tr("web.toggle_project_sessions", { name: workspace.name })}
      onClick={onToggle}
    >
      <span className="workspace-caret" aria-hidden="true">{collapsed ? <CaretRight size={11} weight="bold" /> : <CaretDown size={11} weight="bold" />}</span>
      <Folder size={14} />
      <span className="workspace-project-label">{workspace.name}</span>
    </button>
    <span className="workspace-actions">
      {onCreate && <button type="button" className="icon-button" aria-label={tr("web.new_session_in", { name: workspace.name })} title={tr("web.new_session_in", { name: workspace.name })} onClick={onCreate}><Plus size={13} weight="bold" /></button>}
      {onUnregister && <MenuButton label={tr("web.workspace_actions", { name: workspace.name })} items={items}><DotsThree size={14} weight="bold" /></MenuButton>}
    </span>
    {context.menu}
  </div>;
}

export function SessionRail({ sessions, selected, onSelect, onCreate, open, collapsed = false, children, nav, connectionLatency = null, selectedProgress, workspaces = [], onSelectWorkspace, onRegisterWorkspace, onUnregisterWorkspace, renaming = null, onRenameStart, onRenameSubmit, onRenameCancel, onArchive }: {
  sessions: SessionSummary[];
  selected: string | null;
  onSelect: (session: SessionSummary) => void;
  onCreate: () => void;
  /** Drawer state on compact screens. */
  open: boolean;
  /** Docked list folded away on wide screens; it slides out and leaves the tab order. */
  collapsed?: boolean;
  children?: ReactNode;
  /** Fixed navigation pinned below the scrolling session list. */
  nav?: ReactNode;
  connectionLatency?: number | null;
  selectedProgress?: string;
  workspaces?: WorkspaceRow[];
  /** Start a new session in this workspace. */
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
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const actions: RowActions = { onRenameStart, onArchive };
  const selectedProject = workspaces.find((item) => item.selected)?.project_id;
  const ownerOf = (session: SessionSummary) => session.project_id ?? selectedProject;
  // A session under a workspace row is not repeated in the top-level list.
  const unowned = sessions.filter((session) => !workspaces.some((item) => item.project_id === ownerOf(session)));
  useEffect(() => {
    const owner = sessions.find((session) => session.session_id === selected)?.project_id;
    if (owner) setCollapsedProjects((current) => current[owner] ? { ...current, [owner]: false } : current);
  }, [selected, sessions]);
  const row = (session: SessionSummary) => <SessionRow
    key={session.session_id}
    session={session}
    selected={selected === session.session_id}
    progress={selectedProgress}
    renaming={renaming === session.session_id}
    actions={actions}
    onSelect={() => onSelect(session)}
    onRenameSubmit={(title) => onRenameSubmit?.(session, title)}
    onRenameCancel={onRenameCancel}
  />;
  // One list per owner, newest first. Collapsed lists still show the selected
  // session and anything that is working, so neither can hide behind "Show all".
  const renderList = (items: SessionSummary[], owner: string) => {
    const ordered = byRecency(items);
    const showAll = expanded[owner] ?? false;
    const visible = showAll ? ordered : ordered.filter((session, index) => index < RECENT_LIMIT || session.session_id === selected || isWorking(session));
    const hidden = ordered.length - visible.length;
    return <>
      {visible.map(row)}
      {hidden > 0 && <button type="button" className="recent-more" onClick={() => setExpanded((current) => ({ ...current, [owner]: true }))}>{tr("web.show_all_tasks", { count: hidden })}</button>}
      {showAll && ordered.length > RECENT_LIMIT && <button type="button" className="recent-more" onClick={() => setExpanded((current) => ({ ...current, [owner]: false }))}>{tr("web.show_recent_tasks")}</button>}
    </>;
  };
  return <aside className={`sidebar${open ? " responsive-open" : ""}${collapsed ? " is-closed" : ""}`} inert={collapsed} aria-hidden={collapsed || undefined}>
    <div className="sidebar-inner">
    <div className="sidebar-scroll">
      <button className="primary-action" onClick={onCreate} aria-label={tr("web.new_session")}><span className="primary-action-label"><Plus size={13} weight="bold" /><span>{tr("web.new_task")}</span></span><kbd>{tr("web.alt_shortcut_key", { key: "N" })}</kbd></button>
      {(!workspaces.length || unowned.length > 0) && <div className="session-group">
        <div className="section-label"><span>{tr("web.recent_tasks")}</span>{unowned.some(isWorking) && <span className="live-dot" />}</div>
        {renderList(unowned, "all")}
        {!unowned.length && <p className="empty-small">{tr("web.no_active")}</p>}
      </div>}
      <div>
        <div className="section-label workspace-heading"><span>{tr("web.workspaces")}</span>
          {onRegisterWorkspace && <button type="button" className="icon-button section-add" aria-label={tr("web.register_workspace")} title={tr("web.register_workspace")} onClick={onRegisterWorkspace}><Plus size={12} weight="bold" /></button>}
        </div>
        {workspaces.map((workspace) => {
          const owned = sessions.filter((session) => ownerOf(session) === workspace.project_id);
          const collapsed = Boolean(collapsedProjects[workspace.project_id]);
          const listId = `project-sessions-${workspace.project_id}`;
          return <section className="workspace-block" key={workspace.project_id} aria-label={workspace.name}>
            <WorkspaceHeader
              workspace={workspace}
              collapsed={collapsed}
              controls={listId}
              onToggle={() => setCollapsedProjects((current) => ({ ...current, [workspace.project_id]: !current[workspace.project_id] }))}
              onCreate={onSelectWorkspace ? () => onSelectWorkspace(workspace.project_id) : undefined}
              onUnregister={onUnregisterWorkspace ? () => onUnregisterWorkspace(workspace) : undefined}
            />
            <div className="workspace-sessions" id={listId} hidden={collapsed}>
              {renderList(owned, workspace.project_id)}
              {!owned.length && <p className="empty-small">{tr("web.no_project_sessions")}</p>}
            </div>
          </section>;
        })}
      </div>
      {children}
    </div>
    {nav && <nav className="sidebar-nav" aria-label={tr("web.capabilities")}>{nav}</nav>}
    <div className="sidebar-foot">
      <span
        className={`connection-latency${connectionLatency == null ? " offline" : ""}`}
        title={connectionLatency == null ? undefined : `${connectionLatency} ms`}
      ><i /><span className="connection-label">{tr(connectionLatency == null ? "web.connection.disconnected" : "web.connection.connected")}</span></span>
    </div>
    </div>
  </aside>;
}
