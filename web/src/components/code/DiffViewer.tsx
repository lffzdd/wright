import { ChatCircle, Check, Copy } from "@phosphor-icons/react";
import { memo, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { languageForPath, useHighlight } from "./highlight";
import { useT } from "../../i18n";
import { parseDiff } from "./diff";

export type DiffFeedbackLine = { oldLine: number | null; newLine: number | null; content: string; side: "old" | "new" };

export const DiffViewer = memo(function DiffViewer({
  patch,
  filename,
  onLineFeedback,
  renderHunkActions,
  embedded = false,
}: {
  patch: string;
  filename?: string;
  onLineFeedback?: (line: DiffFeedbackLine) => void;
  renderHunkActions?: (hunk: number) => ReactNode;
  /** Inline under a Changes row: no card, filename, copy, or git meta. */
  embedded?: boolean;
}) {
  const tr = useT();
  const [copied, setCopied] = useState(false);

  const { lines, additions, deletions } = useMemo(() => parseDiff(patch), [patch]);
  const hunkIndices = useMemo(() => {
    let hunk = -1;
    return lines.map((line) => line.type === "hunk" ? ++hunk : hunk);
  }, [lines]);
  const columns = onLineFeedback ? 5 : 4;
  // Code lines are colored as one block in the file's language; hunk and meta lines are left out.
  const codeIndex = useMemo(() => {
    let next = 0;
    return lines.map((line) => line.type === "hunk" || line.type === "meta" ? -1 : next++);
  }, [lines]);
  const code = useMemo(() => lines.filter((_, idx) => codeIndex[idx] >= 0).map((line) => line.content).join("\n"), [lines, codeIndex]);
  const tokens = useHighlight(code, languageForPath(filename));

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(patch);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Ignore clipboard error
    }
  };

  if (!patch) {
    return <p className="empty-small">{tr("web.no_diff")}</p>;
  }

  return (
    <div className={`diff-viewer${embedded ? " embedded" : ""}`}>
      {!embedded && <div className="diff-header">
        <div className="diff-title-info">
          {filename && <span className="diff-filename">{filename}</span>}
          <div className="diff-stats">
            {additions > 0 && <span className="stat-add">+{additions}</span>}
            {deletions > 0 && <span className="stat-del">-{deletions}</span>}
          </div>
        </div>
        <button
          type="button"
          className="copy-button"
          onClick={handleCopy}
          title={tr("web.copy_patch_title")}
          aria-label={tr("web.copy_patch_title")}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
          <span>{copied ? tr("web.copied") : tr("web.copy_patch")}</span>
        </button>
      </div>}
      <div className="diff-table-wrap">
        <table className="diff-table">
          <tbody>
            {lines.map((line, idx) => {
              if (line.type === "hunk") {
                return (
                  <tr key={idx} className="diff-row diff-row-hunk hunk">
                    <td colSpan={columns} className="diff-cell-hunk">
                      <div className="diff-hunk-heading"><span>{line.content}</span>{renderHunkActions && <div className="diff-hunk-actions">{renderHunkActions(hunkIndices[idx])}</div>}</div>
                    </td>
                  </tr>
                );
              }
              if (line.type === "meta") {
                if (embedded) return null;
                return (
                  <tr key={idx} className="diff-row diff-row-meta">
                    <td colSpan={columns} className="diff-cell-meta">
                      {line.content}
                    </td>
                  </tr>
                );
              }
              const marker = line.type === "add" ? "+" : line.type === "del" ? "-" : " ";
              const kind = line.type === "add" ? "add" : line.type === "del" ? "del" : "";
              return (
                <tr key={idx} className={`diff-row diff-row-${line.type}${kind ? ` ${kind}` : ""}`}>
                  <td className="diff-gutter diff-gutter-old ln">{line.oldNum ?? ""}</td>
                  <td className="diff-gutter diff-gutter-new ln">{line.newNum ?? ""}</td>
                  <td className="diff-marker sign">{marker}</td>
                  <td className="diff-code">
                    <pre>{tokens?.[codeIndex[idx]]?.length
                      ? tokens[codeIndex[idx]].map((token, at) => <span key={at} className="cv-tok" style={token.style as CSSProperties}>{token.content}</span>)
                      : line.content || " "}</pre>
                  </td>
                  {onLineFeedback && <td className="diff-feedback-cell"><button type="button" className="diff-line-feedback icon-button" aria-label={tr("web.changes.line_feedback", { side: tr(line.type === "del" ? "web.changes.old" : "web.changes.new"), line: line.newNum ?? line.oldNum ?? "" })} title={tr("web.changes.feedback")} onClick={() => onLineFeedback({ oldLine: line.oldNum, newLine: line.newNum, content: line.content, side: line.type === "del" ? "old" : "new" })}><ChatCircle size={13} /></button></td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
});
