import {
  Archive, Check, FileCode, Paperclip, PaperPlaneRight, Plus, Square, Warning, X,
} from "@phosphor-icons/react";
import { FormEvent, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { DiffViewer } from "../DiffViewer";
import { MarkdownContent } from "../Markdown";
import { api } from "../api";
import { present, useT } from "../i18n";
import { commandAfter } from "../protocol";
import type { Attachment, FileReference, ReviewChange, SessionSummary, ViewState } from "../types";
import { InteractionCard, SummaryRow, UserMessage, toolCardFromState, toolCardFromTimeline } from "./cards";

export type Draft = {
  prompt: string;
  attachments: Attachment[];
  documents?: Array<{ id: string; filename: string }>;
  references?: FileReference[];
  mentions?: string[];
  commandId: string | null;
  clientRequestId?: string | null;
  boundSessionId?: string | null;
  environment?: "local" | "worktree";
  localFiles?: Array<{ name: string; file: File; image: boolean }>;
  phase: "idle" | "awaiting" | "accepted" | "rejected" | "unknown";
  reason: string;
};

export const emptyDraft = (): Draft => ({
  prompt: "", attachments: [], documents: [], references: [], mentions: [], commandId: null,
  clientRequestId: null, boundSessionId: null, environment: "local", localFiles: [], phase: "idle", reason: "",
});

export function visibleModels(models: string[], currentModel: string, extras: string[] = []): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const model of [...models, ...extras, currentModel]) {
    const cleaned = model.trim();
    if (!cleaned || seen.has(cleaned)) continue;
    seen.add(cleaned);
    result.push(cleaned);
  }
  return result;
}

export { InteractionCard };

