import { Check, Copy } from "@phosphor-icons/react";
import { memo, useMemo, useState } from "react";

type DiffLine = {
  type: "meta" | "hunk" | "add" | "del" | "context";
  oldNum: number | null;
  newNum: number | null;
  content: string;
};

export const DiffViewer = memo(function DiffViewer({
  patch,
  filename,
}: {
  patch: string;
  filename?: string;
}) {
  const [copied, setCopied] = useState(false);

  const { lines, additions, deletions } = useMemo(() => {
    if (!patch) return { lines: [], additions: 0, deletions: 0 };
    const rawLines = patch.split("\n");
    let oldLine: number | null = null;
    let newLine: number | null = null;
    let additions = 0;
    let deletions = 0;
    const lines: DiffLine[] = [];

    for (const line of rawLines) {
      const hunkMatch = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (hunkMatch) {
        oldLine = parseInt(hunkMatch[1], 10);
        newLine = parseInt(hunkMatch[2], 10);
        lines.push({ type: "hunk", oldNum: null, newNum: null, content: line });
      } else if (
        line.startsWith("+++") ||
        line.startsWith("---") ||
        line.startsWith("diff ") ||
        line.startsWith("index ")
      ) {
        lines.push({ type: "meta", oldNum: null, newNum: null, content: line });
      } else if (line.startsWith("+")) {
        additions++;
        lines.push({ type: "add", oldNum: null, newNum: newLine, content: line.slice(1) });
        if (newLine !== null) newLine++;
      } else if (line.startsWith("-")) {
        deletions++;
        lines.push({ type: "del", oldNum: oldLine, newNum: null, content: line.slice(1) });
        if (oldLine !== null) oldLine++;
      } else {
        const content = line.startsWith(" ") ? line.slice(1) : line;
        lines.push({ type: "context", oldNum: oldLine, newNum: newLine, content });
        if (oldLine !== null) oldLine++;
        if (newLine !== null) newLine++;
      }
    }
    return { lines, additions, deletions };
  }, [patch]);

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
    return <p className="empty-small">No textual diff.</p>;
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
          title="Copy raw patch"
          aria-label="Copy raw patch"
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
          <span>{copied ? "Copied" : "Copy patch"}</span>
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
