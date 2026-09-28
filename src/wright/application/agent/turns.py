"""Turn outcome handlers.

The handler receives a TurnControl for one decision. It does not hold the
Agent or reach into Agent private state.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...domain.model.llm import UsageRecord
from ...domain.model.llm.events import ContentDone
from ...domain.model.session import Session
from ...domain.model.tool import ToolResult
from ...domain.policy.verifier import Verifier
from ...domain.protocol import build_tool_results_messages
from ...infrastructure.llm.wire import assistant_message
from ..session.events import SessionEvents
from ..tool_execution.dispatch import ToolDispatchService
from .parse import TurnAbort


@dataclass
class RetryCounters:
    invalid: int = 0
    verifier: int = 0
    hook: int = 0


@dataclass
class TurnControl:
    """The collaborators one turn decision is allowed to use."""

    session: Session
    verifier: Verifier | None
    ui: SessionEvents
    executor: ToolDispatchService
    responses: Any
    max_verification_retries: int
    max_consecutive_invalid: int
    record_run_event: Callable[..., None]
    record_usage: Callable[..., None]
    stop_if_cancelled: Callable[[], bool]
    checkpoint: Callable[[], None]
    emit_agent_stop: Callable[..., Any]
    finalize_memory: Callable[..., None]
    terminate: Callable[..., None]


def handle_final_turn(
    control: TurnControl,
    turn,
    content: str,
    usage_record: UsageRecord | None,
    transient_plan_tokens: int,
    counters: RetryCounters,
) -> tuple[str | None, str]:
    """Return (final_answer, outcome): done, retry, terminated, or cancelled."""
    turn_record = control.session.record_assistant_turn(
        assistant_raw=content,
        parsed=turn.parsed,
        assistant_message=turn.assistant_message,
        route="final",
    )
    control.record_run_event(
        "model_step",
        {
            "session_run_id": turn_record.run_id,
            "step_id": turn_record.step_id,
            "route": "final",
            "content": content,
            "parsed": turn.parsed,
            "usage": _usage_payload(usage_record),
        },
        event_key=f"step:{turn_record.step_id}",
    )
    if usage_record is not None:
        control.record_usage(turn_record, usage_record, transient_plan_tokens)
    if control.stop_if_cancelled():
        return None, "cancelled"

    verification = (
        control.verifier.verify(control.session, turn.final_answer)
        if control.verifier
        else None
    )
    if verification is not None:
        control.session.record_verification(
            turn_record,
            verification.approved,
            [issue.to_dict() for issue in verification.issues],
        )
        if not verification.approved:
            counters.verifier += 1
            control.ui.on_completion_rejected(verification.issues)
            control.session.append_message(verification.feedback_message())
            control.checkpoint()
            if counters.verifier >= control.max_verification_retries:
                control.terminate(
                    "failed",
                    reason="completion verification retry limit",
                    message="最终答案连续未通过完成验证，任务终止。",
                )
                return None, "terminated"
            return None, "retry"
        counters.verifier = 0

    # Persist the terminal state before advertising completion.
    control.session.mark_completed()
    control.checkpoint()
    stop_decision = control.emit_agent_stop(
        "completed", final_answer=turn.final_answer
    )
    if stop_decision is not None and stop_decision.decision == "deny":
        counters.hook += 1
        active_run = control.session.active_run()
        if active_run is not None:
            active_run.resume_after_rejection()
        control.session.revoke_turn_commit()
        control.session.append_message({
            "role": "user",
            "content": json.dumps(
                {
                    "error": "agent_stop hook rejected completion",
                    "reason": stop_decision.reason,
                },
                ensure_ascii=False,
            ),
        })
        control.checkpoint()
        if counters.hook >= control.max_verification_retries:
            control.terminate(
                "failed",
                reason="agent_stop hook retry limit",
                message="最终答案连续未通过 lifecycle hook，任务终止。",
            )
            return None, "terminated"
        return None, "retry"
    counters.hook = 0

    control.ui.on_final(turn.final_answer)
    active_run = control.session.active_run()
    if active_run is not None:
        control.responses.finish_response(active_run.run_id)
    control.finalize_memory(turn.final_answer, extract_semantic=True)
    control.checkpoint()
    return turn.final_answer, "done"


def handle_tool_calls_turn(
    control: TurnControl,
    turn,
    content: str,
    usage_record: UsageRecord | None,
    transient_plan_tokens: int,
) -> bool:
    """Return False if the run was cancelled or terminated mid-turn."""
    turn_record = control.session.record_assistant_turn(
        assistant_raw=content,
        parsed=turn.parsed,
        assistant_message=turn.assistant_message,
        route="tool_calls",
        tool_calls=turn.tool_calls,
    )
    control.record_run_event(
        "model_step",
        {
            "session_run_id": turn_record.run_id,
            "step_id": turn_record.step_id,
            "route": "tool_calls",
            "content": content,
            "parsed": turn.parsed,
            "tool_call_ids": [call.id for call in turn.tool_calls],
            "usage": _usage_payload(usage_record),
        },
        event_key=f"step:{turn_record.step_id}",
    )
    if usage_record is not None:
        control.record_usage(turn_record, usage_record, transient_plan_tokens)
    if control.stop_if_cancelled():
        cancelled = []
        for call in turn.tool_calls:
            result = ToolResult.fail("Cancelled before tool execution")
            control.session.record_tool_execution(call.id, result)
            cancelled.append((call, result))
        for message in build_tool_results_messages(cancelled):
            control.session.append_message(message)
        control.checkpoint()
        return False

    control.checkpoint()
    control.executor.bind_step(turn_record.step_id)

    outcomes = control.executor.execute(
        turn.tool_calls,
        on_call=control.ui.on_tool_call,
        on_phase=control.ui.on_tool_phase,
        on_result=control.ui.on_tool_result,
    )

    for outcome in outcomes:
        control.session.record_tool_execution(
            call_id=outcome.call.id,
            result=outcome.result,
            status=outcome.status,
        )

    for message in build_tool_results_messages(
        [(outcome.call, outcome.result) for outcome in outcomes]
    ):
        control.session.append_message(message)
    control.checkpoint()
    if any(
        isinstance(outcome.result.data, dict)
        and outcome.result.data.get("outcome") == "unknown"
        for outcome in outcomes
    ):
        control.terminate(
            "failed",
            reason="tool execution outcome unknown",
            message="工具已执行但结果未能可靠持久化；不会自动重试副作用。",
        )
        return False
    return True


def handle_invalid_turn(
    control: TurnControl,
    response: ContentDone,
    error: TurnAbort,
    usage_record: UsageRecord | None,
    transient_plan_tokens: int,
    counters: RetryCounters,
) -> str:
    """Return retry, terminated, or cancelled."""
    counters.invalid += 1
    turn_record = control.session.record_invalid_turn(
        response.content,
        f"LLM output could not be parsed or routed: {error}",
        parsed={
            "response": assistant_message(response),
            "finish_reason": response.finish_reason,
        },
    )
    control.record_run_event(
        "model_step",
        {
            "session_run_id": turn_record.run_id,
            "step_id": turn_record.step_id,
            "route": "invalid",
            "content": response.content,
            "error": str(error),
            "finish_reason": response.finish_reason,
        },
        event_key=f"step:{turn_record.step_id}",
    )
    if usage_record is not None:
        control.record_usage(turn_record, usage_record, transient_plan_tokens)
    if control.stop_if_cancelled():
        return "cancelled"

    if counters.invalid >= control.max_consecutive_invalid:
        control.terminate(
            "failed",
            reason="invalid output retry limit",
            message=f"连续 {counters.invalid} 轮输出无法解析，任务终止。",
        )
        return "terminated"

    control.session.append_message({
        "role": "user",
        "content": json.dumps(
            {"error": f"LLM output could not be parsed or routed: {error}"},
            ensure_ascii=False,
        ),
    })
    control.checkpoint()
    return "retry"


def _usage_payload(usage_record: UsageRecord | None) -> dict[str, int]:
    if usage_record is None:
        return {}
    return {
        "prompt_tokens": usage_record.prompt_tokens,
        "completion_tokens": usage_record.completion_tokens,
        "total_tokens": usage_record.total_tokens,
    }