export function Timeline({ state, respond, cancelQueued }: {
  state: ViewState;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
  cancelQueued: (commandId: string) => void;
}) {
  const tr = useT();
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [stuck, setStuck] = useState(true);
  const onScroll = () => {
    const node = scroller.current;
    if (!node) return;
    const near = node.scrollHeight - node.scrollTop - node.clientHeight < 48;
    stick.current = near;
    setStuck(near);
  };
  useEffect(() => {
    if (stick.current) scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [state.history.length, state.active_turn?.content, state.active_turn?.tools.length, state.timeline?.length, state.pending_interactions.length]);
  const timeline = state.timeline ?? [];
  const known = new Set(timeline.map((item) => item.id));
  const liveTools = (state.active_turn?.tools ?? []).filter((tool) => !known.has(tool.call_id));
  const liveText = state.active_turn?.content && !timeline.some((item) => item.kind === "text" && item.text === state.active_turn?.content) ? state.active_turn.content : "";
  const pending = state.pending_interactions.filter((item) => !known.has(item.request_id));
  return <div className="timeline" ref={scroller} onScroll={onScroll}>
    {!timeline.length && !state.history.length && !state.active_turn && <p className="empty-small">{tr("web.empty_timeline")}</p>}
    {timeline.length > 0 ? <>
      {timeline.map((item) => {
        if (item.kind === "text" && item.role === "user") return <UserMessage key={item.id} text={item.text || ""} />;
        if (item.kind === "text") return <SummaryRow key={item.id} text={item.text || ""} />;
        if (item.kind === "approval" && item.interaction) {
          const stillPending = state.pending_interactions.some((entry) => entry.request_id === item.interaction?.request_id);
          if (!stillPending) return null;
          return <InteractionCard key={item.id} interaction={item.interaction} respond={respond} />;
        }
        return toolCardFromTimeline(item);
      })}
      {liveTools.map((tool) => toolCardFromState(tool))}
      {liveText && <SummaryRow text={liveText} />}
      {state.history.filter((turn) => turn.status === "failed" || turn.status === "cancelled").map((turn) => <p className="form-error" key={turn.turn_id || turn.status}>{turn.status === "cancelled" ? tr("web.turn_cancelled") : tr("web.turn_failed")}</p>)}
    </> : <>
      {state.history.map((turn, index) => <div className="turn" key={turn.turn_id || index}>
        {turn.user && <UserMessage text={turn.user} />}
        {turn.tools?.map((tool) => toolCardFromState(tool))}
        {turn.status === "failed" && <p className="form-error">{tr("web.turn_failed")}</p>}
        {turn.status === "cancelled" && <p className="form-error">{tr("web.turn_cancelled")}</p>}
        {turn.assistant ? <SummaryRow text={turn.assistant} /> : turn.status === "failed" || turn.status === "cancelled" ? null : <p className="empty-small">{tr("web.no_answer")}</p>}
      </div>)}
      {state.active_turn && <div className="turn">
        {state.active_turn.prompt && <UserMessage text={state.active_turn.prompt} />}
        {state.active_turn.tools.map((tool) => toolCardFromState(tool))}
        {state.active_turn.content ? <SummaryRow text={state.active_turn.content} /> : <p className="empty-small">Working…</p>}
      </div>}
    </>}
    {(state.agents ?? []).map((agent) => <section className="agent-progress" key={agent.task_id}><strong>Subagent {agent.task_id} · {agent.status}</strong>{agent.content && <MarkdownContent content={agent.content} />}</section>)}
    {pending.map((item) => <InteractionCard key={item.request_id} interaction={item} respond={respond} />)}
    {state.queued_commands.map((item) => <div className="queue-item" key={item.command_id}><span>{item.prompt}</span><button type="button" className="button" onClick={() => cancelQueued(item.command_id)}>{tr("web.remove_queued")}</button></div>)}
    {state.notices.slice(-5).map((notice) => <div className="system-notice" role="status" key={notice.id}>{present(notice.code, notice.params, notice.text)}</div>)}
    {!stuck && <button type="button" className="button jump" onClick={() => { stick.current = true; setStuck(true); scroller.current?.scrollTo({ top: scroller.current.scrollHeight }); }}>{tr("web.jump_bottom")}</button>}
  </div>;
}

export function Composer({
  sessionId, connection, draft, updateDraft, submit, cancel, cancelAll, running, models = [], currentModel = "", interactionMode = "agent", permissionMode = "default", onModel, onPolicy,
  projectId = null, chooseEnvironment = false, git = false, workLabel = "",
}: {
  sessionId: string;
  connection: ViewState["connection"];
  draft: Draft;
  updateDraft: (sessionId: string, patch: (current: Draft) => Draft) => void;
  submit: (commandId: string, prompt: string, attachmentIds: string[], documentIds?: string[], references?: FileReference[]) => Promise<"accepted" | "rejected" | "unknown">;
  cancel: () => Promise<void>;
  cancelAll?: () => Promise<void>;
  running: boolean;
  models?: string[];
  currentModel?: string;
  interactionMode?: string;
  permissionMode?: string;
  onModel?: (model: string) => void;
  onPolicy?: (body: { interaction_mode?: string; permission_mode?: string }) => Promise<void>;
  projectId?: string | null;
  chooseEnvironment?: boolean;
  git?: boolean;
  workLabel?: string;
}) {
  const tr = useT();
  const [uploading, setUploading] = useState(false);
  const [menu, setMenu] = useState<null | { kind: "file" | "command"; query: string; index: number }>(null);
  const [results, setResults] = useState<Array<{ id: string; label: string; insert: string; reference?: FileReference }>>([]);
  const [menuError, setMenuError] = useState("");
  const imageInput = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const upload = async (files: FileList | File[], images: boolean) => {
    const owner = sessionId;
    const selected = Array.from(files);
    if (!selected.length) return;
    if (chooseEnvironment) {
      updateDraft(owner, (current) => ({
        ...current,
        localFiles: [...(current.localFiles ?? []), ...selected.map((file) => ({ name: file.name, file, image: images }))],
      }));
      return;
    }
    setUploading(true);
    updateDraft(owner, (current) => ({ ...current, reason: "" }));
    const uploaded: Attachment[] = [];
    const documents: Array<{ id: string; filename: string }> = [];
    const errors: string[] = [];
    for (const file of selected) {
      try {
        if (images && file.type.startsWith("image/")) uploaded.push(await api.uploadAttachment(owner, file) as Attachment);
        else documents.push(await api.uploadDocument(owner, file));
      } catch (reason) { errors.push(reason instanceof Error ? reason.message : String(reason)); }
    }
    if (uploaded.length || documents.length) {
      updateDraft(owner, (current) => ({
        ...current,
        attachments: [...current.attachments, ...uploaded],
        documents: [...(current.documents ?? []), ...documents],
      }));
    }
    if (errors.length) updateDraft(owner, (current) => ({ ...current, reason: errors.join("; ") }));
    setUploading(false);
  };
  const sending = useRef(false);
  const send = async (event?: FormEvent) => {
    event?.preventDefault();
    if (sending.current || draft.phase === "awaiting") return;
    const current = draft;
    if (!current.prompt.trim() && !current.attachments.length && !(current.documents ?? []).length && !(current.references ?? []).length && !(current.localFiles ?? []).length) return;
    sending.current = true;
    try {
      const commandId = current.commandId && current.phase !== "rejected" ? current.commandId : crypto.randomUUID();
      updateDraft(sessionId, (item) => ({ ...item, commandId, phase: "awaiting", reason: "" }));
      const outcome = await submit(commandId, current.prompt.trim(), current.attachments.map((item) => item.id), (current.documents ?? []).map((item) => item.id), current.references ?? []);
      const next = commandAfter(outcome === "accepted" ? "accepted" : outcome === "rejected" ? "rejected" : "disconnected");
      if (next.phase === "accepted") updateDraft(sessionId, (item) => item.commandId === commandId ? emptyDraft() : item);
      else updateDraft(sessionId, (item) => ({
        ...item,
        commandId: next.keepId ? commandId : null,
        phase: next.phase,
        reason: next.phase === "rejected"
          ? "The server rejected this command. Edit it and send again to start a new one. Acceptance only means the command was received."
          : "Not confirmed. Retry keeps this command id and does not start a second one.",
      }));
    } finally {
      sending.current = false;
    }
  };
  const refreshMenu = (value: string, caret: number) => {
    const before = value.slice(0, caret);
    const match = /(^|\s)([@/])([^\s]*)$/.exec(before);
    if (!match) { setMenu(null); return; }
    const kind = match[2] === "@" ? "file" : "command";
    setMenu({ kind, query: match[3], index: 0 });
  };
  useEffect(() => {
    if (!menu) return;
    let cancelled = false;
    const load = menu.kind === "command"
      ? api.commands().then((commands) => commands.filter((command) => command.name.toLowerCase().includes(menu.query.toLowerCase())).map((command) => ({ id: command.name, label: `${command.name} ${command.description}`, insert: command.name })))
      : (projectId ? api.references(projectId, menu.query) : api.search(sessionId, menu.query, "file")).then((found) => {
          const rows = "results" in found ? found.results : [];
          return rows.map((item) => ({
            id: String(item.path),
            label: String(item.path),
            insert: "",
            reference: {
              kind: "file" as const,
              path: String(item.path),
              name: String(item.name ?? item.path),
              project_id: String(item.project_id ?? projectId ?? ""),
              external: Boolean(item.external),
            },
          }));
        });
    load.then((items) => { if (!cancelled) { setResults(items); setMenuError(""); } }).catch((error) => { if (!cancelled) setMenuError(String(error)); });
    return () => { cancelled = true; };
  }, [menu?.kind, menu?.query, sessionId, projectId]);
  const applyMenu = (item: { insert: string; reference?: FileReference }) => {
    const node = box.current;
    const value = draft.prompt;
    const caret = node?.selectionStart ?? value.length;
    if (item.reference) {
      const before = value.slice(0, caret).replace(/(^|\s)[@/][^\s]*$/, "$1");
      updateDraft(sessionId, (current) => ({
        ...current,
        prompt: before + value.slice(caret),
        references: [...(current.references ?? []).filter((ref) => ref.path !== item.reference?.path), item.reference as FileReference],
      }));
    } else {
      const before = value.slice(0, caret).replace(/(^|\s)([@/])([^\s]*)$/, `$1${item.insert} `);
      updateDraft(sessionId, (current) => ({ ...current, prompt: before + value.slice(caret) }));
    }
    setMenu(null);
    node?.focus();
  };
  const offline = connection === "disconnected" || connection === "reconnecting" || connection === "closed";
  return <form className="composer" onSubmit={(event) => { send(event).catch((reason) => updateDraft(sessionId, (item) => ({ ...item, phase: "unknown", reason: String(reason) }))); }}>
    <input ref={imageInput} className="file-input" type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => { if (event.target.files) upload(event.target.files, true); event.target.value = ""; }} />
    <input ref={fileInput} className="file-input" type="file" multiple onChange={(event) => { if (event.target.files) upload(event.target.files, false); event.target.value = ""; }} />
    {chooseEnvironment && <div className="row-actions">
      <span className="hint">{workLabel}</span>
      <button type="button" className="button" aria-pressed={(draft.environment ?? "local") === "local"} onClick={() => updateDraft(sessionId, (current) => ({ ...current, environment: "local" }))}>{tr("web.work_here")}</button>
      <button type="button" className="button" aria-pressed={draft.environment === "worktree"} disabled={!git} onClick={() => updateDraft(sessionId, (current) => ({ ...current, environment: "worktree" }))}>{tr("web.isolated_option")}</button>
      <span className="hint">{draft.environment === "worktree" ? tr("web.isolated_help") : tr("web.current_help")}</span>
    </div>}
    {(draft.attachments.length > 0 || (draft.documents ?? []).length > 0 || (draft.references ?? []).length > 0 || (draft.localFiles ?? []).length > 0) && <div className="chips">
      {draft.attachments.map((attachment) => <span className="chip" key={attachment.id}>{attachment.filename}<button type="button" aria-label={tr("web.remove", { name: attachment.filename })} onClick={() => {
        const owner = sessionId;
        api.deleteAttachment(owner, attachment.id).then(() => {
          updateDraft(owner, (current) => ({ ...current, attachments: current.attachments.filter((item) => item.id !== attachment.id) }));
        }).catch((error) => updateDraft(owner, (current) => ({ ...current, reason: String(error) })));
      }}><X size={10} /></button></span>)}
      {(draft.documents ?? []).map((document) => <span className="chip" key={document.id}>{document.filename}<button type="button" aria-label={tr("web.remove", { name: document.filename })} onClick={() => updateDraft(sessionId, (current) => ({ ...current, documents: (current.documents ?? []).filter((item) => item.id !== document.id) }))}><X size={10} /></button></span>)}
      {(draft.references ?? []).map((reference) => <span className="chip" key={reference.path}>{reference.path}<button type="button" aria-label={tr("web.remove", { name: reference.name })} onClick={() => updateDraft(sessionId, (current) => ({ ...current, references: (current.references ?? []).filter((item) => item.path !== reference.path) }))}><X size={10} /></button></span>)}
      {(draft.localFiles ?? []).map((file, index) => <span className="chip" key={`${file.name}-${index}`}>{file.name}<button type="button" aria-label={tr("web.remove", { name: file.name })} onClick={() => updateDraft(sessionId, (current) => ({ ...current, localFiles: (current.localFiles ?? []).filter((_, item) => item !== index) }))}><X size={10} /></button></span>)}
    </div>}
    {(draft.references ?? []).length > 0 && <p className="hint">{tr("web.capture_hint")}</p>}
    <textarea ref={box} aria-label={tr("web.message")} rows={2} value={draft.prompt} placeholder={running ? tr("web.queue_placeholder") : tr("web.message_placeholder")} onChange={(event) => {
      const value = event.target.value;
      updateDraft(sessionId, (item) => ({ ...item, prompt: value }));
      refreshMenu(value, event.target.selectionStart);
    }} onKeyDown={(event) => {
      if (menu && results.length) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          setMenu({ ...menu, index: (menu.index + (event.key === "ArrowDown" ? 1 : results.length - 1)) % results.length });
          return;
        }
        if (event.key === "Enter" || event.key === "Tab") { event.preventDefault(); applyMenu(results[menu.index]); return; }
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setMenu(null); return; }
      }
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
    }} />
    {menu && <div className="mention-menu" role="listbox">
      {menuError && <p className="form-error" role="alert">{menuError}</p>}
      {!results.length && !menuError && <p className="empty-small">No matches</p>}
      {results.map((item, index) => <button type="button" role="option" aria-selected={index === menu.index} key={item.id} onMouseDown={(event) => { event.preventDefault(); applyMenu(item); }}>{item.label}</button>)}
    </div>}
    <div className="composer-actions">
      <div className="row-actions">
        <button type="button" className="icon-button" title={tr("web.attach")} onClick={() => imageInput.current?.click()}><Paperclip size={14} /></button>
        <button type="button" className="icon-button" title={tr("web.attach_file")} aria-label={tr("web.attach_file")} onClick={() => fileInput.current?.click()}><FileCode size={14} /></button>
        <button type="button" className="icon-button" title="Reference file" onClick={() => { updateDraft(sessionId, (item) => ({ ...item, prompt: `${item.prompt}@` })); setMenu({ kind: "file", query: "", index: 0 }); box.current?.focus(); }}>@</button>
        <button type="button" className="icon-button" title="Commands" onClick={() => { updateDraft(sessionId, (item) => ({ ...item, prompt: `${item.prompt}/` })); setMenu({ kind: "command", query: "", index: 0 }); box.current?.focus(); }}>/</button>
        <span className="hint">{draft.reason || (uploading ? "Uploading…" : "") || (draft.phase === "awaiting" ? "Waiting for the server to accept this command." : "") || (offline ? "Offline. The draft stays here until this command is confirmed." : "")}</span>
      </div>
      <div className="row-actions">
        <select className="select" aria-label={tr("web.interaction_mode")} value={interactionMode} onChange={(event) => onPolicy?.({ interaction_mode: event.target.value }).catch((error) => updateDraft(sessionId, (item) => ({ ...item, reason: String(error) })))}>
          <option value="agent">{tr("web.mode.agent")}</option>
          <option value="plan">{tr("web.mode.plan")}</option>
          <option value="ask">{tr("web.mode.ask")}</option>
        </select>
        <select className="select" aria-label={tr("web.model")} value={currentModel} onChange={(event) => onModel?.(event.target.value)}>
          {visibleModels(models, currentModel).map((model) => <option key={model} value={model}>{model}</option>)}
        </select>
        <select className="select" aria-label={tr("web.permission_mode")} value={permissionMode || "default"} onChange={(event) => onPolicy?.({ permission_mode: event.target.value }).catch((error) => updateDraft(sessionId, (item) => ({ ...item, reason: String(error) })))}>
          <option value="default">{tr("web.perm.default")}</option>
          <option value="acceptEdits">{tr("web.perm.acceptEdits")}</option>
          <option value="plan">{tr("web.perm.plan")}</option>
          <option value="bypass">{tr("web.perm.bypass")}</option>
        </select>
        {running && <button type="button" className="button stop" onClick={() => { cancel().catch(() => undefined); }}><Square size={11} weight="fill" />{tr("web.stop")}</button>}
        {running && cancelAll && <button type="button" className="button stop" onClick={() => { cancelAll().catch(() => undefined); }}>{tr("web.stop_all")}</button>}
        <button className="button primary" disabled={uploading || draft.phase === "awaiting" || (!draft.prompt.trim() && !draft.attachments.length && !(draft.documents ?? []).length && !(draft.references ?? []).length && !(draft.localFiles ?? []).length)}><PaperPlaneRight size={13} />{draft.phase === "unknown" ? tr("web.retry") : tr("web.send")}</button>
      </div>
    </div>
  </form>;
}

