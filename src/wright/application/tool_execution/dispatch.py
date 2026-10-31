"""Tool dispatch application service.

Coordinates tool invocation preparation, schema validation, lifecycle hooks,
permission resolution, authorization persistence, concurrency analysis, and
delegates execution of prepared batches to an IToolExecutor infrastructure engine.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from wright.application.permission.workflow import AuthorizationWorkflow

from ...core.logger import get_logger
from ...domain.gateway.runtime_protocols import AuthorizedExecutionPort, ExecutionBackend
from ...domain.gateway.runtime_types import ExecutionPath
from ...domain.gateway.tool_executor import IToolExecutor
from ...domain.model.agent import CapabilitySnapshot
from ...domain.model.tool import (
    ToolCall,
    ToolExecutionOutcome,
    ToolResult,
)
from ...domain.model.tool_definition import Tool
from ...domain.model.tool_runtime import (
    ToolCancelledError,
    ToolRuntime,
)
from ...domain.policy import (
    AccessScope,
    InvocationIdentity,
    PermissionApprovalHandler,
    PermissionPolicy,
    PermissionResolution,
    PermissionSubject,
)
from .capabilities import CapabilityAssembly
from .executor import ConcurrentToolExecutor
from .validation import validate_tool_arguments

logger = get_logger(__name__)


@dataclass
class PreparedInvocation:
    """A permission-committed call ready for execution.

    Preparation owns schema/hooks, permission resolution, approval and
    authorization persistence. Worker threads receive only this immutable-in-
    practice execution plan; they never ask for approval or reread policy.
    """

    original_call: ToolCall
    call: ToolCall
    tool: Tool
    final_arguments: dict
    resolution: PermissionResolution
    cwd: ExecutionPath
    access_scope: AccessScope
    concurrency_safe: bool
    approval_wait_ms: float
    effective_timeout: float
    local_cancel: threading.Event
    runtime: ToolRuntime
    # Bound to this invocation; None when nothing was granted.
    execution: AuthorizedExecutionPort | None = None
    before_execute: Callable[[], None] | None = None


class ToolDispatchService:
    """Application service for gating, preparing, and scheduling tool executions."""

    def __init__(
        self,
        tool_registry: dict[str, Tool],
        assembly: CapabilityAssembly,
        tool_timeout: float = 30,
        on_command_output: Callable[[str], None] | None = None,
        on_tool_output: Callable[[str, str], None] | None = None,
        on_progress: Callable[[dict], None] | None = None,
        on_shell_task_done: Callable[[str], None] | None = None,
        permission_policy: PermissionPolicy | None = None,
        permission_approval_handler: PermissionApprovalHandler | None = None,
        permission_workflow: AuthorizationWorkflow | None = None,
        session=None,
        cancellation_check: Callable[[], bool] | None = None,
        allow_background_tasks: bool = True,
        lifecycle=None,
        capability_snapshot: CapabilitySnapshot | None = None,
        execution_journal=None,
        executor: IToolExecutor | None = None,
    ):
        if tool_timeout <= 0:
            raise ValueError("tool_timeout 必须 > 0")
        self.tool_registry = tool_registry
        self.capability_snapshot = capability_snapshot
        self.execution_journal = execution_journal
        self._active_step_id = ""
        self.tool_timeout = tool_timeout
        self.session = session
        self.permission_workflow = permission_workflow or AuthorizationWorkflow(
            permission_policy or PermissionPolicy(),
            permission_approval_handler,
        )
        self._apply_assembly(assembly)
        self.cancellation_check = cancellation_check
        self.on_tool_output = on_tool_output
        self.lifecycle = lifecycle
        def _shell_finished(command_id: str) -> None:
            journal = getattr(self.runtime, "file_journal", None)
            if journal is not None:
                try:
                    journal.finish_held(command_id)
                except Exception:
                    logger.exception("change journal could not close %s", command_id)
            if on_shell_task_done is not None:
                on_shell_task_done(command_id)

        self.runtime = ToolRuntime(
            capabilities=self.capabilities,
            emit_output=on_command_output,
            emit_progress=on_progress,
            notify_background_done=_shell_finished,
            allow_background_tasks=allow_background_tasks,
            lifecycle=lifecycle,
        )
        if session is not None and getattr(session, "workspace_dir", None):
            from ..workspace.journal import SessionChangeJournal

            self.runtime = replace(
                self.runtime,
                file_journal=SessionChangeJournal(session),
            )
        self.executor = executor or ConcurrentToolExecutor(journal=execution_journal)

    def bind_run(
        self,
        assembly: CapabilityAssembly,
    ) -> None:
        """Install a freshly assembled capability view for the selected Run."""
        self._active_step_id = ""
        self._apply_assembly(assembly)
        self.runtime = replace(
            self.runtime,
            capabilities=self.capabilities,
        )

    def _apply_assembly(self, assembly: CapabilityAssembly) -> None:
        self.capabilities = assembly.capabilities
        self._execution_backend = assembly.backend
        self.workspace_dir = assembly.workspace_dir
        self.cwd_provider = assembly.cwd_provider
        self.permissions = assembly.permissions

    @property
    def backend(self) -> ExecutionBackend:
        return self._execution_backend

    def bind_step(self, step_id: str) -> None:
        """Associate the next batch of tool intents with one ModelStep."""
        self._active_step_id = str(step_id or "")[:300]
        if hasattr(self.executor, "bind_step"):
            self.executor.bind_step(self._active_step_id)

    def _emit_lifecycle(self, event: str, payload: dict):
        if self.lifecycle is None:
            return None
        return self.lifecycle.emit(
            event,
            payload,
            agent_task_id=self.capabilities.scope.agent_task_id,
            root_turn_id=self.capabilities.scope.root_turn_id,
        )

    def _prepare_tool_call(
        self, tool_call: ToolCall
    ) -> tuple[ToolCall, ToolResult | None]:
        """Run schema + pre-tool hooks before concurrency is classified.

        The returned call is an execution-only copy. Session has already
        recorded the model's original input, so hook rewrites remain observable
        without mutating history.
        """
        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            return tool_call, ToolResult.fail(err=f"Unknown tool: {tool_call.name}")
        from ..workspace.modes import tool_visible

        if not tool_visible(tool.name, self.session):
            return tool_call, ToolResult.fail(
                f"Interaction mode blocks {tool.name}",
                data={"reason_code": "permission.ask_denied"},
            )
        if self.capability_snapshot is not None and tool_call.name not in self.capability_snapshot.names:
            return tool_call, ToolResult.fail(
                f"Capability is not authorized for this Run: {tool_call.name}"
            )
        validation_error = validate_tool_arguments(tool, tool_call.arguments)
        if validation_error is not None:
            return tool_call, validation_error

        arguments = dict(tool_call.arguments)
        hook_decision = self._emit_lifecycle(
            "pre_tool_use",
            {
                "tool_name": tool_call.name,
                "tool_call_id": tool_call.id,
                "arguments": arguments,
                "cwd": str(self._current_cwd()),
            },
        )
        if hook_decision is not None:
            if hook_decision.decision == "deny":
                return tool_call, ToolResult.fail(
                    f"Hook denied: {hook_decision.reason}",
                    data={
                        "hook": {
                            "decision": "deny",
                            "reason": hook_decision.reason,
                        }
                    },
                )
            if hook_decision.updated_input is not None:
                arguments = dict(hook_decision.updated_input)
                updated_validation_error = validate_tool_arguments(tool, arguments)
                if updated_validation_error is not None:
                    return (
                        ToolCall(tool_call.name, arguments, tool_call.id),
                        updated_validation_error,
                    )
        return ToolCall(tool_call.name, arguments, tool_call.id), None

    def _access_scope(self) -> AccessScope:
        if self._managed_permissions():
            return self.permissions.snapshot(self.session).scope
        from ...infrastructure.config.permission_store import protected_permission_paths
        return AccessScope(self.workspace_dir, protected=protected_permission_paths(self.workspace_dir))

    def _managed_permissions(self) -> bool:
        return self.permissions is not None and self.session is not None

    def _runtime_for_preparation(
        self,
        tool: Tool,
        tool_call: ToolCall,
        local_cancel: threading.Event,
        scope: AccessScope,
    ) -> ToolRuntime:
        return replace(
            self.runtime,
            tool_name=tool_call.name,
            tool_call_id=tool_call.id,
            capabilities=self.capabilities.restricted(tool.required_capabilities),
            access_scope=scope,
            cancellation_check=lambda: local_cancel.is_set()
            or bool(self.cancellation_check and self.cancellation_check()),
            cancellation_reason=lambda: (
                "timeout"
                if local_cancel.is_set()
                else (
                    "parent_cancelled"
                    if self.cancellation_check and self.cancellation_check()
                    else ""
                )
            ),
            emit_output=(
                (lambda line: self.on_tool_output(tool_call.id, line))
                if self.on_tool_output is not None
                else self.runtime.emit_output
            ),
        )

    def _prepare_invocation(
        self,
        original_call: ToolCall,
        effective_call: ToolCall,
        tool: Tool,
    ) -> PreparedInvocation | ToolResult:
        """Resolve, approve and commit one call before any worker is started."""
        local_cancel = threading.Event()
        try:
            scope = self._access_scope()
        except Exception as exc:
            return ToolResult.fail(f"Permission store could not be read: {exc}")
        backend = self._execution_backend
        if backend is None:
            return ToolResult.fail("No execution backend is configured")
        runtime = self._runtime_for_preparation(tool, effective_call, local_cancel, scope)
        try:
            runtime.raise_if_cancelled()
        except ToolCancelledError as exc:
            return ToolResult.fail(str(exc))

        permission_started = time.monotonic()
        fixed_cwd = backend.cwd()
        identity = InvocationIdentity(
            self.capabilities.scope.session_id,
            self.capabilities.scope.run_id,
            effective_call.id,
            self.capabilities.scope.agent_task_id,
        )
        try:
            invocation = self.permissions.authorize(self.session, effective_call,
                PermissionSubject(tool.name, tool.requires_user_interaction, tool.describe_access,
                    lambda arguments: validate_tool_arguments(tool, arguments), tool.approval_details), backend=backend,
                workflow=self.permission_workflow, cwd=fixed_cwd, scope=scope, identity=identity,
                requires_execution="execution" in tool.required_capabilities,
                requires_network="network" in tool.required_capabilities)
            permission = invocation.resolution
        except Exception as exc:
            return ToolResult.fail(f"Could not authorize invocation: {exc}", data={"display_code": "permission.auth_save_failed", "display_params": {"error": str(exc)}})
        approval_wait_ms = (time.monotonic() - permission_started) * 1_000
        self._emit_lifecycle("permission_decision", {
            "tool_name": effective_call.name,
            "tool_call_id": effective_call.id,
            "decision": permission.decision,
            "reason": permission.reason,
            "risk_flags": list(permission.risk_flags),
            "source": permission.source,
            "approval_wait_ms": round(approval_wait_ms, 3),
        })
        if permission.decision != "allow":
            return ToolResult.fail(
                err=f"Permission denied: {permission.reason}",
                data={
                    "permission": {
                        "decision": permission.decision,
                        "reason": permission.reason,
                        "reason_code": permission.reason_code,
                        "reason_params": dict(permission.reason_params),
                        "risk_flags": list(permission.risk_flags),
                        "source": permission.source,
                    },
                    "display_code": permission.reason_code,
                    "display_params": dict(permission.reason_params),
                },
            )
        if permission.grant is None:
            return ToolResult.fail(
                "Permission resolver returned allow without an invocation grant"
            )
        authorized = invocation.execution
        validate_permission = invocation.validator
        runtime = replace(runtime, access_scope=invocation.scope, execution=authorized, network=invocation.network)
        final_arguments = dict(permission.final_arguments)
        try:
            concurrency_safe = bool(tool.is_concurrency_safe(final_arguments))
        except Exception:
            concurrency_safe = False
        effective_timeout = (
            tool.execution_timeout
            if tool.execution_timeout is not None
            else self.tool_timeout
        )
        if effective_timeout <= 0:
            effective_timeout = self.tool_timeout
        return PreparedInvocation(
            original_call=original_call,
            call=effective_call,
            tool=tool,
            final_arguments=final_arguments,
            resolution=permission,
            cwd=permission.grant.cwd,
            access_scope=invocation.scope,
            concurrency_safe=concurrency_safe,
            approval_wait_ms=approval_wait_ms,
            effective_timeout=effective_timeout,
            local_cancel=local_cancel,
            runtime=runtime,
            execution=authorized,
            before_execute=validate_permission,
        )

    def _current_cwd(self) -> Path:
        try:
            return self.cwd_provider().resolve()
        except Exception:
            logger.debug("cwd_provider failed; falling back to workspace_dir", exc_info=True)
            return self.workspace_dir

    def _is_concurrency_safe(self, tool_call: ToolCall) -> bool:
        """按本次参数判断能否并发；未知/判断异常一律按排他执行。"""
        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            return False
        try:
            return bool(tool.is_concurrency_safe(dict(tool_call.arguments)))
        except Exception:
            return False

    def _partition_original_calls(
        self,
        indexed_calls: list[tuple[int, ToolCall, ToolCall, ToolResult | None]],
    ) -> list[list[tuple[int, ToolCall, ToolCall, ToolResult | None]]]:
        """Partition only on pre-permission arguments.

        A preparation failure remains a barrier. This prevents a later call
        from being merged into an earlier original batch merely because the
        failed call was removed from the runnable list.
        """
        batches: list[list[tuple[int, ToolCall, ToolCall, ToolResult | None]]] = []
        previous_safe = False
        for item in indexed_calls:
            _, _, effective_call, error = item
            safe = error is None and self._is_concurrency_safe(effective_call)
            if safe and batches and previous_safe:
                batches[-1].append(item)
            else:
                batches.append([item])
            previous_safe = safe
        return batches

    @staticmethod
    def _partition_prepared_calls(
        invocations: list[tuple[int, PreparedInvocation]],
    ) -> list[list[tuple[int, PreparedInvocation]]]:
        """Partition a single original batch using final approved arguments."""
        batches: list[list[tuple[int, PreparedInvocation]]] = []
        previous_safe = False
        for item in invocations:
            safe = item[1].concurrency_safe
            if safe and batches and previous_safe:
                batches[-1].append(item)
            else:
                batches.append([item])
            previous_safe = safe
        return batches

    def execute(
        self,
        tool_calls: list[ToolCall],
        on_call: Callable[[ToolCall], None] | None = None,
        on_result: Callable[[ToolCall, ToolResult], None] | None = None,
        on_phase: Callable[[ToolCall, str], None] | None = None,
        max_workers: int = 8,
    ) -> list[ToolExecutionOutcome]:
        """保持调用顺序切批执行,返回顺序恒等于输入。

        连续 concurrency-safe 调用并发；每个不安全调用独占一个批次。
        因此 `[read, read, write, read]` 是 `[read+read] → [write] → [read]`，
        不会把后面的 read 提前到 write 前面。
        """
        slots: list[ToolExecutionOutcome | None] = [None] * len(tool_calls)

        if max_workers < 1:
            raise ValueError("max_workers 必须 >= 1")

        prepared: list[tuple[int, ToolCall, ToolCall, ToolResult | None]] = []
        for idx, tool_call in enumerate(tool_calls):
            effective_call, error = self._prepare_tool_call(tool_call)
            prepared.append((idx, tool_call, effective_call, error))

        if on_call:
            for _, _, effective_call, _ in prepared:
                on_call(effective_call)

        def record_failure(idx: int, call: ToolCall, result: ToolResult) -> None:
            outcome = ToolExecutionOutcome(call=call, result=result, status="failed")
            slots[idx] = outcome
            self._emit_lifecycle(
                "tool_failure",
                {
                    "tool_name": call.name,
                    "tool_call_id": call.id,
                    "status": "failed",
                    "duration_ms": 0,
                    "result": result.to_dict(),
                },
            )
            if on_result:
                on_result(call, result)

        def handle_outcome(outcome: ToolExecutionOutcome, metrics: dict[str, float]) -> None:
            self._emit_lifecycle(
                "post_tool_use" if outcome.result.ok else "tool_failure",
                {
                    "tool_name": outcome.call.name,
                    "tool_call_id": outcome.call.id,
                    "status": outcome.status,
                    "total_duration_ms": metrics.get("total_duration_ms", 0.0),
                    "approval_wait_ms": metrics.get("approval_wait_ms", 0.0),
                    "execution_ms": metrics.get("execution_ms", 0.0),
                    "result": outcome.result.to_dict(),
                },
            )

        for batch in self._partition_original_calls(prepared):
            prepared_batch: list[
                tuple[int, PreparedInvocation | None, ToolCall, ToolResult | None]
            ] = []
            for idx, original_call, effective_call, error in batch:
                if error is not None:
                    prepared_batch.append((idx, None, effective_call, error))
                    continue
                tool = self.tool_registry.get(effective_call.name)
                if tool is None:
                    prepared_batch.append(
                        (
                            idx,
                            None,
                            effective_call,
                            ToolResult.fail(err=f"Unknown tool: {effective_call.name}"),
                        )
                    )
                    continue
                prepared_invocation = self._prepare_invocation(
                    original_call, effective_call, tool
                )
                if isinstance(prepared_invocation, ToolResult):
                    prepared_batch.append(
                        (idx, None, effective_call, prepared_invocation)
                    )
                else:
                    prepared_batch.append((idx, prepared_invocation, effective_call, None))

            final_invocations: list[tuple[int, PreparedInvocation]] = []

            def run_final_segment() -> None:
                nonlocal final_invocations
                for final_batch in self._partition_prepared_calls(final_invocations):
                    for prepared_idx, outcome in self.executor.execute_batch(
                        final_batch,
                        max_workers=min(max_workers, len(final_batch)),
                        on_result=on_result,
                        on_phase=on_phase,
                        on_outcome=handle_outcome,
                    ).items():
                        slots[prepared_idx] = outcome
                final_invocations = []

            for idx, prepared_invocation, effective_call, error in prepared_batch:
                if error is not None:
                    run_final_segment()
                    record_failure(idx, effective_call, error)
                    continue
                assert prepared_invocation is not None
                final_invocations.append((idx, prepared_invocation))
            run_final_segment()

        return [slot for slot in slots if slot is not None]


__all__ = ["PreparedInvocation", "ToolDispatchService", "ToolExecutionOutcome"]
