import { ArrowUp, At, CaretDown, ChatCircleDots, Compass, Desktop, FolderSimple, GitFork, Lightning, Paperclip, Shield, ShieldCheck, ShieldWarning, Square, Warning, X } from "@phosphor-icons/react";
import { FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { useT } from "../../i18n";
import { commandAfter } from "../../protocol";
import type { Attachment, ExecutionMode, FileReference, ViewState } from "../../types";
import { emptyDraft, type Draft } from "./draft";
import { MenuButton } from "../ui/Menu";
import { Select } from "../ui/Select";
import { shortPath } from "../session/session";

export function EnvironmentChoice({
  sessionId, draft, updateDraft, git = false, workLabel = "",
}: {
  sessionId: string;
  draft: Draft;
  updateDraft: (sessionId: string, patch: (current: Draft) => Draft) => void;
  git?: boolean;
  workLabel?: string;
  compact?: boolean;
}) {
  const tr = useT();
  const local = (draft.environment ?? "local") === "local";
  const host = draft.execution_mode === "host";
  const setEnvironment = (environment: "local" | "worktree") => updateDraft(sessionId, (current) => ({ ...current, environment }));
  const setExecution = (execution_mode: ExecutionMode) => updateDraft(sessionId, (current) => ({ ...current, execution_mode }));
  return <div className="environment-choice">
    <div className="environment-pills">
      {workLabel && <span className="env-pill static" title={workLabel}><FolderSimple size={13} /><span className="env-pill-text">{shortPath(workLabel)}</span></span>}
      <MenuButton className="env-pill" label={tr("web.environment_label")} title={tr("web.environment_label")} align="start" wide items={[
        { label: tr("web.work_here"), hint: tr("web.current_help"), checked: local, onSelect: () => setEnvironment("local") },
        { label: tr("web.isolated_option"), hint: git ? tr("web.isolated_help") : tr("web.isolated_requires_git"), checked: !local, disabled: !git, onSelect: () => setEnvironment("worktree") },
      ]}>{local ? <Desktop size={13} /> : <GitFork size={13} />}<span>{local ? tr("web.work_here") : tr("web.isolated_option")}</span><CaretDown size={10} weight="bold" /></MenuButton>
      <MenuButton className={`env-pill${host ? " warn" : ""}`} label={tr("web.execution_label")} title={tr("web.execution_label")} align="start" wide items={[
        { label: tr("web.execution.sandbox"), hint: tr("web.execution.sandbox_help"), checked: !host, onSelect: () => setExecution("sandbox") },
        { label: tr("web.execution.host"), hint: tr("web.execution.host_help"), checked: host, onSelect: () => setExecution("host") },
      ]}>{host ? <Warning size={13} /> : <ShieldCheck size={13} />}<span>{tr(host ? "web.execution.host" : "web.execution.sandbox")}</span><CaretDown size={10} weight="bold" /></MenuButton>
    </div>
    {host && <p className="hint environment-warning">{tr("web.execution.host_help")}</p>}
  </div>;
}

/** Image types the attachment endpoint accepts; anything else is uploaded as a document. */
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const isImage = (file: File) => IMAGE_TYPES.has(file.type);

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

const MODES = ["agent", "plan", "ask"] as const;
const MODE_ICONS = {
  agent: <Lightning size={15} />,
  plan: <Compass size={15} />,
  ask: <ChatCircleDots size={15} />,
} as const;
const PERM_ICONS = {
  default: <Shield size={15} />,
  acceptEdits: <Lightning size={15} />,
  bypass: <Warning size={15} />,
  plan: <Compass size={15} />,
} as const;

/** The keys worth knowing, shown under the composer. */
export function ComposerHints() {
  const tr = useT();
  return <div className="composer-hints hint" aria-hidden="true">
    <span><kbd>↵</kbd>{tr("web.hint.send")}</span>
    <span><kbd>⇧↵</kbd>{tr("web.hint.newline")}</span>
    <span><kbd>/</kbd>{tr("web.hint.commands")}</span>
    <span><kbd>@</kbd>{tr("web.hint.files")}</span>
    <span><kbd>⇧Tab</kbd>{tr("web.hint.mode")}</span>
    <span><kbd>{tr("web.shortcut_key", { key: "K" })}</kbd>{tr("web.hint.search")}</span>
  </div>;
}

export function Composer({
  sessionId, connection, draft, updateDraft, submit, cancel, cancelAll, running, models = [], currentModel = "", interactionMode = "agent", permissionMode = "default", executionMode = "sandbox", onModel, onPolicy,
  projectId = null, chooseEnvironment = false, deferUploads = chooseEnvironment, git = false, workLabel = "", context,
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
  executionMode?: ExecutionMode;
  onModel?: (model: string) => void;
  onPolicy?: (body: { interaction_mode?: string; permission_mode?: string }) => Promise<void>;
  projectId?: string | null;
  chooseEnvironment?: boolean;
  deferUploads?: boolean;
  git?: boolean;
  workLabel?: string;
  /** Context usage meter, shown beside the mode and model it depends on. */
  context?: ReactNode;
}) {
  const tr = useT();
  const [uploading, setUploading] = useState(false);
  const [menu, setMenu] = useState<null | { kind: "file" | "command"; query: string; index: number }>(null);
  const [results, setResults] = useState<Array<{ id: string; label: string; insert: string; reference?: FileReference }>>([]);
  const [menuError, setMenuError] = useState("");
  const [menuLoading, setMenuLoading] = useState(false);
  const composing = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const upload = async (files: FileList | File[]) => {
    const owner = sessionId;
    const selected = Array.from(files);
    if (!selected.length) return;
    if (deferUploads) {
      updateDraft(owner, (current) => ({
        ...current,
        localFiles: [...(current.localFiles ?? []), ...selected.map((file) => ({ name: file.name, file, image: isImage(file) }))],
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
        if (isImage(file)) uploaded.push(await api.uploadAttachment(owner, file) as Attachment);
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
    if (sending.current || uploading || draft.phase === "awaiting") return;
    const current = draft;
    if (!current.prompt.trim() && !current.command && !current.attachments.length && !(current.documents ?? []).length && !(current.references ?? []).length && !(current.localFiles ?? []).length) return;
    sending.current = true;
    try {
      const commandId = current.commandId && current.phase !== "rejected" ? current.commandId : crypto.randomUUID();
      const commandPrefix = current.command ? `/${current.command.replace(/^\/+/, "")}` : "";
      const prompt = [commandPrefix, current.prompt.trim()].filter(Boolean).join(" ");
      updateDraft(sessionId, (item) => ({ ...item, commandId, phase: "awaiting", reason: "" }));
      const outcome = await submit(commandId, prompt, current.attachments.map((item) => item.id), (current.documents ?? []).map((item) => item.id), current.references ?? []);
      const next = commandAfter(outcome === "accepted" ? "accepted" : outcome === "rejected" ? "rejected" : "disconnected");
      if (next.phase === "accepted") updateDraft(sessionId, (item) => item.commandId === commandId ? emptyDraft() : item);
      else updateDraft(sessionId, (item) => ({
        ...item,
        commandId: next.keepId ? commandId : null,
        phase: next.phase,
        reason: item.reason || (next.phase === "rejected"
          ? tr("web.command_rejected_hint")
          : tr("web.command_unknown_hint")),
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
    if (menu?.kind !== kind || menu.query !== match[3]) {
      setResults([]);
      setMenuError("");
      setMenuLoading(true);
    }
    setMenu({ kind, query: match[3], index: 0 });
  };
  useEffect(() => {
    if (!menu) return;
    let cancelled = false;
    setResults([]);
    setMenuError("");
    setMenuLoading(true);
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
    load.then((items) => { if (!cancelled) { setResults(items); setMenuLoading(false); } }).catch((error) => { if (!cancelled) { setMenuError(String(error)); setMenuLoading(false); } });
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
      const before = value.slice(0, caret).replace(/(^|\s)([@/])([^\s]*)$/, "$1");
      updateDraft(sessionId, (current) => ({ ...current, prompt: before + value.slice(caret), command: item.insert }));
    }
    setMenu(null);
    node?.focus();
  };
  const offline = connection === "disconnected" || connection === "reconnecting" || connection === "closed";
  const composerMode = interactionMode === "agent" ? "auto" : interactionMode;
  return <form className={`composer${dragOver ? " drag" : ""}`} data-mode={composerMode} data-perm={permissionMode || "default"} data-drop={tr("web.drop_files")} onDragEnter={(event) => { event.preventDefault(); setDragOver(true); }} onDragOver={(event) => { event.preventDefault(); setDragOver(true); }} onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragOver(false); }} onDrop={(event) => { event.preventDefault(); setDragOver(false); if (event.dataTransfer.files?.length) upload(event.dataTransfer.files); }} onSubmit={(event) => { send(event).catch((reason) => updateDraft(sessionId, (item) => ({ ...item, phase: "unknown", reason: String(reason) }))); }}>
    <input ref={fileInput} className="file-input" type="file" multiple tabIndex={-1} aria-hidden="true" onChange={(event) => { if (event.target.files) upload(event.target.files); event.target.value = ""; }} />
    {chooseEnvironment && <EnvironmentChoice sessionId={sessionId} draft={draft} updateDraft={updateDraft} git={git} workLabel={workLabel} />}
    {(draft.attachments.length > 0 || (draft.documents ?? []).length > 0 || (draft.references ?? []).length > 0 || (draft.localFiles ?? []).length > 0 || draft.command) && <div className="attachments chips">
      {draft.attachments.map((attachment) => <span className="att chip" key={attachment.id}><span>{attachment.filename}</span><button type="button" aria-label={tr("web.remove", { name: attachment.filename })} onClick={() => {
        const owner = sessionId;
        api.deleteAttachment(owner, attachment.id).then(() => {
          updateDraft(owner, (current) => ({ ...current, attachments: current.attachments.filter((item) => item.id !== attachment.id) }));
        }).catch((error) => updateDraft(owner, (current) => ({ ...current, reason: String(error) })));
      }}><X size={10} /></button></span>)}
      {(draft.documents ?? []).map((document) => <span className="chip" key={document.id}>{document.filename}<button type="button" aria-label={tr("web.remove", { name: document.filename })} onClick={() => updateDraft(sessionId, (current) => ({ ...current, documents: (current.documents ?? []).filter((item) => item.id !== document.id) }))}><X size={10} /></button></span>)}
      {(draft.references ?? []).map((reference) => <span className="chip reference-chip" key={reference.path} title={tr("web.capture_hint")}><span className="chip-marker">@</span>{reference.path}<button type="button" aria-label={tr("web.remove", { name: reference.name })} onClick={() => updateDraft(sessionId, (current) => ({ ...current, references: (current.references ?? []).filter((item) => item.path !== reference.path) }))}><X size={10} /></button></span>)}
      {(draft.localFiles ?? []).map((file, index) => <span className="chip" key={`${file.name}-${index}`}>{file.name}<button type="button" aria-label={tr("web.remove", { name: file.name })} onClick={() => updateDraft(sessionId, (current) => ({ ...current, localFiles: (current.localFiles ?? []).filter((_, item) => item !== index) }))}><X size={10} /></button></span>)}
      {draft.command && <span className="chip command-chip"><span className="chip-marker">/</span>{draft.command.replace(/^\/+/, "")}<button type="button" aria-label={tr("web.remove", { name: draft.command })} onClick={() => updateDraft(sessionId, (current) => ({ ...current, command: null }))}><X size={10} /></button></span>}
    </div>}
    <textarea ref={box} aria-label={tr("web.message")} rows={2} value={draft.prompt} placeholder={running ? tr("web.queue_placeholder") : tr("web.message_placeholder")} onChange={(event) => {
      const value = event.target.value;
      updateDraft(sessionId, (item) => ({ ...item, prompt: value }));
      refreshMenu(value, event.target.selectionStart);
    }} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={(event) => {
      // Enter confirms an IME candidate before it can submit a message or choose a mention.
      if (composing.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
      if (menu && event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setMenu(null); return; }
      if (menu && results.length) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          setMenu({ ...menu, index: (menu.index + (event.key === "ArrowDown" ? 1 : results.length - 1)) % results.length });
          return;
        }
        if (event.key === "Enter" || event.key === "Tab") { event.preventDefault(); applyMenu(results[menu.index]); return; }
      }
      if (menu && (menuLoading || menuError) && event.key === "Enter") { event.preventDefault(); return; }
      // Shift+Tab steps through the interaction modes, as in a terminal agent.
      if (event.key === "Tab" && event.shiftKey && !menu && onPolicy) {
        event.preventDefault();
        const next = MODES[(MODES.indexOf(interactionMode as typeof MODES[number]) + 1) % MODES.length];
        onPolicy({ interaction_mode: next }).catch((error) => updateDraft(sessionId, (item) => ({ ...item, reason: String(error) })));
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
    }} />
    {menu && <div className="mention-menu popup" role="listbox" aria-busy={menuLoading}>
      <div className="pop-h"><span>{tr(menu.kind === "command" ? "web.commands_title" : "web.reference_file")}</span></div>
      {menuError && <p className="form-error" role="alert">{menuError}</p>}
      {!menuLoading && !results.length && !menuError && <p className="empty-small">{tr("web.no_matches")}</p>}
      {results.map((item, index) => <button type="button" className={`pop-i${index === menu.index ? " sel" : ""}`} role="option" aria-selected={index === menu.index} key={item.id} onMouseDown={(event) => { event.preventDefault(); applyMenu(item); }}>{item.label}</button>)}
    </div>}
    <div className="composer-actions cbar">
      <button type="button" className="icon-button icon-btn" title={tr("web.attach_file")} aria-label={tr("web.attach_file")} onClick={() => fileInput.current?.click()}><Paperclip size={15} /></button>
      <button type="button" className="icon-button icon-btn" title={tr("web.reference_file")} aria-label={tr("web.reference_file")} onClick={() => { updateDraft(sessionId, (item) => ({ ...item, prompt: `${item.prompt}@` })); setMenu({ kind: "file", query: "", index: 0 }); box.current?.focus(); }}><At size={15} /></button>
      {onPolicy && <Select className={`select composer-pill pill select-mode mode mode-${interactionMode}`} ariaLabel={tr("web.interaction_mode")} title={`${tr(`web.mode_help.${interactionMode}`)} (⇧Tab)`} value={interactionMode} wide
        onChange={(next) => onPolicy({ interaction_mode: next }).catch((error) => updateDraft(sessionId, (item) => ({ ...item, reason: String(error) })))}
        options={MODES.map((mode) => ({ value: mode, label: tr(`web.mode.${mode}`), hint: tr(`web.mode_help.${mode}`), icon: MODE_ICONS[mode] }))} />}
      {onPolicy && interactionMode === "agent" && <Select className={`select composer-pill pill select-permissions perm-${permissionMode || "default"}`} wide ariaLabel={tr("web.permission_mode")}
        title={tr(permissionMode === "bypass" && executionMode === "host" ? "web.execution.host_bypass" : `web.perm_help.${permissionMode || "default"}`)}
        value={permissionMode || "default"} onChange={(next) => onPolicy({ permission_mode: next }).catch((error) => updateDraft(sessionId, (item) => ({ ...item, reason: String(error) })))}
        options={[
          ...(["default", "acceptEdits", "bypass"] as const).map((mode) => ({ value: mode, label: tr(`web.perm_short.${mode}`), hint: tr(`web.perm.${mode}`), icon: PERM_ICONS[mode] })),
          ...(permissionMode === "plan" ? [{ value: "plan", label: tr("web.perm.plan"), icon: PERM_ICONS.plan }] : []),
        ]} />}
      {executionMode === "host" && <span className="composer-host pill" title={tr("web.execution.host_help")}><ShieldWarning size={13} weight="fill" aria-hidden="true" /><span className="composer-host-label">{tr("web.execution.host")}</span></span>}
      {onModel && <Select className="select composer-pill pill select-model" ariaLabel={tr("web.model")} value={currentModel} onChange={(next) => onModel(next)}
        options={visibleModels(models, currentModel).map((model) => ({ value: model, label: model }))} />}
      {!onModel && currentModel && <span className="composer-model-static pill">{currentModel}</span>}
      <span className="spacer" />
      {context && <span className="ctx-btn">{context}</span>}
      {running && !draft.prompt.trim() && <button type="button" className="send-button send stop" title={`${tr("web.stop")} (${tr("web.shortcut_key", { key: "." })})`} aria-label={tr("web.stop")} onClick={() => { cancel().catch(() => undefined); }}><Square size={12} weight="fill" /></button>}
      {(!running || draft.prompt.trim()) && <button className="send-button send" title={draft.phase === "unknown" ? tr("web.retry") : tr("web.send")} aria-label={draft.phase === "unknown" ? tr("web.retry") : tr("web.send")} disabled={uploading || draft.phase === "awaiting" || (!draft.prompt.trim() && !draft.command && !draft.attachments.length && !(draft.documents ?? []).length && !(draft.references ?? []).length && !(draft.localFiles ?? []).length)}><ArrowUp size={15} weight="bold" /></button>}
    </div>
    {(draft.reason || uploading || draft.phase === "awaiting" || offline || (running && cancelAll)) && <div className="row-actions composer-primary-actions">
      <span className="hint composer-status" role={draft.phase === "rejected" || draft.phase === "unknown" ? "alert" : "status"}>{draft.reason || (uploading ? tr("web.uploading") : "") || (draft.phase === "awaiting" ? tr("web.awaiting_accept") : "") || (offline ? tr("web.offline_hint") : "")}</span>
      {running && cancelAll && <button type="button" className="button stop ghost" onClick={() => { cancelAll().catch(() => undefined); }}>{tr("web.stop_all")}</button>}
    </div>}
  </form>;
}