type InspectorTab = "task" | "files" | "changes" | "permissions" | "context";

export function Inspector({ state, sessionId, open, narrow = false, focusPath = null, close, staticData = null }: { state: ViewState; sessionId: string; open: boolean; narrow?: boolean; focusPath?: string | null; close: () => void; staticData?: { files: Array<{ name: string; path: string; kind: string }>; changes: Array<{ path: string; status: string }>; review: ReviewChange[] } | null }) {
  const tr = useT();
  const [tab, setTab] = useState<InspectorTab>("task");
  const [changes, setChanges] = useState<Array<{ path: string; status: string }> | null>(null);
  const [changesError, setChangesError] = useState("");
  const [warning, setWarning] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [patch, setPatch] = useState("");
  const [wide, setWide] = useState(false);
  const [review, setReview] = useState<ReviewChange[]>([]);
  const [reviewNote, setReviewNote] = useState("");
  const [files, setFiles] = useState<Array<{ name: string; path: string; kind: string }>>([]);
  const [fileText, setFileText] = useState("");
  const [grants, setGrants] = useState<Record<string, unknown> | null>(null);
  const [panelError, setPanelError] = useState("");
  const generation = useRef(0);
  useEffect(() => {
    setSelected(null); setPatch(""); setChanges(null); setChangesError(""); setReview([]);
  }, [sessionId]);
  const refresh = useCallback(() => {
    if (staticData) {
      setChanges(staticData.changes);
      setReview(staticData.review);
      setChangesError("");
      return;
    }
    const request = ++generation.current;
    const owner = sessionId;
    api.changes(owner).then((result) => {
      if (generation.current !== request) return;
      setChanges(result.changes);
      setWarning(result.local_warning);
      setChangesError("");
    }).catch((error) => {
      if (generation.current !== request) return;
      setChangesError(error instanceof Error ? error.message : String(error));
      setChanges(null);
    });
    api.review(owner).then((result) => {
      if (generation.current !== request) return;
      setReview((result.changes ?? []) as ReviewChange[]);
    }).catch(() => { if (generation.current === request) setReview([]); });
  }, [sessionId, staticData]);
  useEffect(() => { refresh(); }, [refresh, state.session.status]);
  useEffect(() => {
    if (tab === "files") {
      if (staticData) { setFiles(staticData.files); return; }
      api.tree(sessionId).then((result) => setFiles(result.entries)).catch((error) => setPanelError(String(error)));
    }
    if (tab === "permissions" && !staticData) api.grants(sessionId).then(setGrants).catch((error) => setPanelError(String(error)));
  }, [tab, sessionId, staticData]);
  useEffect(() => {
    if (!focusPath) return;
    setTab("files");
    api.file(sessionId, focusPath).then((file) => setFileText(file.binary ? "Binary file" : file.content)).catch((error) => setPanelError(String(error)));
  }, [focusPath, sessionId]);
  const openPatch = (path: string) => {
    const request = ++generation.current;
    setSelected(path);
    setPatch("");
    api.patch(sessionId, path).then((result) => {
      if (generation.current !== request) return;
      setPatch(result.patch);
    }).catch((error) => {
      if (generation.current !== request) return;
      setPatch("");
      setChangesError(error instanceof Error ? error.message : String(error));
    });
  };
  const paired = (path: string) => {
    const item = review.find((entry) => entry.path === path);
    return item?.rename_with ? [path, item.rename_with] : [path];
  };
  const act = async (action: "accept" | "revert", paths: string[]) => {
    const unique = [...new Set(paths)];
    if (!unique.length) { setReviewNote("Nothing is ready for this action."); return; }
    setReviewNote("");
    try {
      const result = await api.reviewAction(sessionId, action, unique);
      if (!result.applied) setReviewNote(result.results.map((item) => `${item.path}: ${item.state} ${item.result}`).join("; ") || result.error || "The server did not apply this review.");
      refresh();
    } catch (error) { setReviewNote(String(error)); }
  };
  const breakdown = state.context_breakdown;
  const tabs: InspectorTab[] = ["task", "files", "changes", "permissions", "context"];
  return <aside className={`inspector ${open ? "" : "is-closed"} ${narrow ? "narrow-open" : ""}`}>
    <div className="inspector-tabs" role="tablist">
      {tabs.map((item) => <button key={item} role="tab" aria-selected={tab === item} onClick={() => setTab(item)}>{tr(`web.tab.${item}`)}</button>)}
      <button className="icon-button" onClick={close} aria-label={tr("web.close_inspector")}><X size={14} /></button>
    </div>
    {tab === "task" && <div className="inspector-body">
      <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.goal")}</span>{state.plan.steps?.length ? <span>{state.plan.steps.filter((step) => step.status === "completed").length} / {state.plan.steps.length}</span> : null}</div>
        <p>{state.plan.objective || state.session.user_goal || tr("web.no_plan")}</p>
        {state.plan.steps?.length ? <div className="progress"><i style={{ width: `${Math.round((state.plan.steps.filter((step) => step.status === "completed").length / state.plan.steps.length) * 100)}%` }} /></div> : null}
      </section>
      <div className="section-label"><span>{tr("web.execution_plan")}</span></div>
      <div className="step-list">{state.plan.steps?.map((step) => <article key={step.id} className={`step-card ${step.status}`}><Check size={14} /><div><strong>{step.title}</strong><small>{step.status}{step.note ? ` · ${step.note}` : ""}</small></div></article>)}</div>
      {!state.plan.steps?.length && <p className="empty-small">{tr("web.no_plan")}</p>}
      <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.accessed_files")}</span><span>{(state.accessed_files ?? []).length}</span></div>
        {(state.accessed_files ?? []).map((file) => <div className="file-stat" key={`${file.call_id}-${file.path}`}><span>{file.path}</span><span>{file.access || ""}</span></div>)}
      </section>
      <section className="goal-card">
        <div className="card-kicker"><span>{tr("web.subagents")}</span><span>{(state.subagents ?? []).length}</span></div>
        {(state.subagents ?? []).map((agent) => <div className="subagent-row" key={agent.task_id}><span className="status-dot" /><span>{agent.task || agent.task_id}</span><span>{agent.status}</span></div>)}
        {!(state.subagents ?? []).length && <p className="empty-small">{tr("web.no_subagents")}</p>}
      </section>
    </div>}
    {tab === "files" && <div className="inspector-body">
      <div className="section-label"><span>{tr("web.workspace_resources")}</span></div>
      {panelError && <p className="form-error" role="alert">{panelError}</p>}
      {files.map((entry) => <button className="resource-row" key={entry.path} onClick={() => api.file(sessionId, entry.path).then((file) => setFileText(file.binary ? "Binary file" : file.content)).catch((error) => setPanelError(String(error)))}><b>{entry.kind === "dir" ? "DIR" : "FILE"}</b><span>{entry.path}</span></button>)}
      {!files.length && <p className="empty-small">This directory is empty.</p>}
      {fileText && <pre className="term-body">{fileText}</pre>}
    </div>}
    {tab === "changes" && <div className="inspector-body">
      {warning && <div className="local-warning"><Warning size={14} />{tr("web.local_warning")}</div>}
      {changesError && <p className="form-error" role="alert">{changesError}</p>}
      {reviewNote && <p className="form-error" role="status">{reviewNote}</p>}
      <div className="changes-head">
        <div className="section-label"><span>{tr("web.working_tree")}</span></div>
        <button type="button" className="button accept" onClick={() => act("accept", review.filter((item) => item.state === "task").map((item) => item.path))}>{tr("web.accept_ready")}</button>
        <button type="button" className="button revert" onClick={() => {
          const paths = review.filter((item) => item.state === "task" || item.state === "accepted").map((item) => item.path);
          if (!paths.length) { setReviewNote("Nothing is ready to revert."); return; }
          if (window.confirm(`Revert ${paths.join(", ")}? Bytes return to the version from before this task first changed each file. A conflict rejects the whole batch.`)) act("revert", paths);
        }}>{tr("web.revert_ready")}</button>
      </div>
      <div className="change-list">{(changes ?? []).map((change) => <button key={change.path} className={selected === change.path ? "change-card selected" : "change-card"} onClick={() => openPatch(change.path)}><b>{change.status}</b><span>{change.path}</span></button>)}</div>
      {review.map((item) => <div className="queue-item" key={`review-${item.path}`}><span>{item.display_kind === "rename" ? `rename ${item.kind === "delete" ? item.path : item.rename_with} → ${item.kind === "delete" ? item.rename_with : item.path}` : item.path} · {item.state}</span>
        <button type="button" className="button" onClick={() => act("accept", paired(item.path))}>{tr("web.accept_review")}</button>
        <button type="button" className="button" onClick={() => { const paths = paired(item.path); if (window.confirm(`Revert ${paths.join(", ")}? Bytes return to the version from before this task first changed the file.`)) act("revert", paths); }}>{tr("web.revert_change")}</button>
      </div>)}
      {selected && <div className="patch">{patch ? <DiffViewer patch={patch} filename={selected} /> : null}<button type="button" className="button" onClick={() => setWide(true)}>{tr("web.expand_diff")}</button></div>}
      {changes && !changes.length && !changesError && <p className="empty-small">{tr("web.tree_clean")}</p>}
      {wide && selected && <div className="wide-diff"><button type="button" className="button" onClick={() => setWide(false)}>{tr("web.close")}</button><DiffViewer patch={patch} filename={selected} /></div>}
    </div>}
    {tab === "permissions" && <div className="inspector-body">
      <div className="section-label"><span>{tr("web.security_policy")}</span></div>
      {panelError && <p className="form-error" role="alert">{panelError}</p>}
      <article className="policy-card"><div><strong>{tr("web.permission_mode")}</strong><span>{String(grants?.permission_mode ?? state.session.permission_mode ?? "default")}</span></div><small>{String(grants?.interaction_mode ?? state.session.interaction_mode ?? "agent")}</small></article>
      {((grants?.boundaries as string[] | undefined) ?? []).map((item) => <article className="policy-card" key={item}><div><strong>{item}</strong></div></article>)}
      {((grants?.session_rules as Array<{ id: string }> | undefined) ?? []).map((rule) => <article className="policy-card" key={rule.id}><div><strong>{rule.id}</strong><button type="button" className="text-button" onClick={() => api.revokeGrant(sessionId, rule.id).then(() => api.grants(sessionId).then(setGrants)).catch((error) => setPanelError(String(error)))}>Revoke</button></div><small>session</small></article>)}
      {((grants?.persistent_rules as Array<{ id: string }> | undefined) ?? []).map((rule) => <article className="policy-card warn" key={rule.id}><div><strong>{rule.id}</strong><button type="button" className="text-button" onClick={() => api.revokeGrant(sessionId, rule.id).then(() => api.grants(sessionId).then(setGrants)).catch((error) => setPanelError(String(error)))}>Revoke</button></div><small>persistent</small></article>)}
    </div>}
    {tab === "context" && <div className="inspector-body">
      <div className="section-label"><span>{tr("web.token_breakdown")}</span></div>
      <section className="stat-card">
        {(breakdown?.categories ?? []).map((item) => <div className="stat-row" key={item.id}><span>{item.id}</span><span>{item.tokens} tok{typeof item.share === "number" ? ` (${Math.round(item.share * 100)}%)` : ""}</span></div>)}
        <div className="stat-total"><span>{tr("web.estimate")}</span><span>{breakdown?.total ?? "unknown"} / {breakdown?.limit ?? "unknown"}</span></div>
      </section>
      {breakdown?.note && <p className="hint">{breakdown.note}</p>}
      <p className="hint">Billing {state.usage.total_tokens ?? "unknown"} total tokens</p>
    </div>}
  </aside>;
}

