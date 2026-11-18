import type { TimelineItem } from "../../types";
import { toolCategory } from "../cards/cards";

export type TurnFileChange = { path: string; additions: number; deletions: number; isNew?: boolean };

export type TurnSummary = {
  toolCalls: number; files: number; additions: number; deletions: number; ended: "done" | "failed" | "cancelled";
  fileChanges?: TurnFileChange[];
  /** Present only when the server reports turn totals. */
  elapsedMs?: number; tokens?: number; costUsd?: number; suggestions?: string[];
};

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? value as Record<string, unknown> : null;
}

function editCounts(item: TimelineItem): { additions: number; deletions: number } {
  const result = record(item.result);
  for (const source of [record(result?.display), record(result?.data), result]) {
    if (typeof source?.additions === "number" || typeof source?.deletions === "number") {
      return { additions: Number(source?.additions ?? 0), deletions: Number(source?.deletions ?? 0) };
    }
  }
  return { additions: 0, deletions: 0 };
}

function editPath(item: TimelineItem): string {
  const args = item.arguments ?? {};
  for (const key of ["path", "file", "file_path", "target"]) {
    if (typeof args[key] === "string" && args[key]) return args[key] as string;
  }
  return item.call_id || item.id;
}

/**
 * What a finished turn did: tool calls made and files its successful edits
 * touched, keyed by turn id. Counts come only from the recorded steps, so a
 * turn with none still gets a summary line saying it is done.
 */
/**
 * The turn each item belongs to. Live items carry a turn id; history loaded
 * from disk does not, so a turn there runs from one prompt to the next.
 */
export function turnKeys(items: TimelineItem[]): string[] {
  let current = "";
  return items.map((item) => {
    if (item.kind === "text" && item.role === "user" && !item.interjection) current = item.turn_id || `after:${item.id}`;
    return item.turn_id || current;
  });
}

export function summarizeTurns(items: TimelineItem[], keys = turnKeys(items)): Map<string, TurnSummary> {
  const turns = new Map<string, TurnSummary & { paths: Set<string>; byPath: Map<string, TurnFileChange> }>();
  items.forEach((item, index) => {
    const key = keys[index];
    if (!key) return;
    let turn = turns.get(key);
    if (!turn) {
      turn = { toolCalls: 0, files: 0, additions: 0, deletions: 0, ended: "done", paths: new Set(), byPath: new Map() };
      turns.set(key, turn);
    }
    if (typeof item.turn_elapsed_ms === "number") turn.elapsedMs = item.turn_elapsed_ms;
    if (item.turn_usage) {
      const usage = item.turn_usage;
      const tokens = (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
      if (tokens > 0) turn.tokens = tokens;
      if (typeof usage.cost_usd === "number") turn.costUsd = usage.cost_usd;
    }
    if (item.suggestions?.length) turn.suggestions = item.suggestions;
    if (item.kind === "status" && (item.status === "failed" || item.status === "interrupted")) turn.ended = "failed";
    if (item.kind === "status" && item.status === "cancelled") turn.ended = "cancelled";
    if (item.kind !== "tool" && item.kind !== "shell" && item.kind !== "edit") return;
    turn.toolCalls += 1;
    if (toolCategory(item.name || item.kind, item.kind) !== "edits" || item.phase !== "succeeded") return;
    const path = editPath(item);
    turn.paths.add(path);
    const counts = editCounts(item);
    turn.additions += counts.additions;
    turn.deletions += counts.deletions;
    const display = record(item.result?.display ?? item.result?.data);
    const existing = turn.byPath.get(path) ?? { path, additions: 0, deletions: 0 };
    const isNew = existing.isNew || display?.is_new === true || display?.created === true;
    turn.byPath.set(path, {
      path,
      additions: existing.additions + counts.additions,
      deletions: existing.deletions + counts.deletions,
      ...(isNew ? { isNew: true } : {}),
    });
  });
  const out = new Map<string, TurnSummary>();
  for (const [id, { paths, byPath, ...turn }] of turns) {
    const fileChanges = [...byPath.values()].sort((a, b) => a.path.localeCompare(b.path));
    out.set(id, { ...turn, files: paths.size, ...(fileChanges.length ? { fileChanges } : {}) });
  }
  return out;
}
