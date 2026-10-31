import { ArrowsOutSimple, ArrowClockwise, ChatCircle, X } from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api";
import { DiffViewer, type DiffFeedbackLine } from "../../DiffViewer";
import { useT } from "../../i18n";
import type { ChangeAction, ChangePatch, ChangeScope, ChangesSummary, GitChange, GitChangeScope, ReviewChange, ViewState } from "../../types";

type Row = GitChange & { review?: ReviewChange };
type Listing = { owner: string; scope: ChangeScope; revision?: string; baseline?: string; git?: boolean; rows: Row[] };

const scopes: ChangeScope[] = ["working", "unstaged", "staged", "session"];
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

export function ChangesPanel({ sessionId, state, onCount, onWorkingChanges, onFeedback }: {
  sessionId: string;
  state: ViewState;
  onCount: (count: number) => void;
  onWorkingChanges?: (changes: ChangesSummary["changes"]) => void;
  onFeedback?: (feedback: string) => void;
}) {
  const tr = useT();
  const [scope, setScope] = useState<ChangeScope>("working");
  const [listing, setListing] = useState<Listing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<{ owner: string; scope: ChangeScope; value: ChangePatch } | null>(null);
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
  }, [sessionId, scope]);

  useEffect(() => {
    let current = true;
    setLoading(true); setError("");
    const read = async (): Promise<Listing> => {
      if (scope === "session") {
        const result = await api.review(sessionId);
        // A session diff is about recorded Agent writes, not every old dirty
        // file saved in the journal baseline.
        const rows = result.changes.filter((item) => item.origin === "task" || ["task", "accepted", "conflict", "unsafe"].includes(item.state)).filter((item) => !(item.display_kind === "rename" && item.kind === "delete" && result.changes.some((other) => other.path === item.rename_with && other.rename_with === item.path))).map((item) => ({
          path: item.path,
          old_path: item.display_kind === "rename" && item.kind !== "delete" ? item.rename_with : undefined,
          status: item.display_kind === "rename" ? "R" : item.kind === "add" ? "A" : item.kind === "delete" ? "D" : "M",
          additions: item.additions, deletions: item.deletions, binary: item.binary, truncated: item.truncated, reason: item.reason, review: item,
        }));
        return { owner: sessionId, scope, revision: result.revision, baseline: "session", rows };
      }
      const result = await api.changes(sessionId, scope);
      if (current && scope === "working") workingRef.current?.(result.changes);
      return { owner: sessionId, scope, revision: result.revision, baseline: result.baseline, git: result.git, rows: result.changes };
    };
    void read().then((result) => {
      if (!current) return;
      setListing(result); countRef.current(result.rows.length); setLoading(false);
      setSelected((path) => path && result.rows.some((row) => row.path === path) ? path : null);
    }).catch((reason) => {
      if (!current) return;
      setError(message(reason)); setLoading(false); setListing(null); countRef.current(0);
    });
    return () => { current = false; };
  }, [sessionId, scope, refresh, state.session.status, state.session.execution, state.connection]);

  useEffect(() => {
    let current = true;
    setDetail(null); setPatchError(""); setPatchLoading(!!selected);
    setFeedback(null); setFeedbackText("");
    if (selected) {
      const request = scope === "session" ? api.reviewPatch(sessionId, selected) : api.patch(sessionId, selected, scope);
      void request.then((value) => {
        if (current) { setDetail({ owner: sessionId, scope, value }); setPatchLoading(false); }
      }).catch((reason) => { if (current) { setPatchError(message(reason)); setPatchLoading(false); } });
    }
    return () => { current = false; };
  }, [selected, sessionId, scope, refresh]);

  useEffect(() => {
    if (!wide) return;
    wideRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { if (expandRef.current?.isConnected) expandRef.current.focus(); };
  }, [wide]);
  useEffect(() => { if (!selected) setWide(false); }, [selected]);

  const owned = listing?.owner === sessionId && listing.scope === scope ? listing : null;
  const rows = owned?.rows ?? [];
  const selectedRow = rows.find((row) => row.path === selected);
  const patch = detail?.owner === sessionId && detail.scope === scope && detail.value.path === selected ? detail.value : null;
  const revision = owned?.revision;
  const stalePatch = !!patch?.revision && !!revision && patch.revision !== revision;
  const execution = state.session.execution ?? state.session.status;
  const runtimeBusy = state.connection !== "connected" || ["running", "queued", "waiting_for_input", "cancelling"].includes(execution);
  const blocked = actionPending || runtimeBusy || loading || !!error || !revision;
  const fileBlocked = blocked || stalePatch || patchLoading || !!patchError || !patch;
  const stats = useMemo(() => rows.reduce((sum, row) => ({ add: sum.add + (row.additions ?? 0), del: sum.del + (row.deletions ?? 0) }), { add: 0, del: 0 }), [rows]);
  const canUndo = (row: Row) => row.review?.can_revert === true;
  const canStage = (row: Row) => row.capabilities?.stage ?? (scope !== "staged" && row.staged !== true || row.unstaged === true || row.untracked === true);
  const canUnstage = (row: Row) => row.capabilities?.unstage ?? (scope === "staged" || row.staged === true);
  const canDiscard = (row: Row) => row.capabilities?.discard ?? (scope !== "staged");
  const pathsFor = (targets: Row[]) => [...new Set(targets.flatMap((row) => scope === "session" && row.review?.rename_with ? [row.path, row.review.rename_with] : [row.path]))];
  const targetsFor = (action: ChangeAction | "undo") => rows.filter((row) => action === "undo" ? canUndo(row) : action === "stage" ? canStage(row) : action === "unstage" ? canUnstage(row) : canDiscard(row));
  const stageBlocked = rows.some((row) => !canStage(row) && (!row.staged || row.unstaged || row.untracked));
  const unstageBlocked = rows.some((row) => row.staged && !canUnstage(row));
  const discardBlocked = rows.some((row) => !canDiscard(row));
  const unavailableUndo = rows.length - targetsFor("undo").length;

  const act = async (action: ChangeAction | "undo", targets: Row[], hunk?: number) => {
    if (blocked || (hunk !== undefined && (stalePatch || !patch)) || !revision) return;
    const paths = pathsFor(targets);
    if (action !== "commit" && !paths.length) return;
    if (action === "discard" || action === "undo") {
      const key = action === "undo" ? "web.changes.undo_confirm" : hunk !== undefined ? "web.changes.discard_hunk_confirm" : `web.changes.discard_confirm.${scope}`;
      if (!window.confirm(tr(key, { paths: paths.join(", ") }))) return;
    }
    if (action === "commit" && (!commitMessage.trim() || !window.confirm(tr("web.changes.commit_confirm", { message: commitMessage.trim() })))) return;
    const owner = generation.current;
    setActionPending(true); setNote("");
    try {
      const result = action === "undo"
        ? await api.reviewAction(sessionId, "revert", paths, revision)
        : await api.changeAction(sessionId, { action, scope: scope as GitChangeScope, paths, expected_revision: revision, hunk, message: action === "commit" ? commitMessage.trim() : undefined, confirm: action === "discard" || action === "commit" });
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

  const fileActions = (row: Row) => scope === "session"
    ? <button type="button" className="button" disabled={fileBlocked || !canUndo(row)} title={row.reason || undefined} onClick={() => void act("undo", [row])}>{tr("web.changes.undo")}</button>
    : <>
      {scope !== "staged" && <button type="button" className="button" disabled={fileBlocked || !canStage(row)} onClick={() => void act("stage", [row])}>{tr("web.changes.stage")}</button>}
      {(scope === "staged" || scope === "working" && row.staged) && <button type="button" className="button" disabled={fileBlocked || !canUnstage(row)} onClick={() => void act("unstage", [row])}>{tr("web.changes.unstage")}</button>}
      {scope !== "staged" && <button type="button" className="button changes-danger" disabled={fileBlocked || !canDiscard(row)} title={row.reason || undefined} onClick={() => void act("discard", [row])}>{tr("web.changes.discard")}</button>}
    </>;

  const hunkSupported = selectedRow && scope !== "working" && scope !== "session" && !selectedRow.old_path && !patch?.binary && !patch?.truncated && selectedRow.capabilities?.hunk === true;
  const hunkActions = hunkSupported ? (hunk: number) => <>
    <button type="button" className="text-button" disabled={blocked || stalePatch} onClick={() => void act(scope === "staged" ? "unstage" : "stage", [selectedRow], hunk)}>{tr(scope === "staged" ? "web.changes.unstage_hunk" : "web.changes.stage_hunk")}</button>
    {scope === "unstaged" && <button type="button" className="text-button changes-danger" disabled={blocked || stalePatch} onClick={() => void act("discard", [selectedRow], hunk)}>{tr("web.changes.discard_hunk")}</button>}
  </> : undefined;

  const addFeedback = () => {
    if (!feedback || !feedbackText.trim() || !onFeedback) return;
    const line = feedback.line;
    const lineNumber = line?.side === "old" ? line.oldLine : line?.newLine;
    const location = `${feedback.path}${lineNumber != null ? `:${lineNumber} (${tr(`web.changes.${line?.side}`)})` : ""}`;
    const context = line ? `\n> ${line.content.replace(/\n/g, "\n> ")}\n` : "\n";
    setWide(false);
    onFeedback(`${tr("web.changes.feedback_context", { scope: tr(`web.changes.scope.${scope}`) })}\n${location}${context}\n${feedbackText.trim()}`);
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
    <div className="changes-detail-head"><strong title={selected}>{selected}</strong><div className="changes-actions">{selectedRow && fileActions(selectedRow)}{onFeedback && <button type="button" className="button" onClick={() => { setFeedback({ path: selected }); setFeedbackText(""); }}><ChatCircle size={13} />{tr("web.changes.feedback")}</button>}</div></div>
    {selectedRow?.reason && <p className="hint">{selectedRow.reason}</p>}
    {patchError ? <p className="form-error" role="alert">{patchError}</p> : patchLoading ? <p className="hint" role="status">{tr("web.inspector_loading")}</p> : patch?.binary ? <p className="hint">{tr("web.changes.binary")}</p> : patch?.truncated ? <p className="hint">{tr("web.changes.large")}</p> : patch?.unavailable ? <p className="hint">{tr("web.changes.unavailable")}</p> : patch && <DiffViewer patch={patch.patch} filename={selected} renderHunkActions={hunkActions} onLineFeedback={onFeedback ? (line) => { setFeedback({ path: selected, line }); setFeedbackText(""); } : undefined} />}
    {patch?.reason && <p className="hint">{patch.reason}</p>}
    {stalePatch && <p className="form-error" role="alert">{tr("web.changes.diff_changed")}</p>}
    {feedbackForm}
  </>;

  return <div className="changes-panel">
    <div className="changes-toolbar"><label className="changes-scope"><span className="sr-only">{tr("web.changes.scope")}</span><select aria-label={tr("web.changes.scope")} value={scope} disabled={actionPending} onChange={(event) => setScope(event.target.value as ChangeScope)}>{scopes.map((value) => <option key={value} value={value}>{tr(`web.changes.scope.${value}`)}</option>)}</select></label><button type="button" className="icon-button" aria-label={tr("web.changes.refresh")} title={tr("web.changes.refresh")} disabled={loading || actionPending} onClick={() => setRefresh((value) => value + 1)}><ArrowClockwise size={15} /></button></div>
    <p className="changes-scope-help">{tr(`web.changes.help.${scope}`)}</p>
    {owned && !error && <div className="changes-summary"><span>{tr("web.changes.count", { count: rows.length })}</span><span className="diff-stats"><span className="stat-add">+{stats.add}</span><span className="stat-del">−{stats.del}</span></span></div>}
    {owned?.baseline && scope !== "session" && <small className="changes-baseline">{tr("web.changes.baseline", { baseline: owned.baseline })}</small>}
    {runtimeBusy && <p className="hint">{tr("web.changes.busy")}</p>}
    {loading && <p className="hint" role="status">{tr("web.inspector_loading")}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    {note && !wide && <p className="changes-note" role="status">{note}</p>}
    {owned?.git === false && <p className="hint">{tr("web.changes.no_git")}</p>}
    {!loading && owned && !error && owned.git !== false && !rows.length && <p className="empty-small">{tr(`web.changes.empty.${scope}`)}</p>}
    {!!rows.length && <div className="changes-actions changes-bulk">
      {scope === "session" ? <button type="button" className="button" disabled={blocked || !targetsFor("undo").length} onClick={() => void act("undo", targetsFor("undo"))}>{tr("web.changes.undo_count", { count: targetsFor("undo").length })}</button> : <>
        {scope !== "staged" && <button type="button" className="button" disabled={blocked || stageBlocked || !targetsFor("stage").length} title={stageBlocked ? tr("web.changes.bulk_unavailable") : undefined} onClick={() => void act("stage", targetsFor("stage"))}>{tr("web.changes.stage_all")}</button>}
        {(scope === "staged" || scope === "working" && rows.some((row) => row.staged)) && <button type="button" className="button" disabled={blocked || unstageBlocked || !targetsFor("unstage").length} title={unstageBlocked ? tr("web.changes.bulk_unavailable") : undefined} onClick={() => void act("unstage", targetsFor("unstage"))}>{tr("web.changes.unstage_all")}</button>}
        {scope !== "staged" && <button type="button" className="button changes-danger" disabled={blocked || discardBlocked || !targetsFor("discard").length} title={discardBlocked ? tr("web.changes.bulk_unavailable") : undefined} onClick={() => void act("discard", targetsFor("discard"))}>{tr("web.changes.discard_all")}</button>}
      </>}
    </div>}
    {scope === "session" && unavailableUndo > 0 && <p className="hint">{tr("web.changes.undo_unavailable", { count: unavailableUndo })}</p>}
    {scope !== "session" && ((scope !== "staged" && (stageBlocked || discardBlocked)) || unstageBlocked) && <p className="hint">{tr("web.changes.bulk_unavailable")}</p>}
    <div className="changes-file-list">{rows.map((row) => <button type="button" className={`changes-file${selected === row.path ? " selected" : ""}`} key={row.path} aria-pressed={selected === row.path} onClick={() => setSelected(row.path)}>
      <span className={`changes-kind kind-${row.status[0]}`} title={row.status}>{row.status}</span><span className="changes-file-name" title={row.old_path ? `${row.old_path} → ${row.path}` : row.path}>{row.old_path && <small>{row.old_path} →</small>}<span>{row.path}</span><small className="changes-file-state">{row.review ? tr(`web.changes.state.${row.review.committed ? "committed" : row.review.state}`) : [row.untracked ? tr("web.changes.new_badge") : "", row.staged ? tr("web.changes.staged_badge") : "", row.unstaged ? tr("web.changes.unstaged_badge") : ""].filter(Boolean).join(" · ")}</small></span><span className="changes-file-stats">{row.binary ? "BIN" : row.truncated ? "…" : <><span className="stat-add">+{row.additions ?? 0}</span><span className="stat-del">−{row.deletions ?? 0}</span></>}</span>
    </button>)}</div>
    {!selected && rows.length > 0 && <p className="hint">{tr("web.changes.select")}</p>}
    {selected && <section className="changes-detail" hidden={wide} aria-label={tr("web.changes.selected_file")}>
      <div className="changes-expand"><button type="button" className="text-button" ref={expandRef} disabled={patchLoading || !!patchError || !patch} onClick={() => setWide(true)}><ArrowsOutSimple size={13} />{tr("web.expand_diff")}</button></div>{!wide && diffBody}
    </section>}
    {scope === "staged" && owned?.git !== false && <form className="changes-commit" onSubmit={(event) => { event.preventDefault(); void act("commit", rows); }}>
      <label htmlFor={`changes-commit-${sessionId}`}>{tr("web.changes.commit_message")}</label><textarea id={`changes-commit-${sessionId}`} value={commitMessage} onChange={(event) => setCommitMessage(event.target.value)} placeholder={tr("web.changes.commit_placeholder")} rows={3} disabled={actionPending} /><p className="hint">{tr("web.changes.commit_help")}</p><button type="submit" className="button" disabled={blocked || !rows.length || !commitMessage.trim()}>{tr("web.changes.commit")}</button>
    </form>}
    {wide && selected && <div className="wide-diff changes-wide" ref={wideRef} role="dialog" aria-modal="true" aria-label={selected} onKeyDown={(event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setWide(false); }
      if (event.key !== "Tab") return;
      event.stopPropagation();
      const controls = Array.from(wideRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), textarea:not([disabled]), [tabindex="0"]') ?? []);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}><div className="changes-wide-head"><span>{tr(`web.changes.scope.${scope}`)}</span><div className="changes-actions"><button type="button" className="icon-button" aria-label={tr("web.close")} onClick={() => setWide(false)}><X size={16} /></button><button type="button" className="icon-button" aria-label={tr("web.changes.refresh")} disabled={loading || actionPending} onClick={() => setRefresh((value) => value + 1)}><ArrowClockwise size={15} /></button></div></div>{note && <p className="changes-note" role="status">{note}</p>}{error && <p className="form-error" role="alert">{error}</p>}{diffBody}</div>}
  </div>;
}
