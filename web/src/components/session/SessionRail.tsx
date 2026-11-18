import { Archive, Copy, PencilSimple, Plus, Trash, XCircle } from "@phosphor-icons/react";
import { BranchIcon, ChevronsIcon, DotsIcon } from "../ui/chromeIcons";
import { useRef, useState, type ReactNode } from "react";
import { useT } from "../../i18n";
import type { SessionSummary } from "../../types";
import { InlineRename } from "../ui/InlineRename";
import { MenuButton, MenuPopover, rectOf, useContextMenu, type Anchor, type MenuEntry } from "../ui/Menu";
import { DATE_GROUPS, dateGroup, rowTime, sessionTitle, shortPath, type DateGroup } from "./session";

// States that are still moving: they get a coloured dot and a word instead of a time.
const LIVE = ["running", "queued", "waiting_for_input", "cancelling"];

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

function sessStatus(state: string, pending?: boolean): "running" | "waiting" | "error" | "idle" {
  if (pending) return "waiting";
  if (state === "running" || state === "cancelling") return "running";
  if (state === "waiting_for_input" || state === "queued") return "waiting";
  if (state === "failed" || state === "interrupted") return "error";
  return "idle";
}

// Finished sessions carry no mark: only states that need a second look get one.
function StateMark({ state, pending }: { state: string; pending?: boolean }) {
  const status = sessStatus(state, pending);
  const extras = status === "running" ? " status-dot running" : status === "error" ? " fail" : "";
  return <span className={`recent-mark sdot st-${status}${extras}`} aria-hidden="true" />;
}

type WorkspaceRow = { project_id: string; name: string; root?: string; selected?: boolean };

type RowActions = {
  onRenameStart?: (session: SessionSummary) => void;
  onArchive?: (session: SessionSummary) => void;
  onClose?: (session: SessionSummary) => void;
};

function sessionMenu(session: SessionSummary, actions: RowActions, tr: ReturnType<typeof useT>): MenuEntry[] {
  const items: MenuEntry[] = [];
  if (actions.onRenameStart) items.push({ label: tr("web.rename"), icon: <PencilSimple size={13} />, onSelect: () => actions.onRenameStart?.(session) });
  items.push({ label: tr("web.copy_session_id"), icon: <Copy size={13} />, onSelect: () => { navigator.clipboard?.writeText(session.session_id).catch(() => undefined); } });
  if (actions.onArchive || actions.onClose) items.push("separator");
  if (actions.onArchive) items.push({ label: tr("web.archive"), icon: <Archive size={13} />, onSelect: () => actions.onArchive?.(session) });
  if (actions.onClose) items.push({ label: tr("web.close_session"), icon: <XCircle size={13} />, danger: true, onSelect: () => actions.onClose?.(session) });
  return items;
}

