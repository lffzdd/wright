export type DiffLine = {
  type: "meta" | "hunk" | "add" | "del" | "context";
  oldNum: number | null;
  newNum: number | null;
  content: string;
};

export function parseDiff(patch: string): { lines: DiffLine[]; additions: number; deletions: number } {
  const rawLines = patch.split(/\r?\n/);
  if (rawLines.at(-1) === "") rawLines.pop();
  let oldLine: number | null = null;
  let newLine: number | null = null;
  let inHunk = false;
  let additions = 0;
  let deletions = 0;
  const lines: DiffLine[] = [];
  for (const line of rawLines) {
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) {
      oldLine = Number(hunk[1]); newLine = Number(hunk[2]); inHunk = true;
      lines.push({ type: "hunk", oldNum: null, newNum: null, content: line });
    } else if (line.startsWith("\\") || (!inHunk && /^(---|\+\+\+|diff |index )/.test(line))) {
      lines.push({ type: "meta", oldNum: null, newNum: null, content: line });
    } else if (line.startsWith("+")) {
      additions++;
      lines.push({ type: "add", oldNum: null, newNum: newLine, content: line.slice(1) });
      if (newLine !== null) newLine++;
    } else if (line.startsWith("-")) {
      deletions++;
      lines.push({ type: "del", oldNum: oldLine, newNum: null, content: line.slice(1) });
      if (oldLine !== null) oldLine++;
    } else if (line.startsWith(" ")) {
      lines.push({ type: "context", oldNum: oldLine, newNum: newLine, content: line.slice(1) });
      if (oldLine !== null) oldLine++;
      if (newLine !== null) newLine++;
    } else {
      lines.push({ type: "meta", oldNum: null, newNum: null, content: line });
    }
  }
  return { lines, additions, deletions };
}
