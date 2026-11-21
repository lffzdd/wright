import { ArrowDown, X } from "@phosphor-icons/react";
import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { MarkdownContent } from "../code/Markdown";
import { present, useT } from "../../i18n";
import type { TimelineItem, ViewState } from "../../types";
import { AssistantMessage, ReasoningRow, UserMessage } from "../cards/Messages";
import { InteractionCard } from "../cards/InteractionCard";
import { PermissionDoneLine, permissionTarget } from "../cards/PermissionRecord";
import { toolCardFromState } from "../cards/ToolCard";
import { RevealCallContext, StepGroup, type RevealCall } from "./StepGroup";
import { appendStep, segmentTimeline, stepFromTool, streamingReasoningId, type Segment } from "./steps";
import { TurnFooter } from "./TurnFooter";
import { summarizeTurns, turnKeys } from "./turnSummary";
import { ConversationOutline, OUTLINE_DOCK_MIN_ITEMS, TimelineTOC, useTimelineTOC } from "./toc";

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
const REVEAL_FLASH_MS = 2000;
const REVEAL_MISS_MS = 3000;

/** Per-message actions; each takes the id of a persisted message. */
export type MessageActions = {
  regenerate: (userMessageId: string) => void;
  edit: (userMessageId: string, text: string) => void;
  fork: (replyId: string) => void;
};

const persisted = (id: string) => /^msg_\d+$/.test(id);

export function QueuedCommands({ items, onCancel }: { items: ViewState["queued_commands"]; onCancel: (commandId: string) => void }) {
  const tr = useT();
  if (!items.length) return null;
  return <div className="queue">
    {items.map((item, index) => <div className="queue-item q-item" key={item.command_id}>
      <span className="q-tag">{tr("web.queue_n", { n: index + 1 })}</span>
      <span className="q-text">{item.prompt}</span>
      <button type="button" className="icon-button" onClick={() => onCancel(item.command_id)} aria-label={tr("web.remove_queued")}><X size={13} /></button>
    </div>)}
  </div>;
}

