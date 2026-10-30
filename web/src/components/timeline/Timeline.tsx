import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { MarkdownContent } from "../../Markdown";
import { present, useT } from "../../i18n";
import type { ViewState } from "../../types";
import { AssistantMessage, InteractionCard, ReasoningRow, UserMessage, toolCardFromState, toolCardFromTimeline } from "../cards/cards";
import { ConversationOutline, OUTLINE_DOCK_MIN_ITEMS, OUTLINE_TEXT_AT, TimelineTOC, useTimelineTOC } from "./toc";

export function InteractionDock({ sessionId, pending, respond, root }: {
  sessionId: string;
  root?: string;
  pending: ViewState["pending_interactions"];
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
}) {
  if (!pending.length) return null;
  return <div className="interaction-dock" aria-live="polite">
    {pending.map((item) => <InteractionCard key={item.request_id} sessionId={sessionId} interaction={item} respond={respond} root={root} />)}
  </div>;
}

function TurnOutcome({ status, detail }: { status?: string; detail?: string }) {
  const tr = useT();
  const label = status === "cancelled" ? tr("web.turn_cancelled") : status === "interrupted" ? tr("web.turn_interrupted") : tr("web.turn_failed");
  return <div className={`turn-outcome ${status === "cancelled" ? "cancelled" : "failed"}`} role="status">
    <strong>{label}</strong>
    {detail && <span className="turn-outcome-detail">{detail}</span>}
  </div>;
}

