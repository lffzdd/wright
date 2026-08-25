"""Thread-safe control plane for the local Agent task tree.

The control plane owns orchestration state, not execution. ``subagent.py`` runs
child Agents; this module gives every child a stable identity, lifecycle,
shared budgets, cancellation propagation, checkpointable state, and a compact
tree view. Child transcripts remain isolated inside their own SessionState.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import threading
import time
from typing import Any, Callable, Literal


AgentTaskStatus = Literal[
    "pending", "running", "completed", "failed", "cancelled", "timed_out"
]
TERMINAL_AGENT_TASK_STATUSES = frozenset({
    "completed", "failed", "cancelled", "timed_out"
})


class AgentControlError(ValueError):
    pass


@dataclass(frozen=True)
class AgentControlConfig:
    max_depth: int = 2
    max_children_per_parent: int = 8
    max_tasks_per_turn: int = 32
    max_stored_tasks: int = 256
    max_concurrent_tasks: int = 8
    max_steps_per_turn: int = 120
    max_tokens_per_turn: int = 500_000
    max_result_chars: int = 8_000

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise AgentControlError(f"{name} 必须是正整数")
        ceilings = {
            "max_depth": 8,
            "max_children_per_parent": 64,
            "max_tasks_per_turn": 1_000,
            "max_stored_tasks": 10_000,
            "max_concurrent_tasks": 64,
            "max_steps_per_turn": 10_000,
            "max_tokens_per_turn": 50_000_000,
            "max_result_chars": 20_000,
        }
        for name, ceiling in ceilings.items():
            if getattr(self, name) > ceiling:
                raise AgentControlError(f"{name} 不能超过 {ceiling}")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_depth": self.max_depth,
            "max_children_per_parent": self.max_children_per_parent,
            "max_tasks_per_turn": self.max_tasks_per_turn,
            "max_stored_tasks": self.max_stored_tasks,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "max_steps_per_turn": self.max_steps_per_turn,
            "max_tokens_per_turn": self.max_tokens_per_turn,
            "max_result_chars": self.max_result_chars,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "AgentControlConfig":
        if not isinstance(value, dict):
            raise AgentControlError("agent control config 必须是对象")
        defaults = cls()
        return cls(**{
            name: value.get(name, getattr(defaults, name))
            for name in defaults.to_dict()
        })


@dataclass
class AgentTaskRecord:
    id: str
    root_turn_id: str
    parent_id: str | None
    tool_call_id: str
    depth: int
    task: str
    status: AgentTaskStatus
    created_at: float
    started_at: float | None = None
    ended_at: float | None = None
    child_session_id: str | None = None
    step_budget: int = 0
    steps_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    result: str = ""
    error: str = ""
    cancel_requested: bool = False
    cancel_reason: str = ""
    children: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "root_turn_id": self.root_turn_id,
            "parent_id": self.parent_id,
            "tool_call_id": self.tool_call_id,
            "depth": self.depth,
            "task": self.task,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "child_session_id": self.child_session_id,
            "step_budget": self.step_budget,
            "steps_used": self.steps_used,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            },
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "cancel_reason": self.cancel_reason,
            "children": list(self.children),
        }

    def summary_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "task": self.task[:300],
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "child_session_id": self.child_session_id,
            "step_budget": self.step_budget,
            "steps_used": self.steps_used,
            "total_tokens": self.total_tokens,
            "result": self.result[:500],
            "error": self.error[:500],
            "cancel_requested": self.cancel_requested,
            "children": list(self.children),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "AgentTaskRecord":
        if not isinstance(value, dict):
            raise AgentControlError("agent task 必须是对象")
        status = value.get("status")
        if status not in {
            "pending", "running", "completed", "failed", "cancelled", "timed_out"
        }:
            raise AgentControlError("agent task status 非法")
        usage = value.get("usage", {})
        if not isinstance(usage, dict):
            raise AgentControlError("agent task usage 必须是对象")
        children = value.get("children", [])
        if not isinstance(children, list) or not all(
            isinstance(child, str) for child in children
        ):
            raise AgentControlError("agent task children 必须是字符串数组")
        parent_id = value.get("parent_id")
        if parent_id is not None and not isinstance(parent_id, str):
            raise AgentControlError("agent task parent_id 非法")
        cancel_requested = value.get("cancel_requested", False)
        if not isinstance(cancel_requested, bool):
            raise AgentControlError("agent task cancel_requested 必须是 boolean")
        record = cls(
            id=_bounded_string(value.get("id"), "id", 80),
            root_turn_id=_bounded_string(
                value.get("root_turn_id"), "root_turn_id", 180
            ),
            parent_id=parent_id,
            tool_call_id=_bounded_string(
                value.get("tool_call_id", ""), "tool_call_id", 180, allow_empty=True
            ),
            depth=_nonnegative_int(value.get("depth"), "depth"),
            task=_bounded_string(value.get("task"), "task", 4_000),
            status=status,
            created_at=_number(value.get("created_at"), "created_at"),
            started_at=_optional_number(value.get("started_at"), "started_at"),
            ended_at=_optional_number(value.get("ended_at"), "ended_at"),
            child_session_id=_optional_string(
                value.get("child_session_id"), "child_session_id", 128
            ),
            step_budget=_nonnegative_int(value.get("step_budget", 0), "step_budget"),
            steps_used=_nonnegative_int(value.get("steps_used", 0), "steps_used"),
            prompt_tokens=_nonnegative_int(
                usage.get("prompt_tokens", 0), "usage.prompt_tokens"
            ),
            completion_tokens=_nonnegative_int(
                usage.get("completion_tokens", 0), "usage.completion_tokens"
            ),
            total_tokens=_nonnegative_int(
                usage.get("total_tokens", 0), "usage.total_tokens"
            ),
            result=_bounded_string(
                value.get("result", ""), "result", 20_000, allow_empty=True
            ),
            error=_bounded_string(
                value.get("error", ""), "error", 4_000, allow_empty=True
            ),
            cancel_requested=cancel_requested,
            cancel_reason=_bounded_string(
                value.get("cancel_reason", ""),
                "cancel_reason",
                1_000,
                allow_empty=True,
            ),
            children=list(children),
        )
        if record.steps_used > record.step_budget:
            raise AgentControlError("agent task steps_used 不能超过 step_budget")
        if (
            record.started_at is not None
            and record.ended_at is not None
            and record.ended_at < record.started_at
        ):
            raise AgentControlError("agent task ended_at 不能早于 started_at")
        return record


class AgentControlPlane:
    """Session-scoped registry shared by the root Agent and all descendants."""

    def __init__(self, config: AgentControlConfig | None = None) -> None:
        self.config = config or AgentControlConfig()
        self._tasks: dict[str, AgentTaskRecord] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._counter = 0
        self._lock = threading.RLock()
        self._on_change: Callable[[], None] | None = None

    def set_on_change(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self._on_change = callback

    def begin_task(
        self,
        *,
        root_turn_id: str,
        parent_id: str | None,
        tool_call_id: str,
        depth: int,
        task: str,
        requested_steps: int,
        max_depth: int | None = None,
    ) -> AgentTaskRecord:
        task = _bounded_string(task, "task", 4_000)
        root_turn_id = _bounded_string(root_turn_id, "root_turn_id", 180)
        if requested_steps < 1:
            raise AgentControlError("requested_steps 必须 > 0")
        depth_limit = min(self.config.max_depth, max_depth or self.config.max_depth)
        if depth < 1 or depth > depth_limit:
            raise AgentControlError(
                f"子 Agent depth={depth} 超过上限 {depth_limit}"
            )

        with self._lock:
            self._prune_history_unlocked(root_turn_id)
            if len(self._tasks) >= self.config.max_stored_tasks:
                raise AgentControlError("控制面历史任务已达到存储上限")
            turn_tasks = [
                record for record in self._tasks.values()
                if record.root_turn_id == root_turn_id
            ]
            if len(turn_tasks) >= self.config.max_tasks_per_turn:
                raise AgentControlError("本轮子 Agent 任务数已达到上限")
            siblings = [
                record for record in turn_tasks if record.parent_id == parent_id
            ]
            if len(siblings) >= self.config.max_children_per_parent:
                raise AgentControlError("同一父任务的子 Agent 数已达到上限")
            if parent_id is not None:
                parent = self._tasks.get(parent_id)
                if parent is None or parent.root_turn_id != root_turn_id:
                    raise AgentControlError("parent agent task 不存在或不属于当前 turn")
                if parent.status not in {"pending", "running"}:
                    raise AgentControlError("parent agent task 已终止")

            used_or_reserved_steps = sum(
                (
                    record.steps_used
                    if record.status in TERMINAL_AGENT_TASK_STATUSES
                    else record.step_budget
                )
                for record in turn_tasks
            )
            available_steps = self.config.max_steps_per_turn - used_or_reserved_steps
            if available_steps < 1:
                raise AgentControlError("本轮子 Agent 共享 step 预算已耗尽")
            allocation = min(requested_steps, available_steps)

            self._counter += 1
            task_id = f"a{self._counter:04d}{secrets.token_hex(3)}"
            active_count = sum(
                record.status == "running" for record in self._tasks.values()
            )
            now = time.time()
            status: AgentTaskStatus = (
                "running"
                if active_count < self.config.max_concurrent_tasks
                else "failed"
            )
            record = AgentTaskRecord(
                id=task_id,
                root_turn_id=root_turn_id,
                parent_id=parent_id,
                tool_call_id=str(tool_call_id or "")[:180],
                depth=depth,
                task=task,
                status=status,
                created_at=now,
                started_at=now if status == "running" else None,
                ended_at=now if status == "failed" else None,
                step_budget=allocation if status == "running" else 0,
                error=(
                    "并发子 Agent 数已达到上限"
                    if status == "failed" else ""
                ),
            )
            self._tasks[task_id] = record
            self._cancel_events[task_id] = threading.Event()
            if parent_id is not None:
                self._tasks[parent_id].children.append(task_id)
        self._notify_change()
        return record

    def bind_child_session(self, task_id: str, session_id: str) -> None:
        with self._lock:
            record = self._require(task_id)
            record.child_session_id = str(session_id)[:128]
        self._notify_change()

    def add_usage(
        self,
        task_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        with self._lock:
            record = self._require(task_id)
            record.prompt_tokens += max(0, int(prompt_tokens))
            record.completion_tokens += max(0, int(completion_tokens))
            record.total_tokens += max(0, int(total_tokens))
            turn_total = sum(
                item.total_tokens for item in self._tasks.values()
                if item.root_turn_id == record.root_turn_id
            )
            if turn_total >= self.config.max_tokens_per_turn:
                self._request_cancel_turn_unlocked(
                    record.root_turn_id,
                    "本轮子 Agent 共享 token 预算已耗尽",
                )
        self._notify_change()

    def finish_task(
        self,
        task_id: str,
        *,
        status: AgentTaskStatus,
        steps_used: int,
        result: str = "",
        error: str = "",
    ) -> AgentTaskRecord:
        if status not in TERMINAL_AGENT_TASK_STATUSES:
            raise AgentControlError("finish_task 需要终态 status")
        with self._lock:
            record = self._require(task_id)
            if record.status in TERMINAL_AGENT_TASK_STATUSES and record.ended_at is not None:
                return record
            record.status = status
            record.ended_at = time.time()
            record.steps_used = max(0, min(int(steps_used), record.step_budget))
            record.result = str(result)[: self.config.max_result_chars]
            record.error = str(error)[:4_000]
            if status in {"cancelled", "timed_out"}:
                record.cancel_requested = True
                record.cancel_reason = record.cancel_reason or record.error
        self._notify_change()
        return record

    def request_cancel(self, task_id: str, reason: str) -> None:
        with self._lock:
            record = self._require(task_id)
            self._request_cancel_subtree_unlocked(record.id, str(reason)[:1_000])
        self._notify_change()

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            current = self._tasks.get(task_id)
            while current is not None:
                event = self._cancel_events.get(current.id)
                if current.cancel_requested or (event is not None and event.is_set()):
                    return True
                current = (
                    self._tasks.get(current.parent_id)
                    if current.parent_id is not None else None
                )
            return False

    def cancellation_reason(self, task_id: str) -> str:
        with self._lock:
            current = self._tasks.get(task_id)
            while current is not None:
                if current.cancel_requested:
                    return current.cancel_reason or "agent task cancelled"
                current = (
                    self._tasks.get(current.parent_id)
                    if current.parent_id is not None else None
                )
            return ""

    def get(self, task_id: str) -> AgentTaskRecord:
        with self._lock:
            return AgentTaskRecord.from_dict(self._require(task_id).to_dict())

    def tree(self, root_turn_id: str | None = None) -> list[dict[str, Any]]:
        return self._build_tree(root_turn_id, compact=False)

    def tree_summary(
        self, root_turn_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self._build_tree(root_turn_id, compact=True)

    def _build_tree(
        self,
        root_turn_id: str | None,
        *,
        compact: bool,
    ) -> list[dict[str, Any]]:
        with self._lock:
            records = [
                AgentTaskRecord.from_dict(record.to_dict())
                for record in self._tasks.values()
                if root_turn_id is None or record.root_turn_id == root_turn_id
            ]
        by_id = {record.id: record for record in records}

        def node(record: AgentTaskRecord) -> dict[str, Any]:
            value = record.summary_dict() if compact else record.to_dict()
            value["children"] = [
                node(by_id[child_id])
                for child_id in record.children if child_id in by_id
            ]
            return value

        return [
            node(record) for record in records
            if record.parent_id is None or record.parent_id not in by_id
        ]

    def _prune_history_unlocked(self, current_turn_id: str) -> None:
        overflow = len(self._tasks) - self.config.max_stored_tasks + 1
        if overflow <= 0:
            return
        groups: dict[str, list[AgentTaskRecord]] = {}
        for record in self._tasks.values():
            if record.root_turn_id != current_turn_id:
                groups.setdefault(record.root_turn_id, []).append(record)
        candidates = sorted(
            (
                records for records in groups.values()
                if all(
                    record.status in TERMINAL_AGENT_TASK_STATUSES
                    for record in records
                )
            ),
            key=lambda records: min(record.created_at for record in records),
        )
        removed = 0
        for records in candidates:
            for record in records:
                self._tasks.pop(record.id, None)
                self._cancel_events.pop(record.id, None)
                removed += 1
            if removed >= overflow:
                break

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "config": self.config.to_dict(),
                "counter": self._counter,
                "tasks": [record.to_dict() for record in self._tasks.values()],
            }

    @classmethod
    def from_snapshot(
        cls,
        value: Any,
        *,
        mark_interrupted: bool = False,
    ) -> "AgentControlPlane":
        if value in (None, {}):
            return cls()
        if not isinstance(value, dict):
            raise AgentControlError("agent control snapshot 必须是对象")
        plane = cls(AgentControlConfig.from_dict(value.get("config", {})))
        counter = _nonnegative_int(value.get("counter", 0), "counter")
        rows = value.get("tasks", [])
        if not isinstance(rows, list):
            raise AgentControlError("agent control tasks 必须是数组")
        if len(rows) > 10_000:
            raise AgentControlError("agent control tasks 超出快照上限")
        with plane._lock:
            plane._counter = counter
            for row in rows:
                record = AgentTaskRecord.from_dict(row)
                if record.id in plane._tasks:
                    raise AgentControlError(f"重复 agent task id: {record.id}")
                if mark_interrupted and record.status in {"pending", "running"}:
                    record.status = "failed"
                    record.ended_at = time.time()
                    record.error = "子 Agent 因进程重启而中断；执行结果未知，请核实 workspace"
                    record.cancel_requested = True
                    record.cancel_reason = "process_restart"
                plane._tasks[record.id] = record
                plane._cancel_events[record.id] = threading.Event()
                if record.cancel_requested:
                    plane._cancel_events[record.id].set()
            plane._validate_tree_unlocked()
        return plane

    def _validate_tree_unlocked(self) -> None:
        for record in self._tasks.values():
            if record.parent_id is None and record.depth != 1:
                raise AgentControlError("root agent task depth 必须为 1")
            if len(set(record.children)) != len(record.children):
                raise AgentControlError(f"agent task {record.id} children 重复")
            for child_id in record.children:
                child = self._tasks.get(child_id)
                if child is None:
                    raise AgentControlError(
                        f"agent task {record.id} 引用了不存在的 child {child_id}"
                    )
                if child.parent_id != record.id:
                    raise AgentControlError("agent task parent/children 关系不一致")
                if child.root_turn_id != record.root_turn_id:
                    raise AgentControlError("父子 agent task 不属于同一个 root turn")
                if child.depth != record.depth + 1:
                    raise AgentControlError("父子 agent task depth 不连续")
            if record.parent_id is not None:
                parent = self._tasks.get(record.parent_id)
                if parent is None or record.id not in parent.children:
                    raise AgentControlError("agent task 缺少对应 parent 反向引用")

            seen: set[str] = set()
            current: AgentTaskRecord | None = record
            while current is not None:
                if current.id in seen:
                    raise AgentControlError("agent task tree 存在环")
                seen.add(current.id)
                current = (
                    self._tasks.get(current.parent_id)
                    if current.parent_id is not None else None
                )

    def _request_cancel_turn_unlocked(self, root_turn_id: str, reason: str) -> None:
        for record in self._tasks.values():
            if (
                record.root_turn_id == root_turn_id
                and record.status in {"pending", "running"}
            ):
                record.cancel_requested = True
                record.cancel_reason = reason
                self._cancel_events[record.id].set()

    def _request_cancel_subtree_unlocked(self, task_id: str, reason: str) -> None:
        record = self._require(task_id)
        if record.status in {"pending", "running"}:
            record.cancel_requested = True
            record.cancel_reason = reason
            self._cancel_events[task_id].set()
        for child_id in record.children:
            self._request_cancel_subtree_unlocked(child_id, reason)

    def _require(self, task_id: str) -> AgentTaskRecord:
        record = self._tasks.get(task_id)
        if record is None:
            raise AgentControlError(f"未知 agent task: {task_id}")
        return record

    def _notify_change(self) -> None:
        with self._lock:
            callback = self._on_change
        if callback is not None:
            try:
                callback()
            except Exception:
                pass


def _bounded_string(
    value: Any,
    field: str,
    max_chars: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AgentControlError(f"{field} 必须是字符串")
    cleaned = value.strip()
    if len(cleaned) > max_chars:
        raise AgentControlError(f"{field} 不能超过 {max_chars} 个字符")
    return cleaned


def _optional_string(value: Any, field: str, max_chars: int) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, field, max_chars)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AgentControlError(f"{field} 必须是非负整数")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AgentControlError(f"{field} 必须是数字")
    return float(value)


def _optional_number(value: Any, field: str) -> float | None:
    return None if value is None else _number(value, field)
