"""工具调度执行器:把一轮里的若干 tool_calls 跑出结果。

从 Agent 里独立出来,职责单一——查表、钳超时、按原始顺序切并发安全批次、
把异常/超时吞成 ToolResult 占位。它不认识 ReAct 主循环,也不认识
整个 Renderer,只在构造时收一个 on_command_output 回调(execute_command 的流式输出
要从工具内部的 reader 线程往外喷,这是唯一需要的渲染钩子)。

调用 execute 时再从参数注入 on_call / on_result 两个插槽:循环的所有权在执行器
手里,渲染只是从插槽插话——不接也照跑(便于单测)。
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path

from ..core.logger import get_logger
from ..domain.model.agent import CapabilitySnapshot
from ..domain.policy import (
    AccessScope,
    AuthorizationChange,
    InvocationIdentity,
    PermissionApprovalHandler,
    PermissionPolicy,
    PermissionResolution,
    PermissionResolver,
    PermissionSubject,
)
from ..domain.model.session import ToolExecutionTerminal
from ..domain.model.tool import ToolCall, ToolResult
from ..infrastructure.runtime import AuthorizedExecution, ExecutionBackend, ExecutionPath
from ..infrastructure.tools.base import Tool
from ..infrastructure.tools.runtime import ToolCancelledError, ToolRuntime
from ..infrastructure.tools.validation import validate_tool_arguments
from .tool_capabilities import CapabilityAssembly

logger = get_logger(__name__)


@dataclass(frozen=True)
class ToolExecutionOutcome:
    call: ToolCall
    result: ToolResult
    status: ToolExecutionTerminal


@dataclass
class PreparedInvocation:
    """A permission-committed call ready for execution.

    Preparation owns schema/hooks, permission resolution, approval and
    authorization persistence.  Worker threads receive only this immutable-in-
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
    execution: AuthorizedExecution | None = None


