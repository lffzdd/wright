import { CaretDown, ChatCircleDots, Check } from "@phosphor-icons/react";
import { useState } from "react";
import { useT } from "../../i18n";
import { textOf } from "./values";

/**
 * An answered (or abandoned) ask_user call, kept in the timeline as a decision:
 * one line with the question and the answer, opening to every option offered.
 * While the question is still open, the interaction dock shows it instead.
 */
export function QuestionRecord({ args, data, phase, enter }: {
  args?: Record<string, unknown>;
  data?: unknown;
  phase?: string;
  enter?: boolean;
}) {
  const tr = useT();
  const [open, setOpen] = useState(false);
  if (phase !== "succeeded" && phase !== "failed") return null;
  const result = data && typeof data === "object" ? data as Record<string, unknown> : undefined;
  const question = textOf(args?.question) || textOf(result?.question);
  const context = textOf(args?.context);
  const options = Array.isArray(args?.options) ? args.options.filter((option): option is string => typeof option === "string") : [];
  const answer = phase === "succeeded" ? textOf(result?.answer) || textOf(args?.answer) : "";
  const own = Boolean(answer) && !options.includes(answer);
  return <div className={`tl-indent${enter ? " enter" : ""}`}>
    <section className={`question-record${open ? " open" : ""}${answer ? "" : " unanswered"}`}>
      <button type="button" className="question-record-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="step-icon question" aria-hidden="true"><ChatCircleDots size={13} weight="fill" /></span>
        <span className="question-record-q">{question}</span>
        <span className="question-record-arrow" aria-hidden="true">→</span>
        <span className="question-record-a">{answer || tr("web.question.no_answer")}</span>
        <CaretDown size={12} className={open ? "" : "collapsed"} aria-hidden="true" />
      </button>
      {open && <div className="question-record-body">
        {context && <p className="question-record-context">{context}</p>}
        {options.length > 0 && <ol className="question-record-options">
          {options.map((option, index) => <li key={option} className={option === answer ? "chosen" : ""} aria-current={option === answer ? "true" : undefined}>
            <kbd>{index + 1}</kbd><span>{option}</span>{option === answer && <Check size={13} weight="bold" aria-label={tr("web.question.chosen")} />}
          </li>)}
        </ol>}
        {own && <div className="question-record-own">
          <span>{tr(options.length ? "web.question.own_answer" : "web.question.answer")}</span>
          <p>{answer}</p>
        </div>}
        {!answer && <p className="question-record-none">{tr("web.question.no_answer_detail")}</p>}
      </div>}
    </section>
  </div>;
}