export function SessionRail({ sessions, selected, onSelect, onCreate, open, children, footer = "" }: {
  sessions: SessionSummary[];
  selected: string | null;
  onSelect: (session: SessionSummary) => void;
  onCreate: () => void;
  open: boolean;
  children?: ReactNode;
  footer?: string;
}) {
  const tr = useT();
  const active = sessions.filter((item) => item.active);
  const history = sessions.filter((item) => !item.active);
  return <aside className={`sidebar ${open ? "responsive-open" : ""}`}>
    <div className="sidebar-scroll">
      <button className="primary-action" onClick={onCreate} aria-label={tr("web.new_session")}><span><Plus size={14} /> {tr("web.new_task")}</span><kbd>⌘K</kbd></button>
      <div>
        <div className="section-label"><span>{tr("web.active_tasks")}</span>{active.length > 0 && <span className="live-dot" />}</div>
        {active.map((session) => {
          const title = session.user_goal && session.user_goal !== "(interactive session)" ? session.user_goal : tr("web.session_label", { id: session.session_id.slice(0, 6) });
          const current = selected === session.session_id;
          const execution = session.execution || session.status;
          return <button key={session.session_id} className={current ? "task-card" : "task-row"} onClick={() => onSelect(session)}>
            {current ? <>
              <span className="task-card-top"><span className={`status-dot ${execution}`} /><span className="task-id">{session.session_id.slice(0, 8)}</span></span>
              <span className="task-title">{title}</span>
              <span className="task-meta">{tr(`web.status.${execution}`)}{session.environment ? ` · ${session.environment}` : ""}</span>
            </> : <>
              <span className="status-dot" /><span className="task-id">{session.session_id.slice(0, 4)}</span><span className="task-title">{title}</span><span className="task-meta">{tr(`web.status.${execution}`)}</span>
            </>}
          </button>;
        })}
        {!active.length && <p className="empty-small">{tr("web.no_active")}</p>}
      </div>
      <div>
        <div className="section-label"><span>{tr("web.recent_tasks")}</span></div>
        {history.map((session) => <button key={session.session_id} className="recent-row" disabled={session.recoverable === false} onClick={() => onSelect(session)}><span className={session.status === "failed" ? "recent-mark fail" : "recent-mark ok"} />{session.user_goal || session.session_id.slice(0, 6)}</button>)}
      </div>
      {children}
    </div>
    {footer && <div className="sidebar-foot"><span>{footer}</span></div>}
  </aside>;
}

export function archiveConfirm(): boolean {
  return window.confirm("Archive this session? A clean isolated worktree may be removed. The project directory stays.");
}

export { Archive };
