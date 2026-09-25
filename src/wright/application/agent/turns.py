"""Turn outcome handlers and routing logic for the Agent execution loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...domain.model.session import UsageRecord
from ...domain.model.events import ContentDone
from ...domain.protocol import TurnAbort
from ...domain.model.tool import ToolResult
from ...utils.util import build_tool_results_messages

if TYPE_CHECKING:
    from .runner import Agent


@dataclass
class RetryCounters:
    invalid: int = 0
    verifier: int = 0
    hook: int = 0


class AgentTurnHandler:
    """Dispatches and processes outcomes for each model turn (final, tool calls, or invalid)."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    def handle_final_turn(
        self,
        turn,
        content: str,
        usage_record: UsageRecord | None,
        transient_plan_tokens: int,
        counters: RetryCounters,
        *,
        record_memory: bool,
    ) -> tuple[str | None, str]:
        """Return (final_answer, outcome) where outcome is
        'done' | 'retry' | 'terminated' | 'cancelled'."""
        agent = self.agent
        turn_record = agent.session_state.record_assistant_turn(
            assistant_raw=content,
            parsed=turn.parsed,
            assistant_message=turn.assistant_message,
            route="final",
        )
        agent._record_run_event(
            "model_step",
            {
                "session_run_id": turn_record.run_id,
                "step_id": turn_record.step_id,
                "route": "final",
                "content": content,
                "parsed": turn.parsed,
                "usage": (
                    {
                        "prompt_tokens": usage_record.prompt_tokens,
                        "completion_tokens": usage_record.completion_tokens,
                        "total_tokens": usage_record.total_tokens,
                    }
                    if usage_record is not None
                    else {}
                ),
            },
            event_key=f"step:{turn_record.step_id}",
        )
        if usage_record is not None:
            agent._record_usage_for_turn(
                turn_record, usage_record, transient_plan_tokens
            )
        if agent._stop_if_cancelled(record_memory=record_memory):
            return None, "cancelled"

        verification = (
            agent.verifier.verify(agent.session_state, turn.final_answer)
            if agent.verifier
            else None
        )
        if verification is not None:
            agent.session_state.record_verification(
                turn_record,
                verification.approved,
                [issue.to_dict() for issue in verification.issues],
            )
            if not verification.approved:
                counters.verifier += 1
                agent.ui.on_completion_rejected(verification.issues)
                agent.session_state.append_message(
                    verification.feedback_message()
                )
                agent._checkpoint()
                if counters.verifier >= agent.max_verification_retries:
                    agent._terminate(
                        "failed",
                        reason="completion verification retry limit",
                        message="最终答案连续未通过完成验证，任务终止。",
                        record_memory=record_memory,
                    )
                    return None, "terminated"
                return None, "retry"
            counters.verifier = 0

        # Persist the terminal state before advertising completion.
        agent.session_state.mark_completed()
        agent._checkpoint()
        stop_decision = agent._emit_agent_stop(
            "completed", final_answer=turn.final_answer
        )
        if stop_decision is not None and stop_decision.decision == "deny":
            counters.hook += 1
            active_run = agent.session_state.active_run()
            if active_run is not None:
                active_run.resume_after_rejection()
            agent.session_state.revoke_turn_commit()
            agent.session_state.append_message({
                "role": "user",
                "content": json.dumps(
                    {
                        "error": "agent_stop hook rejected completion",
                        "reason": stop_decision.reason,
                    },
                    ensure_ascii=False,
                ),
            })
            agent._checkpoint()
            if counters.hook >= agent.max_verification_retries:
                agent._terminate(
                    "failed",
                    reason="agent_stop hook retry limit",
                    message="最终答案连续未通过 lifecycle hook，任务终止。",
                    record_memory=record_memory,
                )
                return None, "terminated"
            return None, "retry"
        counters.hook = 0

        agent.ui.on_final(turn.final_answer)
        active_run = agent.session_state.active_run()
        if active_run is not None:
            agent.runtime_resources.finish_response(active_run.run_id)
        if (
            record_memory
            and not agent._has_live_agent_tasks(
                agent.session_state.agent_root_turn_id
            )
        ):
            agent._finalize_memory(
                turn.final_answer, extract_semantic=True
            )
        agent._checkpoint()
        return turn.final_answer, "done"

    def handle_tool_calls_turn(
        self,
        turn,
        content: str,
        usage_record: UsageRecord | None,
        transient_plan_tokens: int,
        *,
        record_memory: bool,
    ) -> bool:
        """Return False if the run was cancelled mid-turn."""
        agent = self.agent
        turn_record = agent.session_state.record_assistant_turn(
            assistant_raw=content,
            parsed=turn.parsed,
            assistant_message=turn.assistant_message,
            route="tool_calls",
            tool_calls=turn.tool_calls,
        )
        agent._record_run_event(
            "model_step",
            {
                "session_run_id": turn_record.run_id,
                "step_id": turn_record.step_id,
                "route": "tool_calls",
                "content": content,
                "parsed": turn.parsed,
                "tool_call_ids": [call.id for call in turn.tool_calls],
                "usage": (
                    {
                        "prompt_tokens": usage_record.prompt_tokens,
                        "completion_tokens": usage_record.completion_tokens,
                        "total_tokens": usage_record.total_tokens,
                    }
                    if usage_record is not None
                    else {}
                ),
            },
            event_key=f"step:{turn_record.step_id}",
        )
        if usage_record is not None:
            agent._record_usage_for_turn(
                turn_record, usage_record, transient_plan_tokens
            )
        if agent._stop_if_cancelled(record_memory=record_memory):
            cancelled = []
            for call in turn.tool_calls:
                result = ToolResult.fail("Cancelled before tool execution")
                agent.session_state.record_tool_execution(call.id, result)
                cancelled.append((call, result))
            for message in build_tool_results_messages(cancelled):
                agent.session_state.append_message(message)
            agent._checkpoint()
            return False

        agent._checkpoint()
        agent.executor.bind_step(turn_record.step_id)

        outcomes = agent.executor.execute(
            turn.tool_calls,
            on_call=agent.ui.on_tool_call,
            on_phase=agent.ui.on_tool_phase,
            on_result=agent.ui.on_tool_result,
        )

        for outcome in outcomes:
            agent.session_state.record_tool_execution(
                call_id=outcome.call.id,
                result=outcome.result,
                status=outcome.status,
            )

        for message in build_tool_results_messages(
            [(outcome.call, outcome.result) for outcome in outcomes]
        ):
            agent.session_state.append_message(message)
        agent._checkpoint()
        if any(
            isinstance(outcome.result.data, dict)
            and outcome.result.data.get("outcome") == "unknown"
            for outcome in outcomes
        ):
            agent._terminate(
                "failed",
                reason="tool execution outcome unknown",
                message="工具已执行但结果未能可靠持久化；不会自动重试副作用。",
                record_memory=record_memory,
            )
            return False
        return True

    def handle_invalid_turn(
        self,
        response: ContentDone,
        error: TurnAbort,
        usage_record: UsageRecord | None,
        transient_plan_tokens: int,
        counters: RetryCounters,
        *,
        record_memory: bool,
    ) -> str:
        """Return 'retry' | 'terminated' | 'cancelled'."""
        agent = self.agent
        counters.invalid += 1
        turn_record = agent.session_state.record_invalid_turn(
            response.content,
            f"LLM output could not be parsed or routed: {error}",
            parsed={
                "response": response.assistant_message(),
                "finish_reason": response.finish_reason,
            },
        )
        agent._record_run_event(
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
            agent._record_usage_for_turn(
                turn_record, usage_record, transient_plan_tokens
            )
        if agent._stop_if_cancelled(record_memory=record_memory):
            return "cancelled"

        if counters.invalid >= agent.max_consecutive_invalid:
            agent._terminate(
                "failed",
                reason="invalid output retry limit",
                message=(
                    f"连续 {counters.invalid} 轮输出无法解析，任务终止。"
                ),
                record_memory=record_memory,
            )
            return "terminated"

        agent.session_state.append_message({
            "role": "user",
            "content": json.dumps(
                {"error": f"LLM output could not be parsed or routed: {error}"},
                ensure_ascii=False,
            ),
        })
        agent._checkpoint()
        return "retry"
