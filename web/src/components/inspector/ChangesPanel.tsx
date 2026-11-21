import { ArrowClockwise, ArrowCounterClockwise, ArrowsInLineVertical, ArrowsOutLineVertical, CaretRight, Check, Eye, Info, Minus } from "@phosphor-icons/react";
import { confirmDialog } from "../ui/dialogs";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../../api";
import { DiffViewer, type DiffFeedbackLine } from "../code/DiffViewer";
import { useT } from "../../i18n";
import { SegmentedControl } from "../ui/SegmentedControl";
import type { ChangeAction, ChangePatch, ChangesSummary, GitChange, GitChangeScope, ReviewChange, ViewState } from "../../types";
import { ActionSlots, ChangeFold } from "./ChangeFold";
import { CommitBar } from "./CommitBar";
import { isGenerated, layoutRows } from "./changeTree";

type Row = GitChange & { review?: ReviewChange };
type Group = GitChangeScope | "session";
type GroupListing = { revision?: string; rows: Row[] };
type Listing = { owner: string; view: ChangeView; git?: boolean; groups: Partial<Record<Group, GroupListing>> };
type ChangeView = "git" | "session";
type Selection = { group: Group; path: string };

const views: ChangeView[] = ["git", "session"];
// Staged first, as it is what a commit will contain.
const gitGroups: GitChangeScope[] = ["staged", "unstaged"];
const message = (error: unknown) => error instanceof Error ? error.message : String(error);
const keyOf = (selection: Selection) => `${selection.group}\u0000${selection.path}`;
/** One open file's diff. `refresh` is the listing refresh it was fetched for, so a refresh refetches it. */
type PatchState = { refresh: number; loading: boolean; value?: ChangePatch; error?: string };
/** Past this many files, "expand all" would fire too many diff requests at once. */
const EXPAND_ALL_LIMIT = 30;
const splitPath = (path: string) => { const index = path.lastIndexOf("/"); return index < 0 ? ["", path] : [path.slice(0, index + 1), path.slice(index + 1)]; };
/** Git reports untracked files as "??"; they read as additions. */
const kindLetter = (status: string) => status[0] === "?" ? "A" : (status[0] || "M");
const sumStats = (rows: Array<{ additions?: number | null; deletions?: number | null }>) => rows.reduce((sum, row) => ({ add: sum.add + (row.additions ?? 0), del: sum.del + (row.deletions ?? 0) }), { add: 0, del: 0 });
/** Five-block additions/deletions bar, as code hosts show it. */
export function DiffBar({ add, del }: { add: number; del: number }) {
  const total = add + del;
  const green = total ? Math.round((add / total) * 5) : 0;
  const red = total ? Math.min(5 - green, Math.max(del ? 1 : 0, Math.round((del / total) * 5))) : 0;
  return <span className="diff-bar" aria-hidden="true">{Array.from({ length: 5 }, (_, index) => <i key={index} className={index < green ? "add" : index < green + red ? "del" : ""} />)}</span>;
}

