import { Check, Circle, ListChecks, Minus, Warning } from "@phosphor-icons/react";
import { useT } from "../../i18n";
import { toolCategory } from "./toolKinds";
import { textOf } from "./values";

type PlanStep = { id: string; title: string; status: string; note?: string };
export type PlanView = { objective: string; steps: PlanStep[] };
export type PlanRejection = { code: string; params: Record<string, string> };

/** The plan rule that turned an update down, from ToolResult.display; the plan itself did not change. */
export function planRejection(display: unknown): PlanRejection | null {
  const rejected = display && typeof display === "object" ? (display as Record<string, unknown>).rejected : null;
  if (!rejected || typeof rejected !== "object") return null;
  const { code, params } = rejected as Record<string, unknown>;
  return {
    code: textOf(code),
    params: params && typeof params === "object" ? Object.fromEntries(Object.entries(params).map(([key, value]) => [key, textOf(value)])) : {},
  };
}

/** The plan a plan tool returned, or null when its result holds none. */
export function planOf(source: Record<string, unknown> | null | undefined): PlanView | null {
  if (!source || !Array.isArray(source.steps)) return null;
  const steps = (source.steps as Array<Record<string, unknown>>).filter((step) => step && typeof step === "object")
    .map((step) => ({ id: textOf(step.id), title: textOf(step.title), status: textOf(step.status) || "pending", note: textOf(step.note) }));
  return { objective: textOf(source.objective), steps };
}

export function PlanMark({ status }: { status: string }) {
  const tr = useT();
  const label = tr(`web.plan.status.${status}`);
  if (status === "completed") return <Check size={12} weight="bold" className="plan-mark completed" aria-label={label} />;
  if (status === "in_progress") return <span className="plan-mark in_progress" role="img" aria-label={label} />;
  if (status === "blocked") return <Warning size={12} weight="bold" className="plan-mark blocked" aria-label={label} />;
  if (status === "skipped") return <Minus size={12} weight="bold" className="plan-mark skipped" aria-label={label} />;
  return <Circle size={12} className="plan-mark pending" aria-label={label} />;
}

function planRowClass(status: string): string {
  if (status === "completed" || status === "skipped") return "done";
  if (status === "in_progress" || status === "blocked") return "active";
  return "todo";
}

/** Standalone plan card (reference `.plan-card`), distinct from expandable tool rows. */
export function PlanTimelineCard({ plan, rejection, body, enter, callId }: {
  plan: PlanView;
  rejection?: PlanRejection | null;
  body?: string;
  enter?: boolean;
  callId?: string;
}) {
  const tr = useT();
  const done = plan.steps.filter((step) => step.status === "completed").length;
  return <div className={`tl-indent${enter ? " enter" : ""}`} data-call-id={callId}>
    <section className="plan-card">
      <div className="pc-head"><ListChecks size={14} aria-hidden="true" />{tr("web.plan.title")}<span className="count">{tr("web.plan.count", { done, total: plan.steps.length })}</span></div>
      <ul className="todo plan-checklist">
        {plan.steps.map((step) => <li key={step.id || step.title} className={[planRowClass(step.status), step.status].filter(Boolean).join(" ")}>
          <span className="cb" aria-hidden="true">{step.status === "completed" ? <Check size={10} weight="bold" /> : null}</span>
          <span className="tx">{step.title}</span>
        </li>)}
      </ul>
      {rejection && body && <p className="plan-rejection">{body}</p>}
    </section>
  </div>;
}

/** What a plan tool call shows as a plan card, or null when it is an ordinary tool row. */
export function planCardPayload(name: string, phase: string | undefined, display: unknown, data: unknown, result: Record<string, unknown> | null | undefined) {
  const metricSource = data && typeof data === "object" ? data as Record<string, unknown> : result;
  const rejection = phase === "failed" && toolCategory(name) === "plans" ? planRejection(display) : null;
  const planSource = display && typeof display === "object" ? display as Record<string, unknown> : metricSource;
  const plan = toolCategory(name) === "plans" && (phase !== "failed" || rejection) ? planOf(planSource) : null;
  if (!plan || !plan.steps.length) return null;
  const body = typeof metricSource?.err === "string" ? metricSource.err : typeof result?.err === "string" ? result.err : "";
  return { plan, rejection, body: rejection ? body : "" };
}
