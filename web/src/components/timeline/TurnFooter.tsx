import { CaretRight, File, FilePlus, Sparkle, Warning, X } from "@phosphor-icons/react";
import { Fragment, type ReactNode } from "react";
import { ReplyActions } from "../cards/Messages";
import { useT } from "../../i18n";
import { formatElapsed, formatTokens } from "../composer/RunStatus";
import type { TurnSummary } from "./turnSummary";

/**
 * Reference-aligned turn close: file summary card, follow-up chips, then one row
 * with the reply's actions on the left and the turn's stats on the right.
 * Fields the server does not send are omitted.
 */
export function TurnFooter({ summary, latest, reply, onViewChanges, onSuggest, onRegenerate, onFork, onShowTools }: {
  summary: TurnSummary;
  /** The newest turn keeps its actions visible; older ones show them on hover or focus. */
  latest?: boolean;
  /** The turn's last reply; its copy, fork and regenerate join the stats row. */
  reply?: string;
  onViewChanges?: () => void;
  onSuggest?: (text: string) => void;
  onRegenerate?: () => void;
  onFork?: () => void;
  /** Scrolls to the turn's tool calls. */
  onShowTools?: () => void;
}) {
  return <div className={`turn-footer-block${latest ? " latest" : ""}`}>
    <FileSummary summary={summary} onViewChanges={onViewChanges} />
    {Boolean(summary.suggestions?.length) && onSuggest && <div className="followups">
      {summary.suggestions!.slice(0, 4).map((text) => <button type="button" key={text} className="fu" onClick={() => onSuggest(text)}>
        <Sparkle className="ic" size={13} aria-hidden="true" />{text}
      </button>)}
    </div>}
    <div className="turn-foot">
      {reply !== undefined && <span className="tf-actions">
        <ReplyActions text={reply} onFork={onFork} onRegenerate={onRegenerate} />
      </span>}
      <TurnStats summary={summary} onShowTools={onShowTools} />
    </div>
  </div>;
}

/** Elapsed, tokens, cost and tool calls; the outcome only when the turn did not finish normally. */
function TurnStats({ summary, onShowTools }: { summary: TurnSummary; onShowTools?: () => void }) {
  const tr = useT();
  const parts: ReactNode[] = [];
  if (summary.ended !== "done") {
    const cancelled = summary.ended === "cancelled";
    parts.push(<span key="status" className={`tf-status ${cancelled ? "warn" : "danger"}`}>
      {cancelled ? <X size={13} weight="bold" aria-hidden="true" /> : <Warning size={13} weight="bold" aria-hidden="true" />}
      {tr(cancelled ? "web.turn_cancelled" : "web.turn_failed")}
    </span>);
  }
  if (typeof summary.elapsedMs === "number") parts.push(formatElapsed(summary.elapsedMs));
  if (typeof summary.tokens === "number") parts.push(tr("web.turn.tokens", { count: formatTokens(summary.tokens) }));
  if (typeof summary.costUsd === "number") parts.push(tr("web.turn.cost", { amount: summary.costUsd.toFixed(2) }));
  if (summary.toolCalls) {
    const label = tr(summary.toolCalls === 1 ? "web.turn.tool_calls.one" : "web.turn.tool_calls", { count: summary.toolCalls });
    parts.push(onShowTools
      ? <button key="tools" type="button" className="text-button tf-tools" title={tr("web.turn.show_tool_calls")} onClick={onShowTools}>
        {label}
      </button>
      : label);
  }
  if (!parts.length) return null;
  return <span className="tf-stats">
    {parts.map((part, index) => <Fragment key={typeof part === "string" ? part : index}>
      {index > 0 && <span className="sep" aria-hidden="true">·</span>}
      {typeof part === "string" ? <span>{part}</span> : part}
    </Fragment>)}
  </span>;
}

function FileSummary({ summary, onViewChanges }: { summary: TurnSummary; onViewChanges?: () => void }) {
  const tr = useT();
  const fileRows = summary.fileChanges?.length
    ? summary.fileChanges
    : summary.files > 0
      ? [{ path: tr("web.turn.files_changed", { count: summary.files }), additions: summary.additions, deletions: summary.deletions }]
      : [];
  if (!fileRows.length) return null;
  const heading = tr(summary.files === 1 ? "web.turn.files_changed.one" : "web.turn.files_changed", { count: summary.files });
  return <div className="sum">
    <div className="sum-head">
      <File className="ic" size={14} aria-hidden="true" />
      {heading}
      {Boolean(summary.fileChanges?.length) && <span className="nums mono">
        <span className="add-n">+{summary.additions}</span> <span className="del-n">−{summary.deletions}</span>
      </span>}
      <span className="spacer" />
      {onViewChanges && <button type="button" className="btn ghost sm" onClick={onViewChanges}>
        {tr("web.turn.view_changes")}<CaretRight size={13} aria-hidden="true" />
      </button>}
    </div>
    {summary.fileChanges?.map((file) => <button
      type="button" key={file.path} className="sum-row" onClick={onViewChanges} disabled={!onViewChanges}
    >
      {file.isNew ? <FilePlus className="ic" size={14} aria-hidden="true" /> : <File className="ic" size={14} aria-hidden="true" />}
      <span>{file.path}</span>
      {file.isNew && <span className="tag-new">{tr("web.file_new")}</span>}
      <span className="fs mono"><span className="add-n">+{file.additions}</span> <span className="del-n">−{file.deletions}</span></span>
    </button>)}
  </div>;
}
