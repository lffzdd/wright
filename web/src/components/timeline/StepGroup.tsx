import { ReasoningRow } from "../cards/Messages";
import { toolCardFromState, toolCardFromTimeline } from "../cards/ToolCard";
import type { Step } from "./steps";

export type { RevealCall } from "./revealCall";
export { RevealCallContext } from "./revealCall";

function StepRow({ step, sessionId, enter, live }: { step: Step; sessionId?: string; enter?: boolean; live?: boolean }) {
  if (step.kind === "reasoning") return <ReasoningRow text={step.item?.text || ""} durationMs={step.durationMs} enter={enter} live={false} />;
  if (step.item) return toolCardFromTimeline(step.item, sessionId, enter, live);
  return toolCardFromState(step.tool!, sessionId, enter, live);
}

/**
 * Consecutive tools and reasoning in a turn, shown flat like the reference
 * transcript (`.tool-group` spacing only — no summary fold).
 */
export function StepGroup({ steps, live, sessionId, entering }: {
  steps: Step[];
  live: boolean;
  sessionId?: string;
  entering: (key: string) => boolean;
}) {
  // No wrapper element: timeline subgrid expects `.tl-indent` as direct rows (`grid-column: 2`).
  return <>
    {steps.map((step) => <StepRow key={step.key} step={step} sessionId={sessionId} enter={entering(step.key)} live={live} />)}
  </>;
}