export function ChangesPanel({ sessionId, state, onCount, onWorkingChanges, onFeedback, focus, onOpenFile }: {
  sessionId: string;
  /** A file to open once the listing has it; a new request reopens it. */
  focus?: { path: string; request: number } | null;
  state: ViewState;
  onCount: (count: number) => void;
  onWorkingChanges?: (changes: ChangesSummary["changes"]) => void;
  onFeedback?: (feedback: string) => void;
  onOpenFile?: (path: string) => void;
}) {
  const tr = useT();
  const [view, setView] = useState<ChangeView>("git");
  const [listing, setListing] = useState<Listing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  // Several files can be open at once, as in a code host's review page.
  const [expanded, setExpanded] = useState<Selection[]>([]);
  const [patches, setPatches] = useState<Record<string, PatchState>>({});
  const [actionPending, setActionPending] = useState(false);
  const [note, setNote] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [feedback, setFeedback] = useState<{ group: Group; path: string; line?: DiffFeedbackLine } | null>(null);
  const [feedbackText, setFeedbackText] = useState("");
  const generation = useRef(0);
  const countRef = useRef(onCount);
  const workingRef = useRef(onWorkingChanges);
  countRef.current = onCount;
  workingRef.current = onWorkingChanges;
  const sessionBusy = state.connection !== "connected" || ["running", "queued", "waiting_for_input", "cancelling"].includes(state.session.execution ?? state.session.status);
  const wasBusy = useRef(false);

  useEffect(() => { setCommitMessage(""); }, [sessionId]);

  useEffect(() => {
    ++generation.current;
    setListing(null); setExpanded([]); setPatches({}); setNote("");
    setActionPending(false); setFeedback(null); setFeedbackText("");
    countRef.current(0);
    return () => { ++generation.current; };
  }, [sessionId, view]);

  useEffect(() => {
    if (wasBusy.current && !sessionBusy) setRefresh((value) => value + 1);
    wasBusy.current = sessionBusy;
  }, [sessionBusy]);

  useEffect(() => {
    let current = true;
    if (!listing || listing.view !== view || listing.owner !== sessionId) setLoading(true);
    setError("");
    const read = async (): Promise<Listing> => {
      if (view === "session") {
        const result = await api.review(sessionId);
        // A session diff is about recorded Agent writes, not every old dirty
        // file saved in the journal baseline.
        const rows = result.changes.filter((item) => item.origin === "task" || ["task", "accepted", "conflict", "unsafe"].includes(item.state)).filter((item) => !(item.display_kind === "rename" && item.kind === "delete" && result.changes.some((other) => other.path === item.rename_with && other.rename_with === item.path))).map((item) => ({
          path: item.path,
          old_path: item.display_kind === "rename" && item.kind !== "delete" ? item.rename_with : undefined,
          status: item.display_kind === "rename" ? "R" : item.kind === "add" ? "A" : item.kind === "delete" ? "D" : "M",
          additions: item.additions, deletions: item.deletions, binary: item.binary, truncated: item.truncated, reason: item.reason, review: item,
        }));
        return { owner: sessionId, view, groups: { session: { revision: result.revision, rows } } };
      }
      // Each group keeps its own revision: an action is checked against the
      // exact scope it changes.
      const [staged, unstaged] = await Promise.all(gitGroups.map((group) => api.changes(sessionId, group)));
      if (current) workingRef.current?.([...new Map([...staged.changes, ...unstaged.changes].map((row) => [row.path, row])).values()]);
      return { owner: sessionId, view, git: staged.git !== false && unstaged.git !== false, groups: {
        staged: { revision: staged.revision, rows: staged.changes },
        unstaged: { revision: unstaged.revision, rows: unstaged.changes },
      } };
    };
    void read().then((result) => {
      if (!current) return;
      setListing(result); countRef.current(new Set(Object.values(result.groups).flatMap((group) => group?.rows.map((row) => row.path) ?? [])).size); setLoading(false);
      setExpanded((open) => {
        const kept = open.filter((selection) => result.groups[selection.group]?.rows.some((row) => row.path === selection.path));
        return kept.length === open.length ? open : kept;
      });
    }).catch((reason) => {
      if (!current) return;
      setError(message(reason)); setLoading(false); setListing(null); countRef.current(0);
    });
    return () => { current = false; };
  }, [sessionId, view, refresh]);

  // Fetch each open file's diff once per listing refresh; an old diff stays on screen while it reloads.
  const expandedKey = expanded.map(keyOf).join("\u0001");
  useEffect(() => {
    const owner = generation.current;
    for (const selection of expanded) {
      const key = keyOf(selection);
      const entry = patches[key];
      if (entry && entry.refresh === refresh) continue;
      setPatches((current) => ({ ...current, [key]: { refresh, loading: true, value: current[key]?.value } }));
      const request = selection.group === "session" ? api.reviewPatch(sessionId, selection.path) : api.patch(sessionId, selection.path, selection.group);
      void request.then((value) => {
        if (generation.current === owner) setPatches((current) => current[key]?.refresh === refresh ? { ...current, [key]: { refresh, loading: false, value } } : current);
      }).catch((reason) => {
        if (generation.current === owner) setPatches((current) => current[key]?.refresh === refresh ? { ...current, [key]: { refresh, loading: false, error: message(reason) } } : current);
      });
    }
    // `patches` is read, not watched: a fetch result must not start another fetch.
  }, [expandedKey, sessionId, refresh]);

  const isOpen = (selection: Selection) => expanded.some((item) => item.group === selection.group && item.path === selection.path);
  const toggleOpen = (selection: Selection) => {
    setExpanded((open) => isOpen(selection) ? open.filter((item) => !(item.group === selection.group && item.path === selection.path)) : [...open, selection]);
    if (feedback && feedback.group === selection.group && feedback.path === selection.path) { setFeedback(null); setFeedbackText(""); }
  };

  const handledFocus = useRef(0);
  useEffect(() => {
    if (!focus || handledFocus.current === focus.request || !listing || listing.owner !== sessionId || listing.view !== "git") return;
    const group = gitGroups.find((item) => listing.groups[item]?.rows.some((row) => row.path === focus.path));
    if (!group) return;
    handledFocus.current = focus.request;
    const selection = { group, path: focus.path };
    setExpanded((open) => open.some((item) => item.group === group && item.path === focus.path) ? open : [...open, selection]);
    setFocusTarget(keyOf(selection));
  }, [focus, listing, sessionId]);
  // Bring a file opened from elsewhere into view once its row is rendered.
  const [focusTarget, setFocusTarget] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!focusTarget) return;
    const row = [...(panelRef.current?.querySelectorAll<HTMLElement>("[data-change-key]") ?? [])].find((item) => item.dataset.changeKey === focusTarget);
    if (row) { row.scrollIntoView?.({ block: "start" }); setFocusTarget(null); }
  });
  // File heads stick under the panel header, whatever height it wraps to.
  const topRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const top = topRef.current, panel = panelRef.current;
    if (!top || !panel) return;
    const apply = () => panel.style.setProperty("--changes-top", `${top.offsetHeight}px`);
    apply();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(apply);
    observer.observe(top);
    return () => observer.disconnect();
  }, []);

  const owned = listing?.owner === sessionId && listing.view === view ? listing : null;
  const groupsShown: Group[] = view === "session" ? ["session"] : gitGroups;
  const rowsOf = (group: Group) => owned?.groups[group]?.rows ?? [];
  const revisionOf = (group: Group) => owned?.groups[group]?.revision;
  const allRows = groupsShown.flatMap(rowsOf);
  const fileCount = new Set(allRows.map((row) => row.path)).size;
  const patchOf = (selection: Selection) => patches[keyOf(selection)];
  const staleOf = (selection: Selection) => {
    const value = patchOf(selection)?.value;
    return !!value?.revision && !!revisionOf(selection.group) && value.revision !== revisionOf(selection.group);
  };
  const runtimeBusy = sessionBusy;
  const blocked = (group: Group) => actionPending || runtimeBusy || loading || !!error || !revisionOf(group);
  const stats = useMemo(() => sumStats(allRows), [allRows]);
  const canUndo = (row: Row) => row.review?.can_revert === true;
  const canStage = (row: Row) => row.capabilities?.stage ?? true;
  const canUnstage = (row: Row) => row.capabilities?.unstage ?? true;
  const canDiscard = (row: Row) => row.capabilities?.discard ?? true;
  const allowed = (action: ChangeAction | "undo") => action === "undo" ? canUndo : action === "stage" ? canStage : action === "unstage" ? canUnstage : canDiscard;
  const pathsFor = (group: Group, targets: Row[]) => [...new Set(targets.flatMap((row) => group === "session" && row.review?.rename_with ? [row.path, row.review.rename_with] : [row.path]))];
  const targetsFor = (group: Group, action: ChangeAction | "undo") => rowsOf(group).filter(allowed(action));
  const bulkBlocked = (group: Group, action: ChangeAction) => rowsOf(group).some((row) => !allowed(action)(row));
  const unavailableUndo = rowsOf("session").length - targetsFor("session", "undo").length;
  const partial = (row: Row) => rowsOf("staged").some((other) => other.path === row.path) && rowsOf("unstaged").some((other) => other.path === row.path);
  const rowState = (row: Row) => row.review
    ? tr(`web.changes.state.${row.review.committed ? "committed" : row.review.state}`)
    : [row.untracked ? tr("web.changes.new_badge") : "", partial(row) ? tr("web.changes.partial_badge") : ""].filter(Boolean).join(" · ");

  const act = async (action: ChangeAction | "undo", group: Group, targets: Row[], hunk?: number) => {
    const revision = revisionOf(group);
    if (blocked(group) || (hunk !== undefined && (!targets[0] || staleOf({ group, path: targets[0].path }) || !patchOf({ group, path: targets[0].path })?.value)) || !revision) return;
    const paths = pathsFor(group, targets);
    if (action !== "commit" && !paths.length) return;
    // Taken before any confirmation: the user may switch sessions while a dialog is open.
    const owner = generation.current;
    if (action === "discard" || action === "undo") {
      const key = action === "undo" ? "web.changes.undo_confirm" : hunk !== undefined ? "web.changes.discard_hunk_confirm" : "web.changes.discard_confirm.unstaged";
      if (!await confirmDialog({ title: tr(key, { paths: paths.join(", ") }), danger: true })) return;
    }
    if (action === "commit" && (!commitMessage.trim() || !await confirmDialog({ title: tr("web.changes.commit_confirm", { message: commitMessage.trim() }) }))) return;
    if (generation.current !== owner) return;
    setActionPending(true); setNote("");
    try {
      const result = action === "undo"
        ? await api.reviewAction(sessionId, "revert", paths, revision)
        : await api.changeAction(sessionId, { action, scope: group as GitChangeScope, paths, expected_revision: revision, hunk, message: action === "commit" ? commitMessage.trim() : undefined, confirm: action === "discard" || action === "commit" });
      if (generation.current !== owner) return;
      if (!result.applied) {
        setNote(result.error || result.results?.filter((item) => item.result !== "applied").map((item) => `${item.path}: ${item.state ?? ""} ${item.reason ?? item.result ?? ""}`.trim()).join("; ") || tr("web.changes.action_failed"));
      } else {
        setNote(tr(action === "commit" ? "web.changes.commit_done" : "web.changes.action_done"));
        if (action === "commit") setCommitMessage("");
      }
      setRefresh((value) => value + 1);
    } catch (reason) {
      if (generation.current === owner) { setNote(message(reason)); setRefresh((value) => value + 1); }
    } finally { if (generation.current === owner) setActionPending(false); }
  };

  const hunkActionsFor = (group: Group, row: Row, patch: ChangePatch | undefined, stale: boolean) => {
    const supported = group !== "session" && !row.old_path && !!patch && !patch.binary && !patch.truncated && row.capabilities?.hunk === true;
    if (!supported) return undefined;
    return (hunk: number) => <>
      <button type="button" className="text-button" disabled={blocked(group) || stale} onClick={() => void act(group === "staged" ? "unstage" : "stage", group, [row], hunk)}>{tr(group === "staged" ? "web.changes.unstage_hunk" : "web.changes.stage_hunk")}</button>
      {group === "unstaged" && <button type="button" className="text-button changes-danger" disabled={blocked(group) || stale} onClick={() => void act("discard", group, [row], hunk)}>{tr("web.changes.discard_hunk")}</button>}
    </>;
  };

  const groupLabel = (group: Group) => tr(`web.changes.scope.${group}`);
  const addFeedback = () => {
    if (!feedback || !feedbackText.trim() || !onFeedback) return;
    const line = feedback.line;
    const lineNumber = line?.side === "old" ? line.oldLine : line?.newLine;
    const location = `${feedback.path}${lineNumber != null ? `:${lineNumber} (${tr(`web.changes.${line?.side}`)})` : ""}`;
    const context = line ? `\n> ${line.content.replace(/\n/g, "\n> ")}\n` : "\n";
    onFeedback(`${tr("web.changes.feedback_context", { scope: groupLabel(feedback.group) })}\n${location}${context}\n${feedbackText.trim()}`);
    setFeedback(null); setFeedbackText(""); setNote(tr("web.changes.feedback_added"));
  };
  const feedbackForm = (group: Group, path: string) => feedback && feedback.group === group && feedback.path === path && <form className="changes-feedback" onSubmit={(event) => { event.preventDefault(); addFeedback(); }}>
    <strong>{tr("web.changes.feedback_title", { path: feedback.path })}</strong>
    {feedback.line && <small>{tr("web.changes.feedback_line", { side: tr(`web.changes.${feedback.line.side}`), line: (feedback.line.side === "old" ? feedback.line.oldLine : feedback.line.newLine) ?? "" })}</small>}
    {feedback.line && <pre>{feedback.line.content}</pre>}
    <textarea autoFocus aria-label={tr("web.changes.feedback")} placeholder={tr("web.changes.feedback_placeholder")} value={feedbackText} onChange={(event) => setFeedbackText(event.target.value)} rows={3} />
    <div className="changes-actions"><button type="submit" className="button" disabled={!feedbackText.trim()}>{tr("web.changes.add_feedback")}</button><button type="button" className="text-button" onClick={() => setFeedback(null)}>{tr("web.changes.feedback_cancel")}</button></div>
  </form>;

  const diffBody = (group: Group, row: Row) => {
    const selection = { group, path: row.path };
    const entry = patchOf(selection);
    const patch = entry?.value;
    const stale = staleOf(selection);
    return <>
      {row.reason && <p className="hint">{row.reason}</p>}
      {entry?.error ? <p className="form-error" role="alert">{entry.error}</p>
        : !patch ? <p className="hint" role="status">{tr("web.inspector_loading")}</p>
        : patch.binary ? <p className="hint">{tr("web.changes.binary")}</p>
        : patch.truncated ? <p className="hint">{tr("web.changes.large")}</p>
        : patch.unavailable ? <p className="hint">{tr("web.changes.unavailable")}</p>
        : <DiffViewer embedded patch={patch.patch} filename={row.path} renderHunkActions={hunkActionsFor(group, row, patch, stale)} onLineFeedback={onFeedback ? (line) => { setFeedback({ group, path: row.path, line }); setFeedbackText(""); } : undefined} />}
      {patch?.reason && <p className="hint">{patch.reason}</p>}
      {stale && <p className="form-error" role="alert">{tr("web.changes.diff_changed")}</p>}
      {feedbackForm(group, row.path)}
    </>;
  };

  const bulkButton = (group: Group, action: ChangeAction, label: string, danger = false) => <button type="button" className={`btn btn-sm${danger ? " btn-ghost changes-danger changes-bulk-danger" : " btn-outline"}`} disabled={blocked(group) || bulkBlocked(group, action) || !targetsFor(group, action).length} title={bulkBlocked(group, action) ? tr("web.changes.bulk_unavailable") : undefined} onClick={() => void act(action, group, targetsFor(group, action))}>{label}</button>;
  const groupActions = (group: Group) => group === "session"
    ? <button type="button" className="btn btn-sm btn-ghost" disabled={blocked(group) || !targetsFor(group, "undo").length} onClick={() => void act("undo", group, targetsFor(group, "undo"))}>{tr("web.changes.undo_count", { count: targetsFor(group, "undo").length })}</button>
    : group === "staged" ? bulkButton(group, "unstage", tr("web.changes.unstage_all"))
    : <>{bulkButton(group, "discard", tr("web.changes.discard_all"), true)}{bulkButton(group, "stage", tr("web.changes.stage_all"))}</>;
  const groupBlockedHint = (group: Group) => group === "staged" ? bulkBlocked(group, "unstage") : group === "unstaged" && (bulkBlocked(group, "stage") || bulkBlocked(group, "discard"));

  // Under a directory header the row shows the file name only.
  const fileRow = (group: Group, row: Row, showDir = true) => {
    const selection = { group, path: row.path };
    const badge = rowState(row);
    const [fullDir, name] = splitPath(row.path);
    const dir = showDir ? fullDir : "";
    const letter = kindLetter(row.status);
    const active = isOpen(selection);
    const rowBusy = blocked(group);
    const iconAction = (label: string, enabled: boolean, run: () => void, glyph: ReactNode) =>
      <button type="button" className="icon-btn sm" aria-label={`${label} ${row.path}`} title={row.reason || label} disabled={rowBusy || !enabled} onClick={() => void run()}>{glyph}</button>;
    const rowActions = <>
      {onOpenFile && <button type="button" className="icon-btn sm" aria-label={tr("web.changes.view_file")} title={tr("web.changes.view_file")} onClick={() => onOpenFile(row.path)}><Eye size={14} /></button>}
      {group === "session"
        ? iconAction(tr("web.changes.undo"), canUndo(row), () => act("undo", group, [row]), <ArrowCounterClockwise size={14} />)
        : group === "staged"
          ? iconAction(tr("web.changes.unstage"), canUnstage(row), () => act("unstage", group, [row]), <Minus size={14} />)
          : <>
            {iconAction(tr("web.changes.discard"), canDiscard(row), () => act("discard", group, [row]), <ArrowCounterClockwise size={14} />)}
            {iconAction(tr("web.changes.stage"), canStage(row), () => act("stage", group, [row]), <Check size={14} />)}
          </>}
    </>;
    const fileLabel = [`${letter} ${row.old_path ? `${row.old_path} → ` : ""}${row.path}`, badge, row.binary ? "BIN" : `+${row.additions ?? 0} −${row.deletions ?? 0}`].filter(Boolean).join(", ");
    const toggle = () => toggleOpen(selection);
    return <div className={`diff-file changes-file-item${active ? "" : " is-collapsed"}`} key={`${group}:${row.path}`} data-change-key={keyOf(selection)}>
      <div className={`diff-file-head changes-file-head changes-file${active ? " selected" : ""}`} role="button" tabIndex={0} aria-pressed={active} aria-label={fileLabel}
        onClick={(event) => { if ((event.target as HTMLElement).closest(".diff-actions")) return; toggle(); }}
        onKeyDown={(event) => { if (event.target !== event.currentTarget) return; if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } }}>
        <CaretRight className="chev" size={14} aria-hidden="true" />
        <span className={`badge ${letter} changes-kind kind-${letter}`} title={row.status}>{letter}</span>
        <span className="path changes-file-path" title={row.old_path ? `${row.old_path} → ${row.path}` : row.path}>
          <bdi><span className="changes-file-dir">{dir}</span><b>{name}</b></bdi>
        </span>
        {(badge || row.old_path) && <span className="changes-file-meta">{row.old_path && <small>{row.old_path} →</small>}{badge && <small className="changes-file-state">{badge}</small>}</span>}
        <span className="diff-stat changes-file-stats">{row.binary ? "BIN" : row.truncated ? "…" : <><span className="add stat-add">+{row.additions ?? 0}</span><span className="del stat-del">−{row.deletions ?? 0}</span></>}</span>
        <span className="diff-actions">{rowActions}</span>
      </div>
      {/* The diff opens under its row, so a long list never pushes it out of view. */}
      {active && <section className="changes-detail" aria-label={tr("web.changes.selected_file")}>{diffBody(group, row)}</section>}
    </div>;
  };

  // With a single non-empty group its bulk actions sit in the panel header, as one review list.
  const filledGroups = groupsShown.filter((group) => rowsOf(group).length > 0);
  const soloGroup = filledGroups.length === 1 ? filledGroups[0] : null;
  const groupSection = (group: Group) => {
    const rows = rowsOf(group);
    if (!rows.length) return null;
    return <section className="changes-group" key={group} aria-label={groupLabel(group)}>
      {group !== soloGroup && <div className="changes-group-head">
        <span className="changes-group-title">{groupLabel(group)}<small>{rows.length}</small></span>
        <span className="grow" />
        <div className="changes-actions">{groupActions(group)}</div>
      </div>}
      {group !== soloGroup && group === "session" && unavailableUndo > 0 && <p className="hint">{tr("web.changes.undo_unavailable", { count: unavailableUndo })}</p>}
      {group !== soloGroup && groupBlockedHint(group) && <p className="hint">{tr("web.changes.bulk_unavailable")}</p>}
      {groupRows(group, rows)}
    </section>;
  };
  // A fold opens by itself when it holds a file opened from elsewhere.
  const holdsOpen = (group: Group, rows: Row[]) => rows.some((row) => isOpen({ group, path: row.path })) || undefined;
  const generatedFold = (group: Group, rows: Row[]) => {
    const total = sumStats(rows);
    const slots = (onOpenFile ? 1 : 0) + (group === "unstaged" ? 2 : 1);
    const aside = <><span className="changes-file-stats"><span className="stat-add">+{total.add}</span>
      <span className="stat-del">−{total.del}</span></span><ActionSlots count={slots} /></>;
    return <ChangeFold className="changes-generated" title={tr("web.changes.generated", { count: rows.length })}
      aside={aside} open={holdsOpen(group, rows)}>{rows.map((row) => fileRow(group, row))}</ChangeFold>;
  };
  // Build output first, so it is seen without scrolling past every reviewed file.
  const groupRows = (group: Group, rows: Row[]) => {
    const { folded, visible, entries } = layoutRows(rows);
    const generated = folded.length > 0 && generatedFold(group, folded);
    if (!entries.length) {
      return <>{generated}
        {visible.length > 0 && <div className="changes-file-list">{visible.map((row) => fileRow(group, row))}</div>}</>;
    }
    return <>{generated}<div className="changes-file-list">{entries.map((entry) => entry.kind === "file"
      ? fileRow(group, entry.row)
      : <ChangeFold key={entry.dir} className="changes-dir" open
        title={<><bdi>{entry.dir}</bdi><small>{entry.rows.length}</small></>}>
        {entry.rows.map((row) => fileRow(group, row, false))}</ChangeFold>)}</div></>;
  };

  const expandable = allRows.filter((row) => !isGenerated(row.path));
  const allOpen = expandable.length > 0 && expandable.every((row) => groupsShown.some((group) => isOpen({ group, path: row.path })));
  const toggleAll = () => {
    if (allOpen) { setExpanded([]); return; }
    setExpanded(groupsShown.flatMap((group) => rowsOf(group).filter((row) => !isGenerated(row.path)).map((row) => ({ group, path: row.path }))));
  };
  const showSummary = !!owned && !error && owned.git !== false && allRows.length > 0;
  const generatedCount = new Set(groupsShown.flatMap((group) => layoutRows(rowsOf(group)).folded)
    .map((row) => row.path)).size;

  return <div className="changes-panel" ref={panelRef}>
    <div className="changes-top" ref={topRef}>
      <div className="changes-toolbar">
        <SegmentedControl className="changes-view-switch" value={view} ariaLabel={tr("web.changes.scope")}
          describedBy={`changes-scope-help-${sessionId}`} disabled={actionPending}
          options={views.map((value) => ({ value, label: tr(`web.changes.view.${value}`), title: tr(`web.changes.help.${value}`) }))}
          onChange={setView} />
        <div className="changes-toolbar-actions">
          <span className="changes-scope-info" title={tr(`web.changes.help.${view}`)} aria-hidden="true"><Info size={14} /></span>
          {expandable.length > 0 && expandable.length <= EXPAND_ALL_LIMIT && <button type="button" className="icon-btn sm" onClick={toggleAll}
            aria-label={tr(allOpen ? "web.changes.collapse_all" : "web.changes.expand_all")} title={tr(allOpen ? "web.changes.collapse_all" : "web.changes.expand_all")}>
            {allOpen ? <ArrowsInLineVertical size={14} /> : <ArrowsOutLineVertical size={14} />}</button>}
          <button type="button" className="icon-btn sm" aria-label={tr("web.changes.refresh")} title={tr("web.changes.refresh")} disabled={loading || actionPending} aria-busy={loading} onClick={() => setRefresh((value) => value + 1)}><ArrowClockwise size={14} className={loading ? "spin" : undefined} /></button>
        </div>
      </div>
      {showSummary && <div className="changes-summary pane-head">
        <b>{tr("web.changes.count", { count: fileCount })}</b>
        {generatedCount > 0 && <small className="changes-summary-note">
          {tr("web.changes.generated_count", { count: generatedCount })}</small>}
        <span className="diff-stat"><span className="add stat-add">+{stats.add}</span><span className="del stat-del">−{stats.del}</span></span>
        <span className="grow" />
        {soloGroup && <div className="changes-actions">{groupActions(soloGroup)}</div>}
      </div>}
    </div>
    {/* What the scope covers is reference, not news: a tooltip for sight, a description for screen readers. */}
    <p className="sr-only" id={`changes-scope-help-${sessionId}`}>{tr(`web.changes.help.${view}`)}</p>
    {runtimeBusy && <p className="hint">{tr("web.changes.busy")}</p>}
    {loading && !owned && <p className="hint" role="status">{tr("web.inspector_loading")}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {note && <p className="changes-note" role="status">{note}</p>}
    {owned?.git === false && <p className="hint">{tr("web.changes.no_git")}</p>}
    {!loading && owned && !error && owned.git !== false && !allRows.length && <div className="pane-empty"><Check size={26} aria-hidden="true" /><p className="empty-small">{tr(`web.changes.empty.${view === "git" ? "working" : "session"}`)}</p></div>}
    {soloGroup === "session" && unavailableUndo > 0 && <p className="hint">{tr("web.changes.undo_unavailable", { count: unavailableUndo })}</p>}
    {soloGroup && groupBlockedHint(soloGroup) && <p className="hint">{tr("web.changes.bulk_unavailable")}</p>}
    {owned?.git !== false && groupsShown.map(groupSection)}
    {view === "git" && rowsOf("staged").length > 0 && <CommitBar id={`changes-commit-${sessionId}`}
      count={rowsOf("staged").length} message={commitMessage} onMessage={setCommitMessage}
      pending={actionPending} blocked={blocked("staged")}
      onCommit={() => void act("commit", "staged", rowsOf("staged"))} />}
  </div>;
}
