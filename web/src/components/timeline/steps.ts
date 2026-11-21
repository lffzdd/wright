import type { ActiveTurn, TimelineItem, ToolState } from "../../types";
import { planRejection } from "../cards/PlanCard";
import { toolCategory, type StepCategory } from "../cards/toolKinds";

/** One process step of a turn: a reasoning segment or a tool call. */
export type Step = {
  key: string;
  kind: "reasoning" | "tool";
  name: string;
  toolKind?: string;
  phase?: string;
  durationMs?: number | null;
  turnId?: string;
  item?: TimelineItem;
  tool?: ToolState;
};

export type { StepCategory };

export type Segment =
  | { type: "item"; item: TimelineItem }
  | { type: "steps"; key: string; turnId?: string; steps: Step[]; standalone?: boolean };

export type StepSummary = {
  counts: Partial<Record<StepCategory, number>>;
  reasoning: number;
  failed: number;
  running: boolean;
  durationMs: number;
};

const CATEGORY_ORDER: StepCategory[] = ["directories", "reads", "searches", "web", "commands", "edits", "memory", "plans", "tools"];

export function isStepItem(item: TimelineItem): boolean {
  return item.kind === "reasoning" || item.kind === "tool" || item.kind === "shell" || item.kind === "edit";
}

export function stepFromItem(item: TimelineItem): Step {
  if (item.kind === "reasoning") return { key: item.id, kind: "reasoning", name: "", durationMs: item.duration_ms, turnId: item.turn_id, item };
  return {
    key: `tool:${item.call_id || item.id}`, kind: "tool", name: item.name || item.kind, toolKind: item.kind,
    phase: item.phase, durationMs: item.duration_ms, turnId: item.turn_id, item,
  };
}

export function stepFromTool(tool: ToolState, turnId?: string): Step {
  return { key: `tool:${tool.call_id}`, kind: "tool", name: tool.name, phase: tool.phase, turnId, tool };
}

/** A question to the person is a decision, not process noise: it never folds into a group. */
export function isQuestionStep(step: Step): boolean {
  return step.kind === "tool" && step.name === "ask_user";
}

/**
 * Steps that stand on their own instead of folding into a group: questions
 * are decisions, and edits are the result the person came for, so their diff
 * stays in the record rather than behind a one-line summary.
 */
export function isStandaloneStep(step: Step): boolean {
  return isQuestionStep(step) || (step.kind === "tool" && toolCategory(step.name, step.toolKind) === "edits");
}

/** Adds a step to the run it continues, or starts a new run. */
export function appendStep(segments: Segment[], step: Step): void {
  const last = segments[segments.length - 1];
  if (isStandaloneStep(step)) segments.push({ type: "steps", key: step.key, turnId: step.turnId, steps: [step], standalone: true });
  else if (last?.type === "steps" && !last.standalone) last.steps.push(step);
  else segments.push({ type: "steps", key: step.key, turnId: step.turnId, steps: [step] });
}

/**
 * Splits a timeline into messages and runs of consecutive process steps.
 * Pending approvals live in the dock, so they do not split a run. A resolved
 * allow/cancel line stays in the transcript, like a question.
 */
export function segmentTimeline(items: TimelineItem[]): Segment[] {
  const segments: Segment[] = [];
  for (const item of items) {
    if (item.kind === "approval" && item.phase !== "approved" && item.phase !== "cancelled") continue;
    if (!isStepItem(item)) {
      segments.push({ type: "item", item });
      continue;
    }
    appendStep(segments, stepFromItem(item));
  }
  return segments;
}

/** The timeline id of the reasoning the model is streaming right now, if any. */
export function streamingReasoningId(turn: ActiveTurn | null | undefined): string | undefined {
  if (!turn?.reasoning || turn.content) return undefined;
  return `${turn.turn_id}:reasoning:${turn.segment ?? 0}`;
}

export function stepCategory(step: Step): StepCategory | null {
  return step.kind === "reasoning" ? null : toolCategory(step.name, step.toolKind);
}

export function summarizeSteps(steps: Step[]): StepSummary {
  const summary: StepSummary = { counts: {}, reasoning: 0, failed: 0, running: false, durationMs: 0 };
  for (const step of steps) {
    if (typeof step.durationMs === "number") summary.durationMs += step.durationMs;
    const category = stepCategory(step);
    if (!category) {
      summary.reasoning += 1;
      continue;
    }
    summary.counts[category] = (summary.counts[category] ?? 0) + 1;
    // A plan update the plan rules turned down changed nothing and is not a failure.
    if (step.phase === "failed" && !(category === "plans" && planRejection(step.item?.result?.display ?? step.tool?.display))) summary.failed += 1;
    if (step.phase === "planned" || step.phase === "running" || step.phase === "awaiting_approval") summary.running = true;
  }
  return summary;
}

export function summaryCategories(summary: StepSummary): Array<[StepCategory, number]> {
  return CATEGORY_ORDER.filter((category) => summary.counts[category]).map((category) => [category, summary.counts[category]!]);
}
