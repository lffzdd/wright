import { ArrowsClockwise, ArrowsSplit, CaretRight, Check, Copy, PencilSimple } from "@phosphor-icons/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { MarkdownContent } from "../code/Markdown";
import { useT } from "../../i18n";
import { formatDuration } from "./values";

export function UserMessage({ text, userLabel, timestamp, tocId, enter, interjection, onEdit }: { text: string; userLabel?: string; timestamp?: string; tocId?: string; enter?: boolean; interjection?: boolean; onEdit?: () => void }) {
  const tr = useT();
  const parts = text.split(/(@(?:[\w.-]+\/)*[\w.-]+)/g);
  return <div className={`msg-row user-msg${enter ? " enter" : ""}`} data-toc-id={tocId}>
    <div className="msg-actions">
      {onEdit && <MessageActionButton label={tr("web.edit_resend")} onClick={onEdit}><PencilSimple size={14} /></MessageActionButton>}
      <MessageCopyButton text={text} label={tr("web.copy_message")} />
    </div>
    <div className="msg-card">
      <div className="msg-card-head">
        <span><strong>{userLabel || tr("web.you")}</strong> <span className="muted">{tr(interjection ? "web.user_interjection" : "web.user_instruction")}</span></span>
        {timestamp && <span className="mono muted u-meta">{timestamp}</span>}
      </div>
      <p className="msg-body">{parts.map((part, index) => part.startsWith("@") ? <span className="prompt-reference" key={`${part}-${index}`}>{part}</span> : part)}</p>
    </div>
  </div>;
}

export function AssistantMessage({ text, enter, onRegenerate, onFork, streaming }: { text: string; enter?: boolean; onRegenerate?: () => void; onFork?: () => void; streaming?: boolean }) {
  const tr = useT();
  return <div className={`msg-row assistant-msg${enter ? " enter" : ""}${streaming ? " streaming" : ""}`}>
    <div className="msg-card">
      <div className="msg-body"><MarkdownContent content={text} /></div>
      <div className="msg-actions">
        <MessageCopyButton text={text} label={tr("web.copy_reply")} />
        {onRegenerate && <MessageActionButton label={tr("web.regenerate")} onClick={onRegenerate}><ArrowsClockwise size={14} /></MessageActionButton>}
        {onFork && <MessageActionButton label={tr("web.fork_here")} onClick={onFork}><ArrowsSplit size={14} /></MessageActionButton>}
      </div>
    </div>
  </div>;
}

function MessageActionButton({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return <button type="button" className="icon-button msg-action" onClick={onClick} title={label} aria-label={label}>{children}</button>;
}

/** Copies a message's raw text; the icon turns into a check for a moment. */
function MessageCopyButton({ text, label }: { text: string; label: string }) {
  const tr = useT();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return;
    }
    setCopied(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 2000);
  };
  const title = copied ? tr("web.copied") : label;
  return <button type="button" className="icon-button msg-action" onClick={copy} title={title} aria-label={title}>
    {copied ? <Check size={14} /> : <Copy size={14} />}
  </button>;
}

/** The model's reasoning as one quiet line; open while it streams, folded once done. */
export function ReasoningRow({ text, durationMs, enter, live }: { text: string; durationMs?: number | null; enter?: boolean; live?: boolean }) {
  const tr = useT();
  const running = Boolean(live) && typeof durationMs !== "number";
  const defaultOpen = running;
  const [toggled, setToggled] = useState<boolean | null>(null);
  const open = toggled ?? defaultOpen;
  const durationLabel = typeof durationMs === "number" ? formatDuration(durationMs) : "";
  const preview = text.replace(/\s+/g, " ").trim();
  const label = running ? tr("web.run.thinking") : durationLabel ? tr("web.thought_for", { duration: durationLabel }) : tr("web.steps.reasoning");
  return <div className={`tl-indent${enter ? " enter" : ""}`}>
    <details
      className={`think reasoning-card${running ? " running" : ""}${open ? " open" : ""}`}
      open={open}
    >
      <summary
        aria-label={open ? tr("web.reasoning_collapse") : tr("web.reasoning_expand")}
        onClick={(event) => {
          event.preventDefault();
          setToggled(!(toggled ?? defaultOpen));
        }}
      >
        <CaretRight size={12} className="chev" aria-hidden="true" />
        <span className="lbl">{label}</span>
      </summary>
      {preview ? <div className="think-body"><ReasoningBody text={text} /></div> : null}
    </details>
  </div>;
}

/** Scrollable reasoning text; the bottom fades only while more text is below. */
function ReasoningBody({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [fade, setFade] = useState(false);
  const update = () => {
    const el = ref.current;
    if (el) setFade(el.scrollHeight - el.clientHeight - el.scrollTop > 2);
  };
  useEffect(update, [text]);
  return <div ref={ref} className={`reasoning-body${fade ? " fade-end" : ""}`} onScroll={update}>{text}</div>;
}
