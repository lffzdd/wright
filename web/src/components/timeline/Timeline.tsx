import { ArrowDown } from "@phosphor-icons/react";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { MarkdownContent } from "../../Markdown";
import { present, useT } from "../../i18n";
import type { ViewState } from "../../types";
import { AssistantMessage, InteractionCard, ReasoningRow, UserMessage, toolCardFromState } from "../cards/cards";
import { RevealCallContext, StepGroup, type RevealCall } from "./StepGroup";
import { appendStep, segmentTimeline, stepFromTool, type Segment } from "./steps";
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

const ENTER_MS = 400;

/**
 * Says whether an item should play its entrance. Items are keyed by what they
 * are (a tool call, a turn's reply), not by where they render, so a live item
 * moving into the record keeps its first-seen time and does not replay.
 * Everything present when a session first renders counts as already seen.
 */
function useEntrance(sessionId: string) {
  const ref = useRef({ session: sessionId, ready: false, born: new Map<string, number>() });
  if (ref.current.session !== sessionId) ref.current = { session: sessionId, ready: false, born: new Map() };
  const tracker = ref.current;
  useEffect(() => { tracker.ready = true; });
  const now = Date.now();
  return (key: string) => {
    let born = tracker.born.get(key);
    if (born === undefined) {
      born = tracker.ready ? now : 0;
      tracker.born.set(key, born);
    }
    return born > 0 && now - born < ENTER_MS;
  };
}

function TurnOutcome({ status, detail, enter }: { status?: string; detail?: string; enter?: boolean }) {
  const tr = useT();
  const label = status === "cancelled" ? tr("web.turn_cancelled") : status === "interrupted" ? tr("web.turn_interrupted") : tr("web.turn_failed");
  return <div className={`turn-outcome ${status === "cancelled" ? "cancelled" : "failed"}${enter ? " enter" : ""}`} role="status">
    <strong>{label}</strong>
    {detail && <span className="turn-outcome-detail">{detail}</span>}
  </div>;
}

/** How long a revealed tool card stays highlighted, and how long a miss notice stays up. */
const REVEAL_FLASH_MS = 1600;
const REVEAL_MISS_MS = 3000;

/** Per-message actions; each takes the id of a persisted message. */
export type MessageActions = {
  regenerate: (userMessageId: string) => void;
  edit: (userMessageId: string, text: string) => void;
  fork: (replyId: string) => void;
};

const persisted = (id: string) => /^msg_\d+$/.test(id);

