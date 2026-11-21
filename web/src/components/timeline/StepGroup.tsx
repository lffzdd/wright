import { ReasoningRow } from "../cards/Messages";
import { toolCardFromState, toolCardFromTimeline } from "../cards/ToolCard";
import type { Step } from "./steps";

export type { RevealCall } from "./revealCall";
export { RevealCallContext } from "./revealCall";

function StepRow({ step, sessionId, enter, live, thinking }: { step: Step; sessionId?: string; enter?: boolean; live?: boolean; thinking?: boolean }) {
  // Settled reasoning is `<record>:reason` in the timeline and `<record>:reasoning` in the context.
  const reasoningId = step.item?.id.endsWith(":reason") ? `${step.item.id}ing` : undefined;
  if (step.kind === "reasoning") {
    return <ReasoningRow text={step.item?.text || ""} durationMs={step.durationMs} enter={enter} live={thinking} contextId={reasoningId} />;
  }
  if (step.item) return toolCardFromTimeline(step.item, sessionId, enter, live);
  return toolCardFromState(step.tool!, sessionId, enter, live);
}

/**
 * Consecutive tools and reasoning in a turn, shown flat like the reference
 * transcript (`.tool-group` spacing only — no summary fold).
 */
export function StepGroup({ steps, live, thinkingId, sessionId, entering }: {
  steps: Step[];
  live: boolean;
  /** The reasoning the model is writing now: it streams open, the rest stay folded. */
  thinkingId?: string;
  sessionId?: string;
  entering: (key: string) => boolean;
}) {
  // No wrapper element: timeline subgrid expects `.tl-indent` as direct rows (`grid-column: 2`).
  return <>
    {steps.map((step) => <StepRow
      key={step.key} step={step} sessionId={sessionId} enter={entering(step.key)} live={live}
      thinking={live && step.kind === "reasoning" && step.item?.id === thinkingId}
    />)}
  </>;
}
