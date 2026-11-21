import { Lightbulb } from "@phosphor-icons/react";
import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { MarkdownContent } from "../code/Markdown";
import { useT } from "../../i18n";
import { CheckIcon, CopyIcon, EditIcon, ForkIcon, RegenerateIcon } from "../ui/chromeIcons";
import { RevealCallContext } from "../timeline/revealCall";
import { formatDuration } from "./values";

export function UserMessage({ text, userLabel, timestamp, tocId, enter, interjection, onEdit }: { text: string; userLabel?: string; timestamp?: string; tocId?: string; enter?: boolean; interjection?: boolean; onEdit?: () => void }) {
  const tr = useT();
  const parts = text.split(/(@(?:[\w.-]+\/)*[\w.-]+)/g);
  return <div className={`msg-row user-msg${enter ? " enter" : ""}`} data-toc-id={tocId}>
    <div className="msg-actions">
      {onEdit && <MessageActionButton label={tr("web.edit_resend")} onClick={onEdit}><EditIcon size={15} /></MessageActionButton>}
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

/**
 * A reply. `actionsInFooter` leaves its actions to the turn footer, which puts
 * them on one row with the turn's stats.
 */
export function AssistantMessage({ text, enter, onRegenerate, onFork, streaming, contextId, actionsInFooter }: {
  text: string; enter?: boolean; streaming?: boolean; contextId?: string;
  onRegenerate?: () => void; onFork?: () => void; actionsInFooter?: boolean;
}) {
  return <div className={`msg-row assistant-msg${enter ? " enter" : ""}${streaming ? " streaming" : ""}`} data-context-id={contextId}>
    <div className="msg-card">
      <div className="msg-body"><MarkdownContent content={text} /></div>
      {!actionsInFooter && <div className="msg-actions">
        <ReplyActions text={text} onRegenerate={onRegenerate} onFork={onFork} />
      </div>}
    </div>
  </div>;
}

/** Copy, fork and regenerate for one reply, in that order. */
export function ReplyActions({ text, onRegenerate, onFork }: {
  text: string; onRegenerate?: () => void; onFork?: () => void;
}) {
  const tr = useT();
  return <>
    <MessageCopyButton text={text} label={tr("web.copy_reply")} />
    {onFork && <MessageActionButton label={tr("web.fork_here")} onClick={onFork}><ForkIcon size={15} /></MessageActionButton>}
    {onRegenerate && <MessageActionButton label={tr("web.regenerate")} onClick={onRegenerate}>
      <RegenerateIcon size={15} />
    </MessageActionButton>}
  </>;
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
    {copied ? <CheckIcon size={15} /> : <CopyIcon size={15} />}
  </button>;
}

/** The folded line shows one line of text; the rest is clipped by CSS anyway. */
const PREVIEW_CHARS = 240;

/**
 * The model's reasoning as one line in the tool icon column: a bulb and the
 * opening words, folded once done. While it streams, the last few lines
 * scroll by under the label. How loud it reads is the `thought_style` preference.
 */
export function ReasoningRow({ text, durationMs, enter, live, contextId }: { text: string; durationMs?: number | null; enter?: boolean; live?: boolean; contextId?: string }) {
  const tr = useT();
  const running = Boolean(live) && typeof durationMs !== "number";
  const defaultOpen = running;
  const [toggled, setToggled] = useState<boolean | null>(null);
  const reveal = useContext(RevealCallContext);
  // Shown from the context panel: open it, so the flash lands on the text.
  useEffect(() => {
    if (reveal && contextId && reveal.contextId === contextId) setToggled(true);
  }, [reveal?.contextId, reveal?.request, contextId]);
  const open = toggled ?? defaultOpen;
  const durationLabel = typeof durationMs === "number" ? formatDuration(durationMs) : "";
  const preview = text.replace(/\s+/g, " ").trim();
  // Folded, only the opening words are on the page; the full text renders once opened.
  return <div className={`tl-indent${enter ? " enter" : ""}`} data-context-id={contextId}>
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
        <span className="think-ic" aria-hidden="true"><Lightbulb size={14} /></span>
        {running || open || !preview
          ? <span className="lbl">{tr(running ? "web.run.thinking" : "web.reasoning_tag")}</span>
          : <span className="think-preview">{preview.slice(0, PREVIEW_CHARS)}</span>}
        {durationLabel && <span className="think-meta">{durationLabel}</span>}
      </summary>
      {open && preview ? <div className="think-body"><ReasoningBody text={text} tail={running && toggled === null} /></div> : null}
    </details>
  </div>;
}

/**
 * Scrollable reasoning text; the bottom fades only while more text is below.
 * `tail` keeps a short window pinned to the newest lines while it streams.
 */
function ReasoningBody({ text, tail }: { text: string; tail?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [fade, setFade] = useState(false);
  const update = () => {
    const el = ref.current;
    if (el) setFade(el.scrollHeight - el.clientHeight - el.scrollTop > 2);
  };
  useEffect(() => {
    const el = ref.current;
    if (tail && el) el.scrollTop = el.scrollHeight;
  }, [text, tail]);
  useEffect(update, [text]);
  const className = `reasoning-body${tail ? " tail" : ""}${fade && !tail ? " fade-end" : ""}`;
  return <div ref={ref} className={className} onScroll={update}>{text}</div>;
}