export function Timeline({ state, respond, cancelQueued, userLabel, timestamp, children }: {
  state: ViewState;
  respond: (requestId: string, answer: unknown) => boolean | Promise<boolean>;
  cancelQueued: (commandId: string) => void;
  userLabel?: string;
  timestamp?: string;
  children?: ReactNode;
}) {
  const tr = useT();
  const scroller = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [labelsOff, setLabelsOff] = useState(true);
  const [narrow, setNarrow] = useState(false);
  const stick = useRef(true);
  const [stuck, setStuck] = useState(true);
  const lastTop = useRef(0);
  const onScroll = () => {
    const node = scroller.current;
    if (!node) return;
    const near = node.scrollHeight - node.scrollTop - node.clientHeight < 48;
    // Content can grow between a programmatic scroll and its event; only an
    // upward move means the reader left the bottom.
    const movedUp = node.scrollTop < lastTop.current - 1;
    lastTop.current = node.scrollTop;
    if (near) stick.current = true;
    else if (movedUp) stick.current = false;
    setStuck(stick.current);
  };
  useEffect(() => {
    const node = scroller.current;
    if (!node) return;
    const release = () => {
      stick.current = false;
      setStuck(false);
    };
    node.addEventListener("tocjump", release);
    return () => node.removeEventListener("tocjump", release);
  }, []);
  useEffect(() => {
    const stage = stageRef.current;
    const column = stage?.parentElement;
    if (!column || typeof ResizeObserver === "undefined") return;
    const restore = () => {
      const tooNarrow = column.clientWidth < OUTLINE_TEXT_AT;
      setNarrow(tooNarrow);
      if (tooNarrow) setLabelsOff(false);
    };
    restore();
    const observer = new ResizeObserver(restore);
    observer.observe(column);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    // Markdown, code blocks and images can grow after the commit that scrolled
    // to the bottom; keep a stuck view pinned while the content settles.
    const node = scroller.current;
    const content = node?.firstElementChild;
    if (!node || !content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (stick.current) node.scrollTo?.({ top: node.scrollHeight });
    });
    observer.observe(content);
    // The approval dock takes height from the scroller, not from its content.
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    if (stick.current) scroller.current?.scrollTo?.({ top: scroller.current.scrollHeight });
  }, [
    state.history.length,
    state.active_turn?.content,
    state.active_turn?.reasoning,
    state.active_turn?.tools.length,
    state.timeline?.length,
  ]);
  const timeline = state.timeline ?? [];
  const toc = useTimelineTOC(scroller, {
    sessionId: state.session.session_id,
    historyLength: state.history.length,
    activeContent: state.active_turn?.content,
    activeTurnId: state.active_turn?.turn_id,
    timelineLength: timeline.length,
  });
  const known = new Set(timeline.map((item) => item.id));
  const liveTools = (state.active_turn?.tools ?? []).filter((tool) => !known.has(tool.call_id));
  // The snapshot can be taken after the turn started but before the server
  // recorded the user message; show the prompt until the record arrives.
  const lastUser = [...timeline].reverse().find((item) => item.kind === "text" && item.role === "user");
  const pendingPrompt = state.active_turn?.prompt && lastUser?.text !== state.active_turn.prompt
    && !timeline.some((item) => item.role === "user" && item.turn_id !== undefined && item.turn_id === state.active_turn?.turn_id)
    ? state.active_turn.prompt : "";
  const liveText = state.active_turn?.content && !timeline.some((item) => item.kind === "text" && item.role === "assistant" && item.turn_id === state.active_turn?.turn_id && item.turn_id !== undefined) ? state.active_turn.content : "";
  const outlineCompact = toc.items.length < OUTLINE_DOCK_MIN_ITEMS;
  const lastOutlineSessionRef = useRef<string | undefined>(state.session.session_id);
  useEffect(() => {
    if (lastOutlineSessionRef.current === state.session.session_id) return;
    lastOutlineSessionRef.current = state.session.session_id;
    setLabelsOff(toc.items.length < OUTLINE_DOCK_MIN_ITEMS);
  }, [state.session.session_id, toc.items.length]);
  useEffect(() => {
    if (outlineCompact) setLabelsOff(true);
  }, [outlineCompact]);
  return <div className="conversation-stage" ref={stageRef} data-labels={labelsOff ? "off" : undefined} data-compact={outlineCompact ? "true" : undefined}>
    <div
      className="conversation-stream"
      data-outline={outlineCompact || labelsOff || narrow ? "ticks" : "dock"}
    >
    <div className="conversation-body">
    <div className="conversation-scroll" ref={scroller} onScroll={onScroll}>
    <div className="conversation-main">
    <div className="timeline">
    <TimelineTOC {...toc} />
    {!timeline.length && !state.history.length && !state.active_turn && <p className="empty-small">{tr("web.empty_timeline")}</p>}
    {timeline.length > 0 ? <>
      {timeline.map((item) => {
        if (item.kind === "text" && item.role === "user") return <UserMessage key={item.id} text={item.text || ""} userLabel={userLabel} timestamp={timestamp} tocId={item.id} />;
        if (item.kind === "reasoning") return <ReasoningRow key={item.id} text={item.text || ""} durationMs={item.duration_ms} />;
        if (item.kind === "text") return <AssistantMessage key={item.id} text={item.text || ""} />;
        if (item.kind === "approval") return null;
        if (item.kind === "status") return <TurnOutcome key={item.id} status={item.status} detail={item.text} />;
        return toolCardFromTimeline(item, state.session?.session_id);
      })}
      {pendingPrompt && <UserMessage text={pendingPrompt} userLabel={userLabel} timestamp={timestamp} tocId={state.active_turn?.turn_id ? `${state.active_turn.turn_id}:user` : "pending-user"} />}
      {liveTools.map((tool) => toolCardFromState(tool, state.session?.session_id))}
      {liveText && <AssistantMessage text={liveText} />}
      {state.active_turn && !liveText && !liveTools.length && !timeline.some((item) => item.turn_id !== undefined && item.turn_id === state.active_turn?.turn_id && item.role === "assistant") && <p className="empty-small working-indicator">{tr("web.working")}</p>}
    </> : <>
      {state.history.map((turn, index) => <div className="turn" key={turn.turn_id || index}>
        {turn.user && <UserMessage text={turn.user} tocId={turn.turn_id || `history-${index}`} />}
        {turn.tools?.map((tool) => toolCardFromState(tool, state.session?.session_id))}
        {turn.status === "failed" && <p className="form-error">{tr("web.turn_failed")}</p>}
        {turn.status === "cancelled" && <p className="form-error">{tr("web.turn_cancelled")}</p>}
        {turn.assistant ? <AssistantMessage text={turn.assistant} /> : turn.status === "failed" || turn.status === "cancelled" ? null : <p className="empty-small">{tr("web.no_answer")}</p>}
      </div>)}
      {state.active_turn && <div className="turn">
        {state.active_turn.prompt && <UserMessage text={state.active_turn.prompt} tocId={state.active_turn.turn_id ? `${state.active_turn.turn_id}:user` : "active-user"} />}
        {state.active_turn.tools.map((tool) => toolCardFromState(tool, state.session?.session_id))}
        {state.active_turn.reasoning && <ReasoningRow text={state.active_turn.reasoning} />}
        {state.active_turn.content ? <AssistantMessage text={state.active_turn.content} /> : <p className="empty-small">{tr("web.working")}</p>}
      </div>}
    </>}
    {state.session.execution === "queued" && <p className="hint queue-reason" role="status">{tr("web.queued_reason", { reason: state.session.queue_reason || tr("web.queued_default") })}</p>}
    {(state.agents ?? []).map((agent) => <section className="agent-progress" key={agent.task_id}><strong>Subagent {agent.task_id} · {agent.status}</strong>{agent.content && <MarkdownContent content={agent.content} />}</section>)}
    {state.queued_commands.map((item) => <div className="queue-item" key={item.command_id}><span>{item.prompt}</span><button type="button" className="button" onClick={() => cancelQueued(item.command_id)}>{tr("web.remove_queued")}</button></div>)}
    {state.notices.filter((notice, index, all) => notice.code !== "notice.context_compact" || !all.slice(index + 1).some((item) => item.code === notice.code)).slice(-5).map((notice) => <div className="system-notice" role="status" key={notice.id}>{present(notice.code, notice.params, notice.text)}</div>)}
    {!stuck && <button type="button" className="button jump" onClick={() => { stick.current = true; setStuck(true); scroller.current?.scrollTo?.({ top: scroller.current.scrollHeight }); }}>{tr("web.jump_bottom")}</button>}
    </div>
    </div>
    </div>
    <div className="conversation-main conversation-main-foot">
    {children}
    </div>
    </div>
    <ConversationOutline
      items={toc.items}
      activeId={toc.activeId}
      tickTops={toc.tickTops}
      scrollToHeading={toc.scrollToHeading}
      labelsOff={labelsOff}
      onLabelsOff={() => setLabelsOff(true)}
      onLabelsOn={() => setLabelsOff(false)}
    />
    </div>
  </div>;
}