function SessionRow({ session, selected, renaming, actions, onSelect, onRenameSubmit, onRenameCancel }: {
  session: SessionSummary;
  selected: boolean;
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
  const pending = Boolean(session.pending_interactions);
  const status = sessStatus(state, pending);
  const statusLabel = status === "idle" ? "" : tr(`web.sess.${status}`);
  const branch = session.branch_name?.trim() || "";
  const time = rowTime(session.saved_at);
  return <div className={`session-entry${selected ? " selected" : ""}`} onContextMenu={renaming ? undefined : context.onContextMenu}>
    {renaming
      ? <div className="session-row renaming">
        <StateMark state={state} pending={pending} />
        <InlineRename value={title} label={tr("web.rename_prompt")} onSubmit={(next) => onRenameSubmit?.(next)} onCancel={() => onRenameCancel?.()} />
      </div>
      : <button type="button" className={`session-row sess${selected ? " selected active" : ""}`} aria-current={selected ? "page" : undefined} onClick={onSelect} onDoubleClick={() => actions.onRenameStart?.(session)} title={title}>
        <StateMark state={state} pending={pending} />
        <span className="s-main">
          <span className="session-title s-title">{title}</span>
          {(statusLabel || branch || time) && <span className={`session-meta s-sub${pending ? " attention" : ""}`}>
            {statusLabel && <span className={`st-label ${status}`}>{statusLabel}</span>}
            {statusLabel && " · "}
            {branch && <><BranchIcon size={11} /><span>{branch}</span>{" · "}</>}
            {time && <span className="st-label">{time}</span>}
          </span>}
        </span>
      </button>}
    {!renaming && <span className="session-actions s-more">
      <MenuButton className="icon-button" label={tr("web.session_actions")} items={menuItems}><DotsIcon size={14} /></MenuButton>
    </span>}
    {context.menu}
  </div>;
}

/** Initial for the project tile: the first letter or digit of the name. */
function projectInitial(name: string): string {
  const match = name.match(/[\p{L}\p{N}]/u);
  return (match ? match[0] : "?").toUpperCase();
}

/**
 * The project this sidebar is scoped to, pinned at the top like the reference
 * `.workspace` button. Opens a menu to switch, add or remove projects.
 */
function ProjectSwitcher({ workspaces, current, fallbackName, fallbackRoot, working, onSwitch, onRegister, onUnregister }: {
  workspaces: WorkspaceRow[];
  current?: WorkspaceRow;
  fallbackName: string;
  fallbackRoot?: string;
  /** Working sessions per project id. */
  working: Map<string, number>;
  onSwitch?: (projectId: string) => void;
  onRegister?: () => void;
  onUnregister?: (workspace: WorkspaceRow) => void;
}) {
  const tr = useT();
  const trigger = useRef<HTMLButtonElement>(null);
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const close = useRef((restoreFocus: boolean) => {
    setAnchor(null);
    if (restoreFocus) trigger.current?.focus();
  }).current;
  const name = current?.name ?? fallbackName;
  const root = current?.root ?? fallbackRoot ?? "";
  const elsewhere = [...working.entries()].some(([id, count]) => id !== current?.project_id && count > 0);
  const interactive = Boolean(onSwitch || onRegister);
  const items = (): MenuEntry[] => {
    const entries: MenuEntry[] = workspaces.map((workspace) => {
      const count = working.get(workspace.project_id) ?? 0;
      const path = workspace.root ? shortPath(workspace.root) : "";
      return {
        label: workspace.name,
        hint: [path, count ? tr("web.project_working", { count }) : ""].filter(Boolean).join(" · ") || undefined,
        icon: <span className={`ws-mini${count ? " working" : ""}`} aria-hidden="true">{projectInitial(workspace.name)}</span>,
        checked: workspace.project_id === current?.project_id,
        onSelect: () => { if (workspace.project_id !== current?.project_id) onSwitch?.(workspace.project_id); },
      };
    });
    if (onRegister) entries.push("separator", { label: tr("web.add_project"), icon: <Plus size={14} />, onSelect: onRegister });
    if (onUnregister && current) {
      if (!onRegister) entries.push("separator");
      entries.push({ label: tr("web.remove_project", { name: current.name }), icon: <Trash size={14} />, danger: true, onSelect: () => onUnregister(current) });
    }
    return entries;
  };
  return <>
    <button
      ref={trigger}
      type="button"
      className="ws-switch workspace"
      aria-label={`${tr("web.switch_project")}: ${name}`}
      aria-haspopup={interactive ? "menu" : undefined}
      aria-expanded={interactive ? anchor !== null : undefined}
      disabled={!interactive}
      title={root || name}
      onClick={(event) => {
        const rect = rectOf(event.currentTarget);
        setAnchor((open) => open ? null : rect);
      }}
    >
      <span className="ws-logo" aria-hidden="true">{projectInitial(name)}</span>
      <span className="ws-text">
        <span className="ws-name crumb">{name}</span>
        {root && <span className="ws-path">{shortPath(root)}</span>}
      </span>
      {elsewhere && <span className="ws-elsewhere" title={tr("web.other_projects_working")} aria-label={tr("web.other_projects_working")} />}
      {interactive && <span className="ws-caret"><ChevronsIcon size={16} /></span>}
    </button>
    {anchor && <MenuPopover anchor={anchor} items={items()} onClose={close} label={tr("web.projects")} align="start" matchWidth className="ws-menu" />}
  </>;
}

export function SessionRail({ sessions, selected, onSelect, onCreate, open, collapsed = false, children, nav, projectName, projectRoot, search, footer, connectionLatency = null, workspaces = [], onSelectWorkspace, onRegisterWorkspace, onUnregisterWorkspace, renaming = null, onRenameStart, onRenameSubmit, onRenameCancel, onArchive, onClose }: {
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
  /** Shown in the switcher when no workspace registry is available. */
  projectName?: string;
  projectRoot?: string;
  /** Search trigger, pinned under new-session like the reference `.sb-search`. */
  search?: ReactNode;
  /** Controls beside the connection state at the foot. */
  footer?: ReactNode;
  connectionLatency?: number | null;
  workspaces?: WorkspaceRow[];
  /** Switch the sidebar (and a new draft) to this workspace. */
  onSelectWorkspace?: (projectId: string) => void;
  onRegisterWorkspace?: () => void;
  onUnregisterWorkspace?: (workspace: WorkspaceRow) => void;
  renaming?: string | null;
  onRenameStart?: (session: SessionSummary) => void;
  onRenameSubmit?: (session: SessionSummary, title: string) => void;
  onRenameCancel?: () => void;
  onArchive?: (session: SessionSummary) => void;
  onClose?: (session: SessionSummary) => void;
}) {
  const tr = useT();
  const actions: RowActions = { onRenameStart, onArchive, onClose };
  const current = workspaces.find((item) => item.selected);
  const ownerOf = (session: SessionSummary) => session.project_id ?? current?.project_id;
  const registered = (id?: string) => workspaces.some((item) => item.project_id === id);
  // The list shows the current project. Sessions whose project is not registered
  // have no other home, so they stay visible here instead of disappearing.
  const visible = current
    ? sessions.filter((session) => ownerOf(session) === current.project_id || !registered(ownerOf(session)))
    : sessions;
  const working = new Map<string, number>();
  for (const session of sessions) {
    const owner = ownerOf(session);
    if (owner && isWorking(session)) working.set(owner, (working.get(owner) ?? 0) + 1);
  }
  const groups = new Map<DateGroup, SessionSummary[]>();
  for (const session of byRecency(visible)) {
    const group = dateGroup(session.saved_at);
    groups.set(group, [...(groups.get(group) ?? []), session]);
  }
  const row = (session: SessionSummary) => <SessionRow
    key={session.session_id}
    session={session}
    selected={selected === session.session_id}
    renaming={renaming === session.session_id}
    actions={actions}
    onSelect={() => onSelect(session)}
    onRenameSubmit={(title) => onRenameSubmit?.(session, title)}
    onRenameCancel={onRenameCancel}
  />;
  return <aside className={`sidebar${open ? " responsive-open sb-open" : ""}${collapsed ? " is-closed" : ""}`} inert={collapsed} aria-hidden={collapsed || undefined}>
    <div className="sidebar-inner sb-inner">
    <div className="sidebar-head sb-top">
      <ProjectSwitcher
        workspaces={workspaces}
        current={current}
        fallbackName={projectName ?? tr("web.local")}
        fallbackRoot={projectRoot}
        working={working}
        onSwitch={onSelectWorkspace}
        onRegister={onRegisterWorkspace}
        onUnregister={onUnregisterWorkspace}
      />
    </div>
    <button type="button" className="primary-action new-btn" onClick={onCreate} aria-label={tr("web.new_session")}><span className="primary-action-label"><Plus size={13} weight="bold" /><span>{tr("web.new_task")}</span></span><kbd>{tr("web.alt_shortcut_key", { key: "N" })}</kbd></button>
    {search}
    <nav className="sidebar-scroll sb-list" aria-label={tr("web.recent_tasks")}>
      {DATE_GROUPS.filter((group) => groups.has(group)).map((group) => <section className="sb-section" key={group} aria-label={tr(`web.group.${group}`)}>
        <div className="sb-group">{tr(`web.group.${group}`)}</div>
        {groups.get(group)!.map(row)}
      </section>)}
      {!visible.length && <p className="sb-empty">{tr("web.no_project_sessions")}</p>}
      {children}
    </nav>
    {nav && <nav className="sidebar-nav" aria-label={tr("web.capabilities")}>{nav}</nav>}
    <div className="sidebar-foot sb-foot">
      <span
        className={`connection-latency${connectionLatency == null ? " offline" : ""}`}
        title={connectionLatency == null ? undefined : `${connectionLatency} ms`}
      ><i /><span className="connection-label">{tr(connectionLatency == null ? "web.connection.disconnected" : "web.connection.connected")}</span></span>
      {footer && <span className="sidebar-foot-actions">{footer}</span>}
    </div>
    </div>
  </aside>;
}
