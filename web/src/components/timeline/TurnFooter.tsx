import { ArrowsClockwise, CaretRight, Check, Copy, File, FilePlus, Sparkle, Warning, X } from "@phosphor-icons/react";
import { Fragment } from "react";
import { useT } from "../../i18n";
import { formatElapsed, formatTokens } from "../composer/RunStatus";
import type { TurnSummary } from "./turnSummary";

/**
 * Reference-aligned turn close: file summary card, follow-up chips, and foot stats.
 * Fields the server does not send are omitted.
 */
export function TurnFooter({ summary, onViewChanges, onSuggest, onCopy, onRegenerate }: {
  summary: TurnSummary;
  onViewChanges?: () => void;
  onSuggest?: (text: string) => void;
  onCopy?: () => void;
  onRegenerate?: () => void;
}) {
  const tr = useT();
  const fileRows = summary.fileChanges?.length
    ? summary.fileChanges
    : summary.files > 0
      ? [{ path: tr("web.turn.files_changed", { count: summary.files }), additions: summary.additions, deletions: summary.deletions }]
      : [];
  const StatusIcon = summary.ended === "done" ? Check : summary.ended === "cancelled" ? X : Warning;
  const statusClass = summary.ended === "done" ? "ok" : summary.ended === "cancelled" ? "warn" : "warn";
  const statusLabel = summary.ended === "done" ? tr("web.turn.done") : summary.ended === "cancelled" ? tr("web.turn_cancelled") : tr("web.turn_failed");
  const stats: string[] = [];
  if (typeof summary.elapsedMs === "number") stats.push(formatElapsed(summary.elapsedMs));
  if (typeof summary.tokens === "number") stats.push(tr("web.turn.tokens", { count: formatTokens(summary.tokens) }));
  if (typeof summary.costUsd === "number") stats.push(tr("web.turn.cost", { amount: summary.costUsd.toFixed(2) }));
  if (summary.toolCalls) stats.push(tr(summary.toolCalls === 1 ? "web.turn.tool_calls.one" : "web.turn.tool_calls", { count: summary.toolCalls }));
  return <div className="turn-footer-block">
    {fileRows.length > 0 && <div className="sum">
      <div className="sum-head">
        {summary.fileChanges?.length ? <>
          <File className="ic" size={14} aria-hidden="true" />
          {tr(summary.files === 1 ? "web.turn.files_changed.one" : "web.turn.files_changed", { count: summary.files })}
          <span className="nums mono"><span className="add-n">+{summary.additions}</span> <span className="del-n">−{summary.deletions}</span></span>
        </> : <>
          <File className="ic" size={14} aria-hidden="true" />
          {tr(summary.files === 1 ? "web.turn.files_changed.one" : "web.turn.files_changed", { count: summary.files })}
        </>}
        <span className="spacer" />
        {onViewChanges && <button type="button" className="btn ghost sm" onClick={onViewChanges}>
          {tr("web.turn.view_changes")}<CaretRight size={13} aria-hidden="true" />
        </button>}
      </div>
      {summary.fileChanges?.map((file) => <button type="button" key={file.path} className="sum-row" onClick={onViewChanges} disabled={!onViewChanges}>
        {file.isNew ? <FilePlus className="ic" size={14} aria-hidden="true" /> : <File className="ic" size={14} aria-hidden="true" />}
        <span>{file.path}</span>
        {file.isNew && <span className="tag-new">{tr("web.file_new")}</span>}
        <span className="fs mono"><span className="add-n">+{file.additions}</span> <span className="del-n">−{file.deletions}</span></span>
      </button>)}
    </div>}
    {Boolean(summary.suggestions?.length) && onSuggest && <div className="followups">
      {summary.suggestions!.slice(0, 4).map((text) => <button type="button" key={text} className="fu" onClick={() => onSuggest(text)}>
        <Sparkle className="ic" size={13} aria-hidden="true" />{text}
      </button>)}
    </div>}
    <div className="turn-foot">
      <span className={statusClass}><StatusIcon size={13} weight="bold" aria-hidden="true" /></span>
      <span>{statusLabel}</span>
      {stats.map((part) => <Fragment key={part}><span className="sep">·</span><span>{part}</span></Fragment>)}
      {(onCopy || onRegenerate) && <span className="tf-actions">
        {onCopy && <button type="button" className="icon-button icon-btn" aria-label={tr("web.copy_reply")} title={tr("web.copy_reply")} onClick={onCopy}><Copy size={14} /></button>}
        {onRegenerate && <button type="button" className="icon-button icon-btn" aria-label={tr("web.regenerate")} title={tr("web.regenerate")} onClick={onRegenerate}><ArrowsClockwise size={14} /></button>}
      </span>}
    </div>
  </div>;
}
