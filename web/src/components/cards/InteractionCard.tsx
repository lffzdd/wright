import { ChatCircleDots, PaperPlaneRight } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { useT } from "../../i18n";
import type { Interaction } from "../../types";
import { PermissionApproval } from "../permissions/approval";

/** An open request for the person: a permission approval or a question. */
export function InteractionCard({ sessionId, interaction, respond, root }: {
  sessionId: string;
  interaction: Interaction;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
  root?: string;
}) {
  if (interaction.kind === "permission") {
    return <PermissionApproval sessionId={sessionId} interaction={interaction} respond={respond} root={root} />;
  }
  return <QuestionCard interaction={interaction} respond={respond} />;
}

function QuestionCard({ interaction, respond }: {
  interaction: Interaction;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
}) {
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState("");
  const card = useRef<HTMLElement>(null);
  const tr = useT();
  const options = interaction.options ?? [];
  const headingId = `ask-${interaction.request_id}`;
  useEffect(() => { card.current?.focus({ preventScroll: true }); }, [interaction.request_id]);
  const send = (value: string) => {
    if (!value || submitting) return;
    setSubmitting(value);
    Promise.resolve(respond(interaction.request_id, value)).then((accepted) => { if (accepted === false) setSubmitting(""); });
  };
  return <div className="tl-indent">
    <section ref={card} className="ask-card" role="alert" aria-labelledby={headingId} tabIndex={-1} onKeyDown={(event) => {
      if (event.target instanceof HTMLElement && event.target.closest("input,select,textarea")) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const index = Number(event.key) - 1;
      if (Number.isInteger(index) && index >= 0 && index < Math.min(options.length, 9)) { event.preventDefault(); send(options[index]); }
    }}>
      <header className="ask-head">
        <ChatCircleDots size={16} weight="fill" className="ask-icon" aria-hidden="true" />
        <h3 id={headingId}>{interaction.question}</h3>
        <span className="approval-waiting"><span className="dot pulse" />{tr("web.approval.waiting")}</span>
      </header>
      {interaction.context && <p className="ask-context">{interaction.context}</p>}
      {options.length > 0 && <div className="ask-options">
        {options.map((option, index) => <button type="button" className={`ask-option${submitting === option ? " chosen" : ""}`} key={option} disabled={Boolean(submitting)} onClick={() => send(option)}>
          {index < 9 && <kbd>{index + 1}</kbd>}<span>{option}</span>
        </button>)}
      </div>}
      <form className="ask-reply" onSubmit={(event) => { event.preventDefault(); send(answer.trim()); }}>
        <textarea aria-label={tr("web.answer")} rows={1} value={answer} disabled={Boolean(submitting)}
          placeholder={tr(options.length ? "web.answer_other" : "web.answer_placeholder")}
          onChange={(event) => {
            setAnswer(event.target.value);
            event.target.style.height = "auto";
            event.target.style.height = `${Math.min(event.target.scrollHeight, 160)}px`;
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(answer.trim()); }
          }} />
        <button className="ask-send" aria-label={tr("web.submit_answer")} title={tr("web.submit_answer")} disabled={Boolean(submitting) || !answer.trim()}><PaperPlaneRight size={14} weight="fill" /></button>
      </form>
    </section>
  </div>;
}