export function Timeline({ state, userLabel, timestamp, children, revealCall, messageActions, onViewChanges, onSuggest }: {
  state: ViewState;
  /** Opens the Changes view from a turn's summary line. */
  onViewChanges?: () => void;
  /** Puts a suggested follow-up into the composer. */
  onSuggest?: (text: string) => void;
  /** Shown only while the session is idle; omitted for read-only history. */
  messageActions?: MessageActions;
  /** A tool call to scroll to, open and highlight; a new `request` repeats it. */
  revealCall?: RevealCall | null;
  userLabel?: string;
  timestamp?: string;
  /** Foot content. Pass a function to mount the jump control inside `.composer-inner`. */
  children?: ReactNode | ((jump: ReactNode) => ReactNode);
}) {
  const tr = useT();
  const scroller = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  // The outline list floats over the conversation; pinned keeps it open after the pointer leaves.
  const [outlinePinned, setOutlinePinned] = useState(false);
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
  // A footer's tool-call count reveals the turn's first call; an outside reveal replaces it.
  const [footReveal, setFootReveal] = useState<RevealCall | null>(null);
  useEffect(() => setFootReveal(null), [revealCall]);
  const reveal = footReveal ?? revealCall ?? null;
  const [revealMissed, setRevealMissed] = useState(false);
  useEffect(() => {
    if (!reveal) return;
    let flashTimer: number | undefined;
    let missTimer: number | undefined;
    let frame = 0;
    // A step group opened for this reveal renders its cards a frame or two later.
    const attempt = (tries: number) => {
      const node = scroller.current;
      const escape = (value: string) => value.replace(/["\\]/g, "\\$&");
      const exact = reveal.callId ? `[data-call-id="${escape(reveal.callId)}"]`
        : reveal.contextId ? `[data-context-id="${escape(reveal.contextId)}"]` : "";
      const fallback = reveal.tocId ? `[data-toc-id="${escape(reveal.tocId)}"]` : "";
      // The exact element first; once the retries run out, the turn's prompt.
      const find = (selector: string) => selector ? node?.querySelector<HTMLElement>(selector) ?? null : null;
      const target = find(exact) ?? (tries === 0 || !exact ? find(fallback) : null);
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
  }, [reveal]);
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
  // Each finished turn closes with a summary under its last reply.
  const keys = turnKeys(timeline);
  const turnSummaries = summarizeTurns(timeline, keys);
  const keyOf = new Map(timeline.map((item, index) => [item.id, keys[index]]));
  // While a turn runs, the newest turn is still open and gets no footer yet.
  const openKey = state.active_turn ? keys[keys.length - 1] : undefined;
  // The summary goes after the turn's last segment, whatever kind it is.
  const segmentKey = (segment: Segment) => {
    const id = segment.type === "steps" ? segment.steps.find((step) => step.item)?.item?.id : segment.item.id;
    return id ? keyOf.get(id) : undefined;
  };
  const closingSegment = new Map<string, number>();
  segments.forEach((segment, index) => {
    const key = segmentKey(segment);
    if (segment.type === "item" && segment.item.role === "user") return;
    if (key && key !== openKey && key !== activeTurn) closingSegment.set(key, index);
  });
  const lastAssistantOf = (key: string) => {
    for (let index = timeline.length - 1; index >= 0; index -= 1) {
      const item = timeline[index];
      if (keys[index] === key && item.kind === "text" && item.role === "assistant" && item.text) return item;
    }
    return undefined;
  };
  // The newest settled turn keeps its actions in view.
  const latestKey = keys.length && keys[keys.length - 1] !== openKey ? keys[keys.length - 1] : undefined;
  const findIn = (key: string, test: (item: TimelineItem) => boolean) => timeline.find((item, i) => keys[i] === key && test(item));
  const footerAt = (segment: Segment, index: number) => {
    const key = segmentKey(segment);
    const summary = key && closingSegment.get(key) === index ? turnSummaries.get(key) : undefined;
    if (!summary || !key) return null;
    const reply = lastAssistantOf(key);
    const canRegenerate = Boolean(actions && editableId && reply && reply.id === regenerateReplyId);
    const callId = findIn(key, (item) => ["tool", "shell", "edit"].includes(item.kind) && Boolean(item.call_id))?.call_id;
    const tocId = findIn(key, (item) => item.kind === "text" && item.role === "user")?.id;
    const showTools = callId ? () => setFootReveal({ callId, tocId, request: Date.now() }) : undefined;
    return <TurnFooter
      key={`${key}:footer`}
      summary={summary}
      latest={key === latestKey}
      reply={reply?.text}
      onViewChanges={onViewChanges}
      onSuggest={onSuggest}
      onRegenerate={canRegenerate ? () => actions?.regenerate(editableId!) : undefined}
      onFork={reply && actions && persisted(reply.id) ? () => actions.fork(reply.id) : undefined}
      onShowTools={showTools}
    />;
  };
  const renderSegment = (segment: Segment, index: number) => {
        if (segment.type === "steps") return <StepGroup key={segment.key} steps={segment.steps} live={turnRunning && (pendingPrompt ? false : index > lastPrompt)} thinkingId={streamingReasoningId(state.active_turn)} sessionId={state.session?.session_id} entering={entering} />;
        const item = segment.item;
        if (item.kind === "text" && item.role === "user") return <UserMessage key={item.id} text={item.text || ""} userLabel={userLabel} timestamp={timestamp} tocId={item.id} interjection={item.interjection} enter={entering(item.turn_id !== undefined && !item.interjection ? `user:${item.turn_id}` : item.id)} onEdit={actions && item.id === editableId ? () => actions.edit(item.id, item.text || "") : undefined} />;
        if (item.kind === "text") {
          // A settled turn's last reply hands its actions to the footer row.
          const footerKey = keyOf.get(item.id);
          const hasFooter = Boolean(footerKey && closingSegment.has(footerKey) && turnSummaries.has(footerKey));
          const inFooter = hasFooter && lastAssistantOf(footerKey!)?.id === item.id;
          const reply = <AssistantMessage key={item.id} contextId={`${item.id}:text`} text={item.text || ""} enter={entering(item.turn_id !== undefined ? `reply:${item.turn_id}` : item.id)} actionsInFooter={inFooter} onRegenerate={actions && editableId && item.id === regenerateReplyId && !hasFooter ? () => actions.regenerate(editableId) : undefined} onFork={actions && persisted(item.id) ? () => actions.fork(item.id) : undefined} />;
          return reply;
        }
        if (item.kind === "status") return <TurnOutcome key={item.id} status={item.status} detail={item.text} enter={entering(item.id)} />;
        if (item.kind === "approval" && item.interaction && item.decision) {
          return <PermissionDoneLine key={item.id} command={permissionTarget(item.interaction)} decision={item.decision} enter={entering(item.id)} />;
        }
        return null;
  };
  const isUserSegment = (segment: Segment): segment is Extract<Segment, { type: "item" }> => (
    segment.type === "item" && segment.item.kind === "text" && segment.item.role === "user"
  );
  const renderTurnGroups = () => {
    const nodes: ReactNode[] = [];
    let index = 0;
    while (index < segments.length) {
      const segment = segments[index];
      if (isUserSegment(segment)) {
        nodes.push(<Fragment key={segment.item.id}>{renderSegment(segment, index)}</Fragment>);
        index += 1;
        continue;
      }
      const key = segmentKey(segment) || `turn:${index}`;
      const group: Array<{ segment: Segment; index: number }> = [];
      while (index < segments.length) {
        const next = segments[index];
        if (isUserSegment(next)) break;
        if ((segmentKey(next) || `turn:${index}`) !== key) break;
        group.push({ segment: next, index });
        index += 1;
      }
      const last = group[group.length - 1];
      nodes.push(<div className="turn" key={key}>
        {group.map((entry) => <Fragment key={entry.segment.type === "steps" ? entry.segment.key : entry.segment.item.id}>{renderSegment(entry.segment, entry.index)}</Fragment>)}
        {last ? footerAt(last.segment, last.index) : null}
      </div>);
    }
    return nodes;
  };
  const lastOutlineSessionRef = useRef<string | undefined>(state.session.session_id);
  useEffect(() => {
    if (lastOutlineSessionRef.current === state.session.session_id) return;
    lastOutlineSessionRef.current = state.session.session_id;
    setOutlinePinned(false);
  }, [state.session.session_id]);
  useEffect(() => {
    if (outlineCompact) setOutlinePinned(false);
  }, [outlineCompact]);
  const jump = <button type="button" className={`jump${stuck ? "" : " show"}`} aria-hidden={stuck || undefined} tabIndex={stuck ? -1 : undefined} onClick={() => { stick.current = true; setStuck(true); scroller.current?.scrollTo?.({ top: scroller.current.scrollHeight }); }}><ArrowDown size={14} weight="bold" aria-hidden="true" />{tr("web.jump_bottom")}</button>;
  const foot = typeof children === "function" ? children(jump) : <>{jump}{children}</>;
  return <RevealCallContext.Provider value={reveal}><div className="conversation-stage" ref={stageRef} data-compact={outlineCompact ? "true" : undefined}>
    <div
      className="conversation-stream"
      data-outline="ticks"
    >
    <div className="conversation-body">
    <div className="conversation-scroll-pane">
    <div className="conversation-scroll" ref={scroller} onScroll={onScroll}>
    <div className="conversation-main">
    <div className="timeline">
    <TimelineTOC {...toc} />
    {!timeline.length && !state.history.length && !state.active_turn && <p className="empty-small">{tr("web.empty_timeline")}</p>}
    {timeline.length > 0 ? <>
      {renderTurnGroups()}
      {pendingPrompt && <UserMessage text={pendingPrompt} userLabel={userLabel} timestamp={timestamp} tocId={activeTurn ? `${activeTurn}:user` : "pending-user"} enter={entering(activeTurn ? `user:${activeTurn}` : "pending-user")} />}
      {(liveSegments.length > 0 || liveText || (state.active_turn && !liveText && !liveTools.length && !timeline.some((item) => item.turn_id !== undefined && item.turn_id === state.active_turn?.turn_id && item.role === "assistant"))) && <div className="turn">
        {liveSegments.map((segment) => segment.type === "steps" && <StepGroup key={segment.key} steps={segment.steps} live={turnRunning} thinkingId={streamingReasoningId(state.active_turn)} sessionId={state.session?.session_id} entering={entering} />)}
        {liveText && <AssistantMessage text={liveText} streaming enter={entering(activeTurn ? `reply:${activeTurn}` : "live-reply")} />}
        {state.active_turn && !liveText && !liveTools.length && !timeline.some((item) => item.turn_id !== undefined && item.turn_id === state.active_turn?.turn_id && item.role === "assistant") && <p className="empty-small working-indicator">{tr("web.working")}</p>}
      </div>}
    </> : <>
      {state.history.map((turn, index) => <Fragment key={turn.turn_id || index}>
        {turn.user && <UserMessage text={turn.user} tocId={turn.turn_id || `history-${index}`} />}
        <div className="turn">
          {turn.tools?.map((tool) => toolCardFromState(tool, state.session?.session_id))}
          {turn.status === "failed" && <p className="form-error">{tr("web.turn_failed")}</p>}
          {turn.status === "cancelled" && <p className="form-error">{tr("web.turn_cancelled")}</p>}
          {turn.assistant ? <AssistantMessage text={turn.assistant} /> : turn.status === "failed" || turn.status === "cancelled" ? null : <p className="empty-small">{tr("web.no_answer")}</p>}
        </div>
      </Fragment>)}
      {state.active_turn && <>
        {state.active_turn.prompt && <UserMessage text={state.active_turn.prompt} tocId={state.active_turn.turn_id ? `${state.active_turn.turn_id}:user` : "active-user"} />}
        <div className="turn">
          {state.active_turn.tools.map((tool) => toolCardFromState(tool, state.session?.session_id, undefined, true))}
          {state.active_turn.reasoning && <ReasoningRow text={state.active_turn.reasoning} live />}
          {state.active_turn.content ? <AssistantMessage text={state.active_turn.content} /> : <p className="empty-small">{tr("web.working")}</p>}
        </div>
      </>}
    </>}
    {state.session.execution === "queued" && <p className="hint queue-reason" role="status">{tr("web.queued_reason", { reason: state.session.queue_reason || tr("web.queued_default") })}</p>}
    {(state.agents ?? []).map((agent) => <section className="agent-progress" key={agent.task_id}><strong>Subagent {agent.task_id} · {agent.status}</strong>{agent.content && <MarkdownContent content={agent.content} />}</section>)}
    {state.notices.filter((notice, index, all) => notice.code !== "notice.context_compact" || !all.slice(index + 1).some((item) => item.code === notice.code)).slice(-5).map((notice) => <div className="system-notice" role="status" key={notice.id}>{present(notice.code, notice.params, notice.text)}</div>)}
    {revealMissed && <p className="hint reveal-missed" role="status">{tr("web.reveal_call_missing")}</p>}
    </div>
    </div>
    </div>
    <ConversationOutline
      items={toc.items}
      activeId={toc.activeId}
      tickTops={toc.tickTops}
      scrollToHeading={toc.scrollToHeading}
      pinned={outlinePinned}
      onPin={setOutlinePinned}
    />
    </div>
    <div className="conversation-main conversation-main-foot">
    {foot}
    </div>
    </div>
    </div>
  </div></RevealCallContext.Provider>;
}
