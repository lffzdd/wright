import { useEffect, useRef, useState } from "react";
import { useT } from "../../i18n";
import type { ToolState, ViewState } from "../../types";
import { toolCategory } from "../cards/toolKinds";

/** What the running turn is doing right now, as an i18n key and an optional target. */
export type RunActivity = { key: string; target?: string; waiting?: boolean };

function argText(args: Record<string, unknown> | undefined, keys: string[]): string {
  for (const key of keys) {
    const value = args?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function toolActivity(tool: ToolState): RunActivity {
  const args = tool.arguments;
  const path = argText(args, ["path", "file", "file_path", "target", "directory"]);
  switch (toolCategory(tool.name)) {
    case "edits": return { key: "web.run.editing", target: path };
    case "commands": return { key: "web.run.running_command", target: argText(args, ["command", "cmd"]) };
    case "reads": return { key: "web.run.reading", target: path };
    case "directories": return { key: "web.run.listing", target: path };
    case "searches": return { key: "web.run.searching", target: argText(args, ["pattern", "query", "regex"]) };
    case "web": return { key: "web.run.browsing", target: argText(args, ["url", "query"]) };
    case "memory": return { key: "web.run.recalling" };
    case "plans": return { key: "web.run.planning" };
    default: return { key: "web.run.using_tool", target: tool.name };
  }
}

/**
 * Picks the one thing worth saying about a running turn: a decision waiting on
 * the person first, then the newest tool still at work, then the stream.
 */
export function runActivity(state: ViewState): RunActivity | null {
  const turn = state.active_turn;
  if (!turn) return null;
  const pending = state.pending_interactions[0];
  if (pending) return { key: pending.kind === "ask_user" ? "web.run.waiting_answer" : "web.run.waiting_approval", waiting: true };
  const tool = [...turn.tools].reverse().find((item) => item.phase === "running" || item.phase === "awaiting_approval" || item.phase === "planned");
  if (tool) return toolActivity(tool);
  if (turn.content) return { key: "web.run.replying" };
  if (turn.reasoning) return { key: "web.run.thinking" };
  return { key: "web.working" };
}

export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

export function formatTokens(count: number): string {
  if (count < 1000) return String(count);
  if (count < 1_000_000) return `${(count / 1000).toFixed(count < 10_000 ? 1 : 0)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

/**
 * One line above the composer while a turn runs: what the agent is doing and
 * for how long. The clock starts when this view first sees the turn, since the
 * stream carries no start time; switching sessions starts it again.
 */
export function RunStatus({ state, onStop }: { state: ViewState; onStop?: () => void }) {
  const tr = useT();
  const turn = state.active_turn;
  const turnKey = turn ? turn.turn_id || turn.run_id || "active" : "";
  const started = useRef<{ key: string; at: number }>({ key: "", at: 0 });
  if (turnKey && started.current.key !== turnKey) started.current = { key: turnKey, at: Date.now() };
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!turnKey) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [turnKey]);
  const activity = runActivity(state);
  if (!activity) return null;
  const elapsed = formatElapsed(now - started.current.at);
  const tokens = state.usage?.request_total_tokens ?? state.usage?.total_tokens;
  const verb = tr(activity.key);
  return <div className={`run-status runbar${activity.waiting ? " waiting" : ""}`} role="status" aria-live="polite">
    <span className={activity.waiting ? "wdot" : "spin"} aria-hidden="true" />
    <span className="rb-act"><b>{verb}</b>{activity.target && <> <span className="mono" title={activity.target}>{activity.target}</span></>}</span>
    <span className="rb-time mono">{elapsed}</span>
    {typeof tokens === "number" && tokens > 0 && <span className="rb-tok">{formatTokens(tokens)}</span>}
    {onStop && <button type="button" className="rb-stop" onClick={onStop}>{tr("web.run.stop")} <kbd>{tr("web.shortcut_key", { key: "." })}</kbd></button>}
  </div>;
}
