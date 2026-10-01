import { Check, Copy } from "@phosphor-icons/react";
import { memo, useMemo, useState } from "react";
import { useT } from "./i18n";
import { parseDiff } from "./diff";

export const DiffViewer = memo(function DiffViewer({
  patch,
  filename,
}: {
  patch: string;
  filename?: string;
}) {
  const tr = useT();
  const [copied, setCopied] = useState(false);

  const { lines, additions, deletions } = useMemo(() => parseDiff(patch), [patch]);

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
    <div className="diff-viewer">
      <div className="diff-header">
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
      </div>
      <div className="diff-table-wrap">
        <table className="diff-table">
          <tbody>
            {lines.map((line, idx) => {
              if (line.type === "hunk") {
                return (
                  <tr key={idx} className="diff-row diff-row-hunk">
                    <td colSpan={4} className="diff-cell-hunk">
                      {line.content}
                    </td>
                  </tr>
                );
              }
              if (line.type === "meta") {
                return (
                  <tr key={idx} className="diff-row diff-row-meta">
                    <td colSpan={4} className="diff-cell-meta">
                      {line.content}
                    </td>
                  </tr>
                );
              }
              const marker = line.type === "add" ? "+" : line.type === "del" ? "-" : " ";
              return (
                <tr key={idx} className={`diff-row diff-row-${line.type}`}>
                  <td className="diff-gutter diff-gutter-old">{line.oldNum ?? ""}</td>
                  <td className="diff-gutter diff-gutter-new">{line.newNum ?? ""}</td>
                  <td className="diff-marker">{marker}</td>
                  <td className="diff-code">
                    <pre>{line.content || " "}</pre>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
});
