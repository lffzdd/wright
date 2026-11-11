import { ArrowsOutSimple, ArrowClockwise, ChatCircle, Info, X } from "@phosphor-icons/react";
import { confirmDialog } from "../ui/dialogs";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api";
import { DiffViewer, type DiffFeedbackLine } from "../../DiffViewer";
import { useT } from "../../i18n";
import { SegmentedControl } from "../ui/SegmentedControl";
import type { ChangeAction, ChangePatch, ChangesSummary, GitChange, GitChangeScope, ReviewChange, ViewState } from "../../types";

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
const same = (a: Selection | null, b: Selection | null) => !!a && !!b && a.group === b.group && a.path === b.path;
const splitPath = (path: string) => { const index = path.lastIndexOf("/"); return index < 0 ? ["", path] : [path.slice(0, index + 1), path.slice(index + 1)]; };
// Build output that is regenerated rather than reviewed line by line.
const generatedPatterns = [/(^|\/)static\/assets\//, /(^|\/)(dist|build)\//, /\.min\.(js|css)$/, /\.map$/];
export const isGenerated = (path: string) => generatedPatterns.some((pattern) => pattern.test(path));
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

export function ChangesPanel({ sessionId, state, onCount, onWorkingChanges, onFeedback, focus }: {
  sessionId: string;
  /** A file to open once the listing has it; a new request reopens it. */
  focus?: { path: string; request: number } | null;
  state: ViewState;
  onCount: (count: number) => void;
  onWorkingChanges?: (changes: ChangesSummary["changes"]) => void;
  onFeedback?: (feedback: string) => void;
}) {
  const tr = useT();
  const [view, setView] = useState<ChangeView>("git");
  const [listing, setListing] = useState<Listing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [detail, setDetail] = useState<{ owner: string; selection: Selection; value: ChangePatch } | null>(null);
  const [patchLoading, setPatchLoading] = useState(false);
  const [patchError, setPatchError] = useState("");
  const [actionPending, setActionPending] = useState(false);
  const [note, setNote] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [wide, setWide] = useState(false);
  const [feedback, setFeedback] = useState<{ path: string; line?: DiffFeedbackLine } | null>(null);
  const [feedbackText, setFeedbackText] = useState("");
  const generation = useRef(0);
  const wideRef = useRef<HTMLDivElement>(null);
  const expandRef = useRef<HTMLButtonElement>(null);
  const countRef = useRef(onCount);
  const workingRef = useRef(onWorkingChanges);
  countRef.current = onCount;
  workingRef.current = onWorkingChanges;

  useEffect(() => { setCommitMessage(""); }, [sessionId]);

  useEffect(() => {
    ++generation.current;
    setListing(null); setSelected(null); setDetail(null); setPatchError(""); setNote("");
    setActionPending(false); setWide(false); setFeedback(null); setFeedbackText("");
    countRef.current(0);
    return () => { ++generation.current; };
  }, [sessionId, view]);

  useEffect(() => {
    let current = true;
    setLoading(true); setError("");
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
      setSelected((selection) => selection && result.groups[selection.group]?.rows.some((row) => row.path === selection.path) ? selection : null);
    }).catch((reason) => {
      if (!current) return;
      setError(message(reason)); setLoading(false); setListing(null); countRef.current(0);
    });
    return () => { current = false; };
  }, [sessionId, view, refresh, state.session.status, state.session.execution, state.connection]);

  useEffect(() => {
    let current = true;
    setDetail(null); setPatchError(""); setPatchLoading(!!selected);
    setFeedback(null); setFeedbackText("");
    if (selected) {
      const request = selected.group === "session" ? api.reviewPatch(sessionId, selected.path) : api.patch(sessionId, selected.path, selected.group);
      void request.then((value) => {
        if (current) { setDetail({ owner: sessionId, selection: selected, value }); setPatchLoading(false); }
      }).catch((reason) => { if (current) { setPatchError(message(reason)); setPatchLoading(false); } });
    }
    return () => { current = false; };
  }, [selected?.group, selected?.path, sessionId, refresh]);

  useEffect(() => {
    if (!wide) return;
    wideRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { if (expandRef.current?.isConnected) expandRef.current.focus(); };
  }, [wide]);
  useEffect(() => { if (!selected) setWide(false); }, [selected]);

  const handledFocus = useRef(0);
  useEffect(() => {
    if (!focus || handledFocus.current === focus.request || !listing || listing.owner !== sessionId || listing.view !== "git") return;
    const group = gitGroups.find((item) => listing.groups[item]?.rows.some((row) => row.path === focus.path));
    if (!group) return;
    handledFocus.current = focus.request;
    setSelected({ group, path: focus.path });
  }, [focus, listing, sessionId]);

  const owned = listing?.owner === sessionId && listing.view === view ? listing : null;
  const groupsShown: Group[] = view === "session" ? ["session"] : gitGroups;
  const rowsOf = (group: Group) => owned?.groups[group]?.rows ?? [];
  const revisionOf = (group: Group) => owned?.groups[group]?.revision;
  const allRows = groupsShown.flatMap(rowsOf);
  const fileCount = new Set(allRows.map((row) => row.path)).size;
  const selectedRow = selected ? rowsOf(selected.group).find((row) => row.path === selected.path) : undefined;
  const patch = detail?.owner === sessionId && same(detail.selection, selected) ? detail.value : null;
  const stalePatch = !!selected && !!patch?.revision && !!revisionOf(selected.group) && patch.revision !== revisionOf(selected.group);
  const execution = state.session.execution ?? state.session.status;
  const runtimeBusy = state.connection !== "connected" || ["running", "queued", "waiting_for_input", "cancelling"].includes(execution);
  const blocked = (group: Group) => actionPending || runtimeBusy || loading || !!error || !revisionOf(group);
  const fileBlocked = (group: Group) => blocked(group) || stalePatch || patchLoading || !!patchError || !patch;
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
    if (blocked(group) || (hunk !== undefined && (stalePatch || !patch)) || !revision) return;
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

  const fileActions = (group: Group, row: Row) => group === "session"
    ? <button type="button" className="button" disabled={fileBlocked(group) || !canUndo(row)} title={row.reason || undefined} onClick={() => void act("undo", group, [row])}>{tr("web.changes.undo")}</button>
    : group === "staged"
      ? <button type="button" className="button" disabled={fileBlocked(group) || !canUnstage(row)} title={row.reason || undefined} onClick={() => void act("unstage", group, [row])}>{tr("web.changes.unstage")}</button>
      : <>
        <button type="button" className="button" disabled={fileBlocked(group) || !canStage(row)} title={row.reason || undefined} onClick={() => void act("stage", group, [row])}>{tr("web.changes.stage")}</button>
        <button type="button" className="button changes-danger" disabled={fileBlocked(group) || !canDiscard(row)} title={row.reason || undefined} onClick={() => void act("discard", group, [row])}>{tr("web.changes.discard")}</button>
      </>;

  const hunkSupported = selected && selectedRow && selected.group !== "session" && !selectedRow.old_path && !patch?.binary && !patch?.truncated && selectedRow.capabilities?.hunk === true;
  const hunkActions = hunkSupported ? (hunk: number) => <>
    <button type="button" className="text-button" disabled={blocked(selected.group) || stalePatch} onClick={() => void act(selected.group === "staged" ? "unstage" : "stage", selected.group, [selectedRow], hunk)}>{tr(selected.group === "staged" ? "web.changes.unstage_hunk" : "web.changes.stage_hunk")}</button>
    {selected.group === "unstaged" && <button type="button" className="text-button changes-danger" disabled={blocked(selected.group) || stalePatch} onClick={() => void act("discard", selected.group, [selectedRow], hunk)}>{tr("web.changes.discard_hunk")}</button>}
  </> : undefined;

  const groupLabel = (group: Group) => tr(`web.changes.scope.${group}`);
  const addFeedback = () => {
    if (!feedback || !selected || !feedbackText.trim() || !onFeedback) return;
    const line = feedback.line;
    const lineNumber = line?.side === "old" ? line.oldLine : line?.newLine;
    const location = `${feedback.path}${lineNumber != null ? `:${lineNumber} (${tr(`web.changes.${line?.side}`)})` : ""}`;
    const context = line ? `\n> ${line.content.replace(/\n/g, "\n> ")}\n` : "\n";
    setWide(false);
    onFeedback(`${tr("web.changes.feedback_context", { scope: groupLabel(selected.group) })}\n${location}${context}\n${feedbackText.trim()}`);
    setFeedback(null); setFeedbackText(""); setNote(tr("web.changes.feedback_added"));
  };
  const feedbackForm = feedback && <form className="changes-feedback" onSubmit={(event) => { event.preventDefault(); addFeedback(); }}>
    <strong>{tr("web.changes.feedback_title", { path: feedback.path })}</strong>
    {feedback.line && <small>{tr("web.changes.feedback_line", { side: tr(`web.changes.${feedback.line.side}`), line: (feedback.line.side === "old" ? feedback.line.oldLine : feedback.line.newLine) ?? "" })}</small>}
    {feedback.line && <pre>{feedback.line.content}</pre>}
    <textarea autoFocus aria-label={tr("web.changes.feedback")} placeholder={tr("web.changes.feedback_placeholder")} value={feedbackText} onChange={(event) => setFeedbackText(event.target.value)} rows={3} />
    <div className="changes-actions"><button type="submit" className="button" disabled={!feedbackText.trim()}>{tr("web.changes.add_feedback")}</button><button type="button" className="text-button" onClick={() => setFeedback(null)}>{tr("web.changes.feedback_cancel")}</button></div>
  </form>;

  const diffBody = selected && <>
    <div className="changes-detail-head"><strong title={selected.path}>{selected.path}{view === "git" && <small className="changes-detail-group"> · {groupLabel(selected.group)}</small>}</strong><div className="changes-actions">{selectedRow && fileActions(selected.group, selectedRow)}{onFeedback && <button type="button" className="button" onClick={() => { setFeedback({ path: selected.path }); setFeedbackText(""); }}><ChatCircle size={13} />{tr("web.changes.feedback")}</button>}</div></div>
    {selectedRow?.reason && <p className="hint">{selectedRow.reason}</p>}
    {patchError ? <p className="form-error" role="alert">{patchError}</p> : patchLoading ? <p className="hint" role="status">{tr("web.inspector_loading")}</p> : patch?.binary ? <p className="hint">{tr("web.changes.binary")}</p> : patch?.truncated ? <p className="hint">{tr("web.changes.large")}</p> : patch?.unavailable ? <p className="hint">{tr("web.changes.unavailable")}</p> : patch && <DiffViewer patch={patch.patch} filename={selected.path} renderHunkActions={hunkActions} onLineFeedback={onFeedback ? (line) => { setFeedback({ path: selected.path, line }); setFeedbackText(""); } : undefined} />}
    {patch?.reason && <p className="hint">{patch.reason}</p>}
    {stalePatch && <p className="form-error" role="alert">{tr("web.changes.diff_changed")}</p>}
    {feedbackForm}
  </>;

  const bulkButton = (group: Group, action: ChangeAction, label: string, danger = false) => <button type="button" className={`button${danger ? " changes-danger changes-bulk-danger" : ""}`} disabled={blocked(group) || bulkBlocked(group, action) || !targetsFor(group, action).length} title={bulkBlocked(group, action) ? tr("web.changes.bulk_unavailable") : undefined} onClick={() => void act(action, group, targetsFor(group, action))}>{label}</button>;
  const groupActions = (group: Group) => group === "session"
    ? <button type="button" className="button" disabled={blocked(group) || !targetsFor(group, "undo").length} onClick={() => void act("undo", group, targetsFor(group, "undo"))}>{tr("web.changes.undo_count", { count: targetsFor(group, "undo").length })}</button>
    : group === "staged" ? bulkButton(group, "unstage", tr("web.changes.unstage_all"))
    : <>{bulkButton(group, "stage", tr("web.changes.stage_all"))}{bulkButton(group, "discard", tr("web.changes.discard_all"), true)}</>;
  const groupBlockedHint = (group: Group) => group === "staged" ? bulkBlocked(group, "unstage") : group === "unstaged" && (bulkBlocked(group, "stage") || bulkBlocked(group, "discard"));

  // The diff opens under its row, so a long list never pushes it out of view.
  const detailSection = selected && <section className="changes-detail" hidden={wide} aria-label={tr("web.changes.selected_file")}>
    <div className="changes-expand"><button type="button" className="text-button" ref={expandRef} disabled={patchLoading || !!patchError || !patch} onClick={() => setWide(true)}><ArrowsOutSimple size={13} />{tr("web.expand_diff")}</button></div>{!wide && diffBody}
  </section>;

  const fileRow = (group: Group, row: Row) => {
    const selection = { group, path: row.path };
    const badge = rowState(row);
    const [dir, name] = splitPath(row.path);
    const letter = kindLetter(row.status);
    const active = same(selected, selection);
    return <div className="changes-file-item" key={`${group}:${row.path}`}>
      <button type="button" className={`changes-file${active ? " selected" : ""}`} aria-pressed={active} aria-label={[`${letter} ${row.old_path ? `${row.old_path} → ` : ""}${row.path}`, badge, row.binary ? "BIN" : `+${row.additions ?? 0} −${row.deletions ?? 0}`].filter(Boolean).join(", ")} onClick={() => setSelected(active ? null : selection)}>
        <span className={`changes-kind kind-${letter}`} title={row.status}>{letter}</span>
        <span className="changes-file-name" title={row.old_path ? `${row.old_path} → ${row.path}` : row.path}>
          <span className="changes-file-path"><strong>{name}</strong><span className="changes-file-dir">{dir}</span></span>
          {(badge || row.old_path) && <span className="changes-file-meta">{row.old_path && <small>{row.old_path} →</small>}{badge && <small className="changes-file-state">{badge}</small>}</span>}
        </span>
        <span className="changes-file-stats">{row.binary ? "BIN" : row.truncated ? "…" : <><span className="stat-add">+{row.additions ?? 0}</span><span className="stat-del">−{row.deletions ?? 0}</span></>}</span>
      </button>
      {active && detailSection}
    </div>;
  };

  const groupSection = (group: Group) => {
    const rows = rowsOf(group);
    if (!rows.length) return null;
    return <section className="changes-group" key={group} aria-label={groupLabel(group)}>
      <div className="changes-group-head">
        {view === "git" && <span className="changes-group-title">{groupLabel(group)}<small>{rows.length}</small></span>}
        <div className="changes-actions">{groupActions(group)}</div>
      </div>
      {group === "session" && unavailableUndo > 0 && <p className="hint">{tr("web.changes.undo_unavailable", { count: unavailableUndo })}</p>}
      {groupBlockedHint(group) && <p className="hint">{tr("web.changes.bulk_unavailable")}</p>}
      {(() => {
        const generated = rows.filter((row) => isGenerated(row.path));
        // A lone generated file is not worth a fold.
        const folded = generated.length >= 2 ? generated : [];
        const visible = rows.filter((row) => !folded.includes(row));
        const generatedStats = sumStats(folded);
        return <>
          {visible.length > 0 && <div className="changes-file-list">{visible.map((row) => fileRow(group, row))}</div>}
          {folded.length > 0 && <details className="changes-generated" open={folded.some((row) => same(selected, { group, path: row.path })) || undefined}>
            <summary><span>{tr("web.changes.generated", { count: folded.length })}</span><span className="changes-file-stats"><span className="stat-add">+{generatedStats.add}</span><span className="stat-del">−{generatedStats.del}</span></span></summary>
            <div className="changes-file-list">{folded.map((row) => fileRow(group, row))}</div>
          </details>}
        </>;
      })()}
      {group === "staged" && <form className="changes-commit" onSubmit={(event) => { event.preventDefault(); void act("commit", "staged", rows); }}>
        <label htmlFor={`changes-commit-${sessionId}`}>{tr("web.changes.commit_message")}</label><textarea id={`changes-commit-${sessionId}`} value={commitMessage} onChange={(event) => setCommitMessage(event.target.value)} placeholder={tr("web.changes.commit_placeholder")} rows={3} disabled={actionPending} /><p className="hint">{tr("web.changes.commit_help")}</p><button type="submit" className="button" disabled={blocked("staged") || !commitMessage.trim()}>{tr("web.changes.commit")}</button>
      </form>}
    </section>;
  };

  return <div className="changes-panel">
    <div className="changes-toolbar">
      <SegmentedControl className="changes-view-switch" value={view} ariaLabel={tr("web.changes.scope")}
        describedBy={`changes-scope-help-${sessionId}`} disabled={actionPending}
        options={views.map((value) => ({ value, label: tr(`web.changes.view.${value}`), title: tr(`web.changes.help.${value}`) }))}
        onChange={setView} />
      <div className="changes-toolbar-actions">
        <span className="changes-scope-info" title={tr(`web.changes.help.${view}`)} aria-hidden="true"><Info size={15} /></span>
        <button type="button" className="icon-button" aria-label={tr("web.changes.refresh")} title={tr("web.changes.refresh")} disabled={loading || actionPending} onClick={() => setRefresh((value) => value + 1)}><ArrowClockwise size={15} /></button>
      </div>
    </div>
    {/* What the scope covers is reference, not news: a tooltip for sight, a description for screen readers. */}
    <p className="sr-only" id={`changes-scope-help-${sessionId}`}>{tr(`web.changes.help.${view}`)}</p>
    {owned && !error && owned.git !== false && <div className="changes-summary"><span>{tr("web.changes.count", { count: fileCount })}</span><span className="diff-stats"><span className="stat-add">+{stats.add}</span><span className="stat-del">−{stats.del}</span><DiffBar add={stats.add} del={stats.del} /></span></div>}
    {runtimeBusy && <p className="hint">{tr("web.changes.busy")}</p>}
    {loading && <p className="hint" role="status">{tr("web.inspector_loading")}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {note && !wide && <p className="changes-note" role="status">{note}</p>}
    {owned?.git === false && <p className="hint">{tr("web.changes.no_git")}</p>}
    {!loading && owned && !error && owned.git !== false && !allRows.length && <p className="empty-small">{tr(`web.changes.empty.${view === "git" ? "working" : "session"}`)}</p>}
    {owned?.git !== false && groupsShown.map(groupSection)}
    {!selected && allRows.length > 0 && <p className="hint">{tr("web.changes.select")}</p>}
    {wide && selected && <div className="wide-diff changes-wide" ref={wideRef} role="dialog" aria-modal="true" aria-label={selected.path} onKeyDown={(event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setWide(false); }
      if (event.key !== "Tab") return;
      event.stopPropagation();
      const controls = Array.from(wideRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), textarea:not([disabled]), [tabindex="0"]') ?? []);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}><div className="changes-wide-head"><span>{groupLabel(selected.group)}</span><div className="changes-actions"><button type="button" className="icon-button" aria-label={tr("web.close")} onClick={() => setWide(false)}><X size={16} /></button><button type="button" className="icon-button" aria-label={tr("web.changes.refresh")} disabled={loading || actionPending} onClick={() => setRefresh((value) => value + 1)}><ArrowClockwise size={15} /></button></div></div>{note && <p className="changes-note" role="status">{note}</p>}{error && <p className="form-error" role="alert">{error}</p>}{diffBody}</div>}
  </div>;
}