class ToolExecutor:
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
        permission_resolver: PermissionResolver | None = None,
        session=None,
        cancellation_check: Callable[[], bool] | None = None,
        allow_background_tasks: bool = True,
        lifecycle=None,
        capability_snapshot: CapabilitySnapshot | None = None,
        execution_journal=None,
        authorization_commit: Callable[[AuthorizationChange], None] | None = None,
    ):
        if tool_timeout <= 0:
            raise ValueError("tool_timeout 必须 > 0")
        self.tool_registry = tool_registry
        self.capability_snapshot = capability_snapshot
        self.execution_journal = execution_journal
        self._active_step_id = ""
        self.tool_timeout = tool_timeout
        self._authorization_commit = authorization_commit
        self.session = session
        self.permission_resolver = permission_resolver or PermissionResolver(
            permission_policy or PermissionPolicy(),
            permission_approval_handler,
        )
        self._apply_assembly(assembly)
        self.cancellation_check = cancellation_check
        self.on_tool_output = on_tool_output
        self.lifecycle = lifecycle
        self.runtime = ToolRuntime(
            capabilities=self.capabilities,
            runtime_resources=assembly.runtime_resources,
            emit_output=on_command_output,
            emit_progress=on_progress,
            notify_background_done=on_shell_task_done,
            allow_background_tasks=allow_background_tasks,
            lifecycle=lifecycle,
        )

    def bind_run(
        self,
        assembly: CapabilityAssembly,
        *,
        authorization_commit: Callable[[AuthorizationChange], None] | None = None,
    ) -> None:
        """Install a freshly assembled capability view for the selected Run."""
        self._active_step_id = ""
        if authorization_commit is not None:
            self._authorization_commit = authorization_commit
        self._apply_assembly(assembly)
        self.runtime = replace(
            self.runtime,
            capabilities=self.capabilities,
            runtime_resources=assembly.runtime_resources,
        )

    def _apply_assembly(self, assembly: CapabilityAssembly) -> None:
        self.capabilities = assembly.capabilities
        self._execution_backend = assembly.backend
        self.workspace_dir = assembly.workspace_dir
        self.cwd_provider = assembly.cwd_provider

    @property
    def backend(self) -> ExecutionBackend:
        return self._execution_backend

    def bind_step(self, step_id: str) -> None:
        """Associate the next batch of tool intents with one ModelStep."""
        self._active_step_id = str(step_id or "")[:300]

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
                # Re-run permission on rewritten arguments below. The hook
                # may not turn an approval for path A into execution of B.
        return ToolCall(tool_call.name, arguments, tool_call.id), None

    def _access_scope(self) -> AccessScope:
        additional = ()
        if self.session is not None:
            snapshot = getattr(self.session, "working_directories_snapshot", None)
            additional = (
                tuple(snapshot())
                if callable(snapshot)
                else tuple(getattr(self.session, "additional_working_directories", ()) or ())
            )
        return AccessScope(self.workspace_dir, additional)

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
        scope = self._access_scope()
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
        permission = self.permission_resolver.resolve(
            effective_call,
            PermissionSubject(
                name=tool.name,
                requires_user_interaction=tool.requires_user_interaction,
                describe_access=tool.describe_access,
                validate=lambda arguments: validate_tool_arguments(tool, arguments),
            ),
            backend=backend,
            scope=scope,
            identity=identity,
            cwd=fixed_cwd,
        )
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
                        "risk_flags": list(permission.risk_flags),
                        "source": permission.source,
                    }
                },
            )
        if permission.grant is None:
            return ToolResult.fail(
                "Permission resolver returned allow without an invocation grant"
            )
        try:
            self._commit_authorization_change(permission.changes)
        except Exception as exc:
            return ToolResult.fail(f"授权保存失败，本次调用未执行: {exc}")

        # A successful directory approval changes the session snapshot.  The
        # grant remains fixed to the resolver's target, while shell/child
        # tools see the new immutable per-invocation scope.
        runtime_scope = self._access_scope()
        authorized: AuthorizedExecution | None = None
        if "execution" in tool.required_capabilities:
            try:
                authorized = AuthorizedExecution(backend, permission.grant)
            except Exception as exc:
                return ToolResult.fail(f"Could not create authorized execution: {exc}")
        runtime = replace(runtime, access_scope=runtime_scope, execution=authorized)
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
            access_scope=runtime_scope,
            concurrency_safe=concurrency_safe,
            approval_wait_ms=approval_wait_ms,
            effective_timeout=effective_timeout,
            local_cancel=local_cancel,
            runtime=runtime,
            execution=authorized,
        )

    def _run_allowed_tool(
        self,
        tool,
        tool_call,
        arguments: dict,
        runtime,
        permission,
        effective_timeout: float,
        on_call_start,
        timings: dict[str, float] | None,
    ):
        # The resolver has already revalidated the final arguments. Keep this
        # execution copy separate from the model's recorded input.
        arguments = dict(arguments)

        # 内层超时必须 ≤ 外层线程预算:模型可以给工具传很大的 timeout,
        # 不钳制的话外层先掐,工具内部的超时机制(如 execute_command 转后台)永远轮不到登场
        if isinstance(arguments.get("timeout"), (int, float)):
            arguments["timeout"] = min(arguments["timeout"], effective_timeout)

        journal = self.execution_journal
        if journal is not None:
            try:
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
                        "workspace_dir": str(self.workspace_dir),
                        "cwd": permission.grant.cwd.value,
                    },
                )
                journal.mark_started(tool_call.id)
            except Exception as exc:
                # Intention is the commit point. Never perform a durable
                # side-effect after failing to record that it is about to run.
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
                # The tool has run, so this must be recoverable as unknown;
                # do not claim a normal result or attempt a second execution.
                return ToolResult.fail(
                    f"durable tool result persistence failed: {exc}",
                    data={"outcome": "unknown"},
                )
        return tool_result

    def _commit_authorization_change(self, change: AuthorizationChange) -> None:
        if change == AuthorizationChange():
            return
        if self._authorization_commit is not None:
            self._authorization_commit(change)
        else:
            if change.persistent_directories or change.persistent_rules:
                raise RuntimeError(
                    "persistent authorization requires an authorization commit callback"
                )
            if self.session is None:
                raise RuntimeError("no session authorization store is configured")
            for directory in change.session_directories:
                self.session.add_working_directory(Path(directory.value))
        commit_resolver_change = getattr(
            self.permission_resolver, "commit_authorization_change", None
        )
        if callable(commit_resolver_change):
            commit_resolver_change(change)

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

    def _run_one(
        self,
        idx: int,
        prepared: PreparedInvocation,
        on_phase: Callable[[ToolCall, str], None] | None = None,
    ) -> tuple[int, ToolExecutionOutcome]:
        tool = prepared.tool
        tool_call = prepared.call
        local_cancel = prepared.local_cancel
        timer: threading.Timer | None = None
        effective_timeout = prepared.effective_timeout
        started = time.monotonic()
        timings: dict[str, float] = {
            "approval_wait_ms": prepared.approval_wait_ms,
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
            if tool.timeout_owner == "executor":
                start_deadline()

        try:
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
        finally:
            if timer is not None:
                timer.cancel()
            if prepared.execution is not None:
                prepared.execution.close()

        if local_cancel.is_set():
            result = ToolResult.fail(
                f"timeout: exceeded {effective_timeout}s; tool observed cancel and exited",
                data=result.data,
            )
            status: ToolExecutionTerminal = "timeout"
        else:
            status = "succeeded" if result.ok else "failed"

        self._emit_lifecycle(
            "post_tool_use" if result.ok else "tool_failure",
            {
                "tool_name": tool_call.name,
                "tool_call_id": tool_call.id,
                "status": status,
                "total_duration_ms": round((time.monotonic() - started) * 1_000, 3),
                "approval_wait_ms": round(timings.get("approval_wait_ms", 0.0), 3),
                "execution_ms": round(timings.get("execution_ms", 0.0), 3),
                "result": result.to_dict(),
            },
        )

        return idx, ToolExecutionOutcome(
            call=tool_call,
            result=result,
            status=status,
        )

    def _run_concurrent_batch(
        self,
        indexed_calls: list[tuple[int, PreparedInvocation]],
        on_result: Callable[[ToolCall, ToolResult], None] | None,
        max_workers: int,
        on_phase: Callable[[ToolCall, str], None] | None = None,
    ) -> dict[int, ToolExecutionOutcome]:
        """并发跑一批 (原始下标, ToolCall),返回 {下标: outcome}。

        线程池而非进程池:工具都是 I/O 密集,等待时释放 GIL,线程足够;
        进程池还要求参数能 pickle,得不偿失。

        保序靠下标:每个 future 记住自己的原始下标,调用方按下标回填,
        无论谁先跑完都不乱——结果要按 tool_call.id 喂回 LLM,顺序错就对不上号。

        on_result 在主线程按完成顺序触发。工具执行已把异常吞成
        ToolResult.fail,单个工具失败被隔离;超时的调用以 fail 占位留在结果里,
        绝不"蒸发"(模型靠 id 对账,少一条都不行)。
        """
        out: dict[int, ToolExecutionOutcome] = {}
        if not indexed_calls:
            return out

        # context manager 会等待已经启动的调用真正退出。Python 线程不能安全强杀，
        # 所以 deadline 通过 ToolRuntime 的取消信号协作完成，绝不遗弃后台线程。
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(self._run_one, idx, invocation, on_phase)
                for idx, invocation in indexed_calls
            ]
            for fut in as_completed(futures):
                idx, outcome = fut.result()
                if on_result:
                    on_result(outcome.call, outcome.result)
                out[idx] = outcome

        return out

    def _partition_original_calls(
        self,
        indexed_calls: list[tuple[int, ToolCall, ToolCall, ToolResult | None]],
    ) -> list[list[tuple[int, ToolCall, ToolCall, ToolResult | None]]]:
        """Partition only on pre-permission arguments.

        A preparation failure remains a barrier.  This prevents a later call
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

        on_call 先按输入顺序全报一遍("这一轮要调这些工具"),再开跑。
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
                    for prepared_idx, outcome in self._run_concurrent_batch(
                        final_batch,
                        on_result,
                        min(max_workers, len(final_batch)),
                        on_phase,
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
