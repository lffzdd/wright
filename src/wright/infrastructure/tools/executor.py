"""Concurrent tool execution engine (Infrastructure).

Implements IToolExecutor port: runs prepared tool invocations in a thread pool,
enforces execution deadlines via cooperative cancellation timers, and records
intents/results into durable execution journals.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ...core.logger import get_logger
from ...domain.gateway.tool_executor import IToolExecutor
from ...domain.model.tool import (
    ToolCall,
    ToolExecutionOutcome,
    ToolExecutionStatus,
    ToolResult,
)
from .runtime import ToolCancelledError

logger = get_logger(__name__)


class ConcurrentToolExecutor(IToolExecutor):
    """ThreadPool-based tool execution engine."""

    def __init__(self, journal: Any = None) -> None:
        self.journal = journal
        self._active_step_id: str = ""

    def bind_step(self, step_id: str) -> None:
        """Associate subsequent tool runs with an execution step."""
        self._active_step_id = str(step_id or "")[:300]

    def _run_allowed_tool(
        self,
        tool: Any,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        runtime: Any,
        permission: Any,
        effective_timeout: float,
        on_call_start: Callable[[], None] | None,
        timings: dict[str, float] | None,
    ) -> ToolResult:
        arguments = dict(arguments)
        if isinstance(arguments.get("timeout"), (int, float)):
            arguments["timeout"] = min(arguments["timeout"], effective_timeout)

        journal = self.journal
        if journal is not None:
            try:
                workspace_dir = str(getattr(runtime, "workspace_dir", ""))
                cwd_val = getattr(getattr(permission, "grant", None), "cwd", None)
                cwd_str = cwd_val.value if cwd_val is not None else ""
                journal.record_intent(
                    call_id=tool_call.id,
                    tool_name=tool_call.name,
                    step_id=self._active_step_id,
                    arguments=arguments,
                    permission={
                        "decision": permission.decision,
                        "reason": permission.reason,
                        "source": permission.source,
                    },
                    environment={
                        "workspace_dir": workspace_dir,
                        "cwd": cwd_str,
                    },
                )
                journal.mark_started(tool_call.id)
            except Exception as exc:
                return ToolResult.fail(f"durable tool intent persistence failed: {exc}")

        execution_started: float | None = None
        try:
            runtime.raise_if_cancelled()
            if on_call_start is not None:
                on_call_start()
            execution_started = time.monotonic()
            tool_result = tool.call(arguments, runtime)
        except ToolCancelledError as e:
            tool_result = ToolResult.fail(str(e))
        except Exception as e:
            tool_result = ToolResult.fail(f"{type(e).__name__}: {e}")
        finally:
            if timings is not None:
                timings["execution_ms"] = (
                    (time.monotonic() - execution_started) * 1_000
                    if execution_started is not None
                    else 0.0
                )

        if journal is not None:
            try:
                journal.record_result(
                    tool_call.id,
                    tool_result.to_dict(),
                    status="succeeded" if tool_result.ok else "failed",
                )
            except Exception as exc:
                return ToolResult.fail(
                    f"durable tool result persistence failed: {exc}",
                    data={"outcome": "unknown"},
                )
        return tool_result

    def _run_one(
        self,
        idx: int,
        prepared: Any,
        on_phase: Callable[[ToolCall, str], None] | None = None,
        on_outcome: Callable[[ToolExecutionOutcome, dict[str, float]], None] | None = None,
    ) -> tuple[int, ToolExecutionOutcome]:
        tool = prepared.tool
        tool_call = prepared.call
        local_cancel = prepared.local_cancel
        timer: threading.Timer | None = None
        effective_timeout = prepared.effective_timeout
        started = time.monotonic()
        timings: dict[str, float] = {
            "approval_wait_ms": getattr(prepared, "approval_wait_ms", 0.0),
        }

        def start_deadline() -> None:
            nonlocal timer
            timer = threading.Timer(effective_timeout, local_cancel.set)
            timer.daemon = True
            timer.start()

        def start_execution() -> None:
            if on_phase is not None:
                try:
                    on_phase(tool_call, "running")
                except Exception:
                    logger.debug("tool phase observer failed", exc_info=True)
            if getattr(tool, "timeout_owner", "executor") == "executor":
                start_deadline()

        try:
            if getattr(prepared, "before_execute", None) is not None:
                prepared.before_execute()
            result = self._run_allowed_tool(
                tool,
                tool_call,
                prepared.final_arguments,
                prepared.runtime,
                prepared.resolution,
                effective_timeout,
                start_execution,
                timings,
            )
        except Exception as exc:
            result = ToolResult.fail(f"Permission validation failed: {exc}")
        finally:
            if timer is not None:
                timer.cancel()
            if getattr(prepared, "execution", None) is not None:
                prepared.execution.close()

        if local_cancel.is_set():
            result = ToolResult.fail(
                f"timeout: exceeded {effective_timeout}s; tool observed cancel and exited",
                data=result.data,
            )
            status: ToolExecutionStatus = "timeout"
        else:
            status = "succeeded" if result.ok else "failed"

        outcome = ToolExecutionOutcome(
            call=tool_call,
            result=result,
            status=status,
        )

        if on_outcome is not None:
            metrics = {
                "total_duration_ms": round((time.monotonic() - started) * 1_000, 3),
                "approval_wait_ms": round(timings.get("approval_wait_ms", 0.0), 3),
                "execution_ms": round(timings.get("execution_ms", 0.0), 3),
            }
            try:
                on_outcome(outcome, metrics)
            except Exception:
                logger.debug("on_outcome callback failed", exc_info=True)

        return idx, outcome

    def execute_batch(
        self,
        indexed_invocations: Sequence[tuple[int, Any]],
        max_workers: int = 8,
        on_result: Callable[[ToolCall, ToolResult], None] | None = None,
        on_phase: Callable[[ToolCall, str], None] | None = None,
        on_outcome: Callable[[ToolExecutionOutcome, dict[str, float]], None] | None = None,
    ) -> dict[int, ToolExecutionOutcome]:
        out: dict[int, ToolExecutionOutcome] = {}
        if not indexed_invocations:
            return out

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(self._run_one, idx, invocation, on_phase, on_outcome)
                for idx, invocation in indexed_invocations
            ]
            for fut in as_completed(futures):
                idx, outcome = fut.result()
                if on_result:
                    on_result(outcome.call, outcome.result)
                out[idx] = outcome

        return out


# Infrastructure canonical name
ToolExecutor = ConcurrentToolExecutor

__all__ = ["ConcurrentToolExecutor", "ToolExecutor"]