export function Timeline({ state, respond, cancelQueued, userLabel, timestamp, children, revealCall, messageActions }: {
  state: ViewState;
  /** Shown only while the session is idle; omitted for read-only history. */
  messageActions?: MessageActions;
  /** A tool call to scroll to, open and highlight; a new `request` repeats it. */
  revealCall?: RevealCall | null;
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
  const [revealMissed, setRevealMissed] = useState(false);
  useEffect(() => {
    if (!revealCall) return;
    let flashTimer: number | undefined;
    let missTimer: number | undefined;
    let frame = 0;
    // A step group opened for this reveal renders its cards a frame or two later.
    const attempt = (tries: number) => {
      const node = scroller.current;
      const target = node?.querySelector<HTMLElement>(`[data-call-id="${revealCall.callId.replace(/["\\]/g, "\\$&")}"]`);
      if (!target && tries > 0) { frame = window.requestAnimationFrame(() => attempt(tries - 1)); return; }
      if (!node || !target) {
        setRevealMissed(true);
        missTimer = window.setTimeout(() => setRevealMissed(false), REVEAL_MISS_MS);
        return;
      }
      setRevealMissed(false);
      // Same as an outline jump: leave the bottom so new output does not pull the view away.
      node.dispatchEvent(new Event("tocjump"));
      const top = target.getBoundingClientRect().top - node.getBoundingClientRect().top + node.scrollTop - node.clientHeight / 3;
      if (typeof node.scrollTo === "function") node.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
      else node.scrollTop = Math.max(0, top);
      target.classList.remove("reveal-flash");
      void target.offsetWidth;
      target.classList.add("reveal-flash");
      flashTimer = window.setTimeout(() => target.classList.remove("reveal-flash"), REVEAL_FLASH_MS);
    };
    frame = window.requestAnimationFrame(() => attempt(3));
    return () => { window.cancelAnimationFrame(frame); window.clearTimeout(flashTimer); window.clearTimeout(missTimer); };
  }, [revealCall]);
  const timeline = state.timeline ?? [];
  const toc = useTimelineTOC(scroller, {
    sessionId: state.session.session_id,
    historyLength: state.history.length,
    activeContent: state.active_turn?.content,
    activeTurnId: state.active_turn?.turn_id,
    timelineLength: timeline.length,
  });
  const entering = useEntrance(state.session.session_id);
  const activeTurn = state.active_turn?.turn_id;
  const known = new Set(timeline.map((item) => item.id));
  const liveTools = (state.active_turn?.tools ?? []).filter((tool) => !known.has(tool.call_id));
  // The snapshot can be taken after the turn started but before the server
  // recorded the user message; show the prompt until the record arrives.
  const lastUser = [...timeline].reverse().find((item) => item.kind === "text" && item.role === "user" && !item.interjection);
  const pendingPrompt = state.active_turn?.prompt && lastUser?.text !== state.active_turn.prompt
    && !timeline.some((item) => item.role === "user" && item.turn_id !== undefined && item.turn_id === state.active_turn?.turn_id)
    ? state.active_turn.prompt : "";
  const liveText = state.active_turn?.content && !timeline.some((item) => item.kind === "text" && item.role === "assistant" && item.turn_id === state.active_turn?.turn_id && item.turn_id !== undefined) ? state.active_turn.content : "";
  // Consecutive reasoning and tool calls fold into one group; live tools join
  // the run they continue.
  const segments = segmentTimeline(timeline);
  const liveSteps = liveTools.map((tool) => stepFromTool(tool, activeTurn));
  // Before the prompt is recorded, live steps render after it instead.
  const liveSegments: Segment[] = [];
  for (const step of liveSteps) appendStep(pendingPrompt ? liveSegments : segments, step);
  // Groups after the latest prompt belong to the running turn.
  let lastPrompt = -1;
  // A message sent mid-turn does not start a turn: steps after it stay live.
  segments.forEach((segment, index) => { if (segment.type === "item" && segment.item.kind === "text" && segment.item.role === "user" && !segment.item.interjection) lastPrompt = index; });
  const turnRunning = Boolean(state.active_turn);
  // History can only be rewritten when nothing is running, queued or waiting.
  const actions = messageActions && !turnRunning && !state.queued_commands?.length && !state.pending_interactions.length ? messageActions : undefined;
  const lastUserIndex = lastUser ? timeline.indexOf(lastUser) : -1;
  const editableId = actions && lastUser && persisted(lastUser.id) ? lastUser.id : undefined;
  const lastReply = [...timeline].reverse().find((item) => item.kind === "text" && item.role === "assistant");
  const regenerateReplyId = editableId && lastReply && timeline.indexOf(lastReply) > lastUserIndex ? lastReply.id : undefined;
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
  return <RevealCallContext.Provider value={revealCall ?? null}><div className="conversation-stage" ref={stageRef} data-labels={labelsOff ? "off" : undefined} data-compact={outlineCompact ? "true" : undefined}>
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
      {segments.map((segment, index) => {
        if (segment.type === "steps") return <StepGroup key={segment.key} steps={segment.steps} live={turnRunning && (pendingPrompt ? false : index > lastPrompt)} sessionId={state.session?.session_id} entering={entering} />;        const item = segment.item;
        if (item.kind === "text" && item.role === "user") return <UserMessage key={item.id} text={item.text || ""} userLabel={userLabel} timestamp={timestamp} tocId={item.id} interjection={item.interjection} enter={entering(item.turn_id !== undefined && !item.interjection ? `user:${item.turn_id}` : item.id)} onEdit={actions && item.id === editableId ? () => actions.edit(item.id, item.text || "") : undefined} />;
        if (item.kind === "text") return <AssistantMessage key={item.id} text={item.text || ""} enter={entering(item.turn_id !== undefined ? `reply:${item.turn_id}` : item.id)} onRegenerate={actions && editableId && item.id === regenerateReplyId ? () => actions.regenerate(editableId) : undefined} onFork={actions && persisted(item.id) ? () => actions.fork(item.id) : undefined} />;
        if (item.kind === "status") return <TurnOutcome key={item.id} status={item.status} detail={item.text} enter={entering(item.id)} />;
        return null;
      })}
      {pendingPrompt && <UserMessage text={pendingPrompt} userLabel={userLabel} timestamp={timestamp} tocId={activeTurn ? `${activeTurn}:user` : "pending-user"} enter={entering(activeTurn ? `user:${activeTurn}` : "pending-user")} />}
      {liveSegments.map((segment) => segment.type === "steps" && <StepGroup key={segment.key} steps={segment.steps} live={turnRunning} sessionId={state.session?.session_id} entering={entering} />)}
      {liveText && <AssistantMessage text={liveText} enter={entering(activeTurn ? `reply:${activeTurn}` : "live-reply")} />}
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
        {state.active_turn.tools.map((tool) => toolCardFromState(tool, state.session?.session_id, undefined, true))}
        {state.active_turn.reasoning && <ReasoningRow text={state.active_turn.reasoning} />}
        {state.active_turn.content ? <AssistantMessage text={state.active_turn.content} /> : <p className="empty-small">{tr("web.working")}</p>}
      </div>}
    </>}
    {state.session.execution === "queued" && <p className="hint queue-reason" role="status">{tr("web.queued_reason", { reason: state.session.queue_reason || tr("web.queued_default") })}</p>}
    {(state.agents ?? []).map((agent) => <section className="agent-progress" key={agent.task_id}><strong>Subagent {agent.task_id} · {agent.status}</strong>{agent.content && <MarkdownContent content={agent.content} />}</section>)}
    {state.queued_commands.map((item) => <div className="queue-item" key={item.command_id}><span>{item.prompt}</span><button type="button" className="button" onClick={() => cancelQueued(item.command_id)}>{tr("web.remove_queued")}</button></div>)}
    {state.notices.filter((notice, index, all) => notice.code !== "notice.context_compact" || !all.slice(index + 1).some((item) => item.code === notice.code)).slice(-5).map((notice) => <div className="system-notice" role="status" key={notice.id}>{present(notice.code, notice.params, notice.text)}</div>)}
    {revealMissed && <p className="hint reveal-missed" role="status">{tr("web.reveal_call_missing")}</p>}
    {!stuck && <button type="button" className="button jump" onClick={() => { stick.current = true; setStuck(true); scroller.current?.scrollTo?.({ top: scroller.current.scrollHeight }); }}><ArrowDown size={12} weight="bold" aria-hidden="true" />{tr("web.jump_bottom")}</button>}
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
  </div></RevealCallContext.Provider>;
}
