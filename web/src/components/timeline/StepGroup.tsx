import { CaretDown, Stack } from "@phosphor-icons/react";
import { useState } from "react";
import { useT } from "../../i18n";
import { ReasoningRow, formatDuration, toolCardFromState, toolCardFromTimeline } from "../cards/cards";
import { summarizeSteps, summaryCategories, type Step } from "./steps";

function StepRow({ step, sessionId, enter, live }: { step: Step; sessionId?: string; enter?: boolean; live?: boolean }) {
  if (step.kind === "reasoning") return <ReasoningRow text={step.item?.text || ""} durationMs={step.durationMs} enter={enter} />;
  if (step.item) return toolCardFromTimeline(step.item, sessionId, enter, live);
  return toolCardFromState(step.tool!, sessionId, enter, live);
}

/**
 * A run of consecutive process steps. It stays open while its turn is live and
 * folds into a one-line summary once the turn ends, unless the person opened
 * it or worked inside it.
 */
export function StepGroup({ steps, live, sessionId, entering }: {
  steps: Step[];
  live: boolean;
  sessionId?: string;
  entering: (key: string) => boolean;
}) {
  const tr = useT();
  const [toggled, setToggled] = useState<boolean | null>(null);
  if (steps.length === 1) return <StepRow step={steps[0]} sessionId={sessionId} enter={entering(steps[0].key)} live={live} />;
  const open = toggled ?? live;
  const summary = summarizeSteps(steps);
  const parts = summaryCategories(summary).map(([category, count]) => tr(`web.steps.${category}${count === 1 ? ".one" : ""}`, { count }));
  if (!parts.length) parts.push(tr("web.steps.reasoning"));
  return <section className={`step-group${open ? " open" : ""}${steps.some((step) => entering(step.key)) && !open ? " enter" : ""}`}>
    <button
      type="button"
      className="step-group-head"
      aria-expanded={open}
      aria-label={tr(open ? "web.steps.collapse" : "web.steps.expand")}
      onClick={() => setToggled(!open)}
    >
      <CaretDown size={12} className={open ? "" : "collapsed"} aria-hidden="true" />
      <Stack size={14} aria-hidden="true" className="step-group-icon" />
      <span className="step-group-summary">{parts.join(" · ")}</span>
      {summary.failed > 0 && <span className="step-group-failed">{tr("web.steps.failed", { count: summary.failed })}</span>}
      {summary.durationMs > 0 && <span className="step-group-duration mono">{formatDuration(summary.durationMs)}</span>}
    </button>
    {open && <div className="step-group-body" onClick={() => { if (toggled === null) setToggled(true); }}>
      {steps.map((step) => <StepRow key={step.key} step={step} sessionId={sessionId} enter={entering(step.key)} live={live} />)}
    </div>}
  </section>;
}
