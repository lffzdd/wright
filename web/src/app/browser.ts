import type { ViewState } from "../types";

/**
 * "(n) Wright" while n sessions wait on an approval or answer. The open
 * session's own stream is fresher than the list, so it decides for itself.
 */
export function tabTitle(sessions: Array<{ session_id: string; pending_interactions?: number }>, open: ViewState | null): string {
  const waiting = new Set(sessions.filter((item) => item.pending_interactions).map((item) => item.session_id));
  if (open?.pending_interactions.length) waiting.add(open.session.session_id);
  else if (open) waiting.delete(open.session.session_id);
  return waiting.size ? `(${waiting.size}) Wright` : "Wright";
}

/** Saves text as a Markdown file through the browser's download. */
export function downloadText(fileName: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
