import { CaretDown, Desktop, FolderSimple, GitFork, Image, Paperclip, PaperPlaneRight, ShieldCheck, Square, Warning, X } from "@phosphor-icons/react";
import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { useT } from "../../i18n";
import { commandAfter } from "../../protocol";
import type { Attachment, ExecutionMode, FileReference, ViewState } from "../../types";
import { emptyDraft, type Draft } from "./draft";
import { MenuButton } from "../ui/Menu";
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

export function Composer({
  sessionId, connection, draft, updateDraft, submit, cancel, cancelAll, running, models = [], currentModel = "", interactionMode = "agent", permissionMode = "default", executionMode = "sandbox", onModel, onPolicy,
  projectId = null, chooseEnvironment = false, deferUploads = chooseEnvironment, git = false, workLabel = "",
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
}) {
  const tr = useT();
  const [uploading, setUploading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [menu, setMenu] = useState<null | { kind: "file" | "command"; query: string; index: number }>(null);
  const [results, setResults] = useState<Array<{ id: string; label: string; insert: string; reference?: FileReference }>>([]);
  const [menuError, setMenuError] = useState("");
  const [menuLoading, setMenuLoading] = useState(false);
  const composing = useRef(false);
  const imageInput = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const upload = async (files: FileList | File[], images: boolean) => {
    const owner = sessionId;
    const selected = Array.from(files);
    if (!selected.length) return;
    if (deferUploads) {
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
  const settingsSummary = [tr(`web.mode.${interactionMode}`), currentModel || tr("web.model")].filter(Boolean).join(" · ");
  const policyControls = Boolean(onPolicy || onModel);
  return <form className="composer" onSubmit={(event) => { send(event).catch((reason) => updateDraft(sessionId, (item) => ({ ...item, phase: "unknown", reason: String(reason) }))); }}>
    <input ref={imageInput} className="file-input" type="file" accept="image/png,image/jpeg,image/webp" multiple tabIndex={-1} aria-hidden="true" onChange={(event) => { if (event.target.files) upload(event.target.files, true); event.target.value = ""; }} />
    <input ref={fileInput} className="file-input" type="file" multiple tabIndex={-1} aria-hidden="true" onChange={(event) => { if (event.target.files) upload(event.target.files, false); event.target.value = ""; }} />
    {chooseEnvironment && <EnvironmentChoice sessionId={sessionId} draft={draft} updateDraft={updateDraft} git={git} workLabel={workLabel} />}
    {(draft.attachments.length > 0 || (draft.documents ?? []).length > 0 || (draft.references ?? []).length > 0 || (draft.localFiles ?? []).length > 0 || draft.command) && <div className="chips">
      {draft.attachments.map((attachment) => <span className="chip" key={attachment.id}>{attachment.filename}<button type="button" aria-label={tr("web.remove", { name: attachment.filename })} onClick={() => {
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
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
    }} />
    {menu && <div className="mention-menu" role="listbox" aria-busy={menuLoading}>
      {menuError && <p className="form-error" role="alert">{menuError}</p>}
      {!menuLoading && !results.length && !menuError && <p className="empty-small">{tr("web.no_matches")}</p>}
      {results.map((item, index) => <button type="button" role="option" aria-selected={index === menu.index} key={item.id} onMouseDown={(event) => { event.preventDefault(); applyMenu(item); }}>{item.label}</button>)}
    </div>}
    <div className="composer-actions">
      <div className="row-actions">
        <button type="button" className="icon-button" title={tr("web.attach_file")} aria-label={tr("web.attach_file")} onClick={() => fileInput.current?.click()}><Paperclip size={14} /></button>
        <button type="button" className="icon-button" title={tr("web.attach")} aria-label={tr("web.attach")} onClick={() => imageInput.current?.click()}><Image size={14} /></button>
        <button type="button" className="icon-button" title={tr("web.reference_file")} onClick={() => { updateDraft(sessionId, (item) => ({ ...item, prompt: `${item.prompt}@` })); setMenu({ kind: "file", query: "", index: 0 }); box.current?.focus(); }}>@</button>
        <button type="button" className="icon-button" title={tr("web.commands_title")} onClick={() => { updateDraft(sessionId, (item) => ({ ...item, prompt: `${item.prompt}/` })); setMenu({ kind: "command", query: "", index: 0 }); box.current?.focus(); }}>/</button>
        <span className="hint" role={draft.phase === "rejected" || draft.phase === "unknown" ? "alert" : "status"}>{draft.reason || (uploading ? tr("web.uploading") : "") || (draft.phase === "awaiting" ? tr("web.awaiting_accept") : "") || (offline ? tr("web.offline_hint") : "")}</span>
      </div>
      <div className="row-actions composer-primary-actions">
        {policyControls && (
          <button
            type="button"
            className="text-button composer-settings-toggle"
            aria-expanded={settingsOpen}
            aria-controls={`composer-settings-${sessionId}`}
            title={tr("web.composer_settings")}
            onClick={() => setSettingsOpen((open) => !open)}
          >
            {settingsSummary}
          </button>
        )}
        {running && <button type="button" className="button stop" onClick={() => { cancel().catch(() => undefined); }}><Square size={11} weight="fill" />{tr("web.stop")}<kbd>{tr("web.shortcut_key", { key: "." })}</kbd></button>}
        {running && cancelAll && <button type="button" className="button stop" onClick={() => { cancelAll().catch(() => undefined); }}>{tr("web.stop_all")}</button>}
        {(!running || draft.prompt.trim()) && <button className="button primary" disabled={uploading || draft.phase === "awaiting" || (!draft.prompt.trim() && !draft.command && !draft.attachments.length && !(draft.documents ?? []).length && !(draft.references ?? []).length && !(draft.localFiles ?? []).length)}><PaperPlaneRight size={13} />{draft.phase === "unknown" ? tr("web.retry") : tr("web.send")}</button>}
      </div>
    </div>
    {policyControls && (
      <div className={`composer-advanced ${settingsOpen ? "open" : ""}`} id={`composer-settings-${sessionId}`}>
        <select className="select select-mode" aria-label={tr("web.interaction_mode")} title={tr(`web.mode_help.${interactionMode}`)} value={interactionMode} onChange={(event) => onPolicy?.({ interaction_mode: event.target.value }).catch((error) => updateDraft(sessionId, (item) => ({ ...item, reason: String(error) })))}>
          <option value="agent" title={tr("web.mode_help.agent")}>{tr("web.mode.agent")}</option>
          <option value="plan" title={tr("web.mode_help.plan")}>{tr("web.mode.plan")}</option>
          <option value="ask" title={tr("web.mode_help.ask")}>{tr("web.mode.ask")}</option>
        </select>
        <select className="select select-model" aria-label={tr("web.model")} value={currentModel} onChange={(event) => onModel?.(event.target.value)}>
          {visibleModels(models, currentModel).map((model) => <option key={model} value={model}>{model}</option>)}
        </select>
        {executionMode === "host" && <span className="hint" title={tr("web.execution.host_help")}>{tr("web.execution.host")}</span>}
        <select className="select select-permissions" aria-label={tr("web.permission_mode")}
          title={interactionMode === "agent" ? tr(permissionMode === "bypass" && executionMode === "host" ? "web.execution.host_bypass" : `web.perm_help.${permissionMode || "default"}`) : tr("web.perm_locked", { mode: tr(`web.mode.${interactionMode}`) })}
          disabled={interactionMode !== "agent"}
          value={permissionMode || "default"} onChange={(event) => onPolicy?.({ permission_mode: event.target.value }).catch((error) => updateDraft(sessionId, (item) => ({ ...item, reason: String(error) })))}>
          <option value="default" title={tr("web.perm_help.default")}>{tr("web.perm.default")}</option>
          <option value="acceptEdits" title={tr("web.perm_help.acceptEdits")}>{tr("web.perm.acceptEdits")}</option>
          <option value="bypass" title={tr(executionMode === "host" ? "web.execution.host_bypass" : "web.perm_help.bypass")}>{tr("web.perm.bypass")}</option>
          {permissionMode === "plan" && <option value="plan" title={tr("web.perm_help.plan")}>{tr("web.perm.plan")}</option>}
        </select>
      </div>
    )}
  </form>;
}
