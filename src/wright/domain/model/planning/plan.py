"""Structured plan state machine.

PlanManager does not depend on an LLM, a tool, or a renderer. It stores the
plan and applies transitions. The prompt projection lives in the application
layer. Overall status is derived from step status.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Literal

PlanStepStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "blocked",
    "skipped",
]
PlanStatus = Literal["empty", "pending", "in_progress", "blocked", "completed"]

# 计划是给模型持续阅读的执行摘要，而非项目管理系统。限制规模和文本长度，既能
# 防止单次 tool call 把上下文塞满，也能促使步骤保持可执行、可追踪。
MAX_PLAN_STEPS = 12
MAX_TOTAL_PLAN_STEPS = 24
MAX_OBJECTIVE_LENGTH = 240
MAX_STEP_TITLE_LENGTH = 160
MAX_NOTE_LENGTH = 240
_TERMINAL_STATUSES = frozenset({"completed", "skipped"})
_VALID_STEP_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "blocked", "skipped"}
)


class PlanError(ValueError):
    """A plan operation violated an input or state-machine constraint."""

    def __init__(self, message: str, *, code: str = "", **params: object) -> None:
        super().__init__(message)
        self.code = code
        self.params = {key: "" if value is None else str(value) for key, value in params.items()}


@dataclass
class PlanStep:
    id: str
    title: str
    status: PlanStepStatus = "pending"
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "note": self.note,
        }


class PlanManager:
    """线程安全的会话级计划管理器。"""

    def __init__(self) -> None:
        self.objective = ""
        self.steps: list[PlanStep] = []
        self.revision = 0
        self._step_counter = 0
        self._lock = threading.RLock()

    @property
    def has_plan(self) -> bool:
        with self._lock:
            return bool(self.steps)

    @property
    def status(self) -> PlanStatus:
        with self._lock:
            return self._derive_status()

    def reset(self) -> None:
        """Start a clean plan namespace for a new user turn."""
        with self._lock:
            self.objective = ""
            self.steps = []
            self.revision = 0
            self._step_counter = 0

    def create_plan(
        self,
        objective: str,
        steps: list[str],
        *,
        replace: bool = False,
    ) -> dict:
        """创建计划；已有未完成计划时必须显式 replace。"""
        objective = self._clean_text(
            objective, "objective", max_length=MAX_OBJECTIVE_LENGTH
        )
        titles = self._clean_steps(steps)

        with self._lock:
            if self.steps and self._derive_status() != "completed" and not replace:
                raise PlanError(
                    "An unfinished plan already exists. Keep updating it, or pass replace=true to replace it.",
                    code="plan.open_exists",
                )

            self.objective = objective
            self.steps = []
            self._step_counter = 0
            for title in titles:
                self.steps.append(self._new_step(title))
            self.revision += 1
            return self._snapshot_unlocked()

    def update_step(
        self,
        step_id: str,
        status: PlanStepStatus,
        *,
        note: str | None = None,
    ) -> dict:
        """更新单步状态，并保证同一时刻最多一个步骤 in_progress。"""
        step_id = self._clean_text(step_id, "step_id", max_length=64)
        if status not in _VALID_STEP_STATUSES:
            raise PlanError(
                "Invalid step status. It must be pending, in_progress, completed, blocked, or skipped.",
                code="plan.bad_status",
            )

        with self._lock:
            step = self._find_step(step_id)
            if step.status in _TERMINAL_STATUSES and status != step.status:
                raise PlanError(
                    f"{step.id} is already terminal ({step.status}) and cannot become {status}. Use replan to change the remaining route.",
                    code="plan.terminal_step",
                    step_id=step.id,
                    status=step.status,
                    next_status=status,
                )

            if status == "in_progress":
                active = next(
                    (
                        other
                        for other in self.steps
                        if other.id != step.id and other.status == "in_progress"
                    ),
                    None,
                )
                if active is not None:
                    raise PlanError(
                        f"{active.id} is in progress. Finish, block, or pause it before starting {step.id}.",
                        code="plan.step_busy",
                        active_id=active.id,
                        step_id=step.id,
                    )

            # steps 的 schema 和工具描述都承诺了执行顺序。允许直接完成一个足够小的
            # 首步骤，但不允许越过任何尚未收口的前置步骤去开始、阻塞或完成后续步骤。
            if status in {"in_progress", "completed", "blocked"}:
                self._require_predecessors_terminal(step)

            changed = step.status != status
            step.status = status
            if note is not None:
                normalized_note = self._clean_note(note)
                changed = changed or step.note != normalized_note
                step.note = normalized_note
            if changed:
                self.revision += 1
            return self._snapshot_unlocked()

    def replan(self, steps: list[str], *, reason: str) -> dict:
        """保留已完成历史，跳过旧的未完成部分并追加新路线。"""
        titles = self._clean_steps(steps)
        reason = self._clean_text(reason, "reason", max_length=MAX_NOTE_LENGTH)

        with self._lock:
            if not self.steps:
                raise PlanError(
                    "There is no plan to replan. Call create_plan first.",
                    code="plan.no_plan",
                )
            if self._derive_status() == "completed":
                raise PlanError(
                    "The current plan is already complete. Use create_plan for a new goal.",
                    code="plan.already_done",
                )
            if len(self.steps) + len(titles) > MAX_TOTAL_PLAN_STEPS:
                raise PlanError(
                    f"Plan history plus new steps cannot exceed {MAX_TOTAL_PLAN_STEPS}. Create a new plan or shorten the route.",
                    code="plan.too_long",
                    limit=MAX_TOTAL_PLAN_STEPS,
                )

            for step in self.steps:
                if step.status not in _TERMINAL_STATUSES:
                    step.status = "skipped"
                    marker = f"Replanned: {reason}"
                    combined = (
                        f"{step.note}; {marker}".strip("; ")
                        if step.note
                        else marker
                    )
                    step.note = combined[:MAX_NOTE_LENGTH]

            for title in titles:
                self.steps.append(self._new_step(title))
            self.revision += 1
            return self._snapshot_unlocked()

    def snapshot(self) -> dict:
        """返回可序列化快照，调用方不能借此修改内部状态。"""
        with self._lock:
            return self._snapshot_unlocked()

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> PlanManager:
        manager = cls()
        manager.restore(snapshot)
        return manager

    def restore(self, snapshot: dict) -> dict:
        """Restore a checkpointed plan after validating it transactionally."""
        if not isinstance(snapshot, dict):
            raise PlanError("plan snapshot must be an object", code="plan.snapshot")
        raw_steps = snapshot.get("steps")
        if not isinstance(raw_steps, list):
            raise PlanError("plan snapshot.steps must be an array", code="plan.snapshot")
        if len(raw_steps) > MAX_TOTAL_PLAN_STEPS:
            raise PlanError(
                f"plan snapshot.steps cannot exceed {MAX_TOTAL_PLAN_STEPS} items",
                code="plan.snapshot",
            )

        objective_value = snapshot.get("objective", "")
        if raw_steps:
            objective = self._clean_text(
                objective_value,
                "objective",
                max_length=MAX_OBJECTIVE_LENGTH,
            )
        elif objective_value in (None, ""):
            objective = ""
        else:
            objective = self._clean_text(
                objective_value,
                "objective",
                max_length=MAX_OBJECTIVE_LENGTH,
            )

        revision = snapshot.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise PlanError("plan snapshot.revision must be a non-negative integer", code="plan.snapshot")

        restored_steps: list[PlanStep] = []
        seen_ids: set[str] = set()
        highest_step = 0
        for index, item in enumerate(raw_steps):
            if not isinstance(item, dict):
                raise PlanError(f"plan snapshot.steps[{index}] must be an object", code="plan.snapshot")
            step_id = self._clean_text(
                item.get("id"), f"steps[{index}].id", max_length=64
            )
            match = re.fullmatch(r"step_(\d+)", step_id)
            if match is None or int(match.group(1)) < 1:
                raise PlanError(f"Invalid step id: {step_id}", code="plan.snapshot")
            if step_id in seen_ids:
                raise PlanError(f"Duplicate step id: {step_id}", code="plan.snapshot")
            seen_ids.add(step_id)
            highest_step = max(highest_step, int(match.group(1)))

            title = self._clean_text(
                item.get("title"),
                f"steps[{index}].title",
                max_length=MAX_STEP_TITLE_LENGTH,
            )
            status = item.get("status", "pending")
            if status not in _VALID_STEP_STATUSES:
                raise PlanError(f"steps[{index}].status is invalid: {status}", code="plan.snapshot")
            note = self._clean_note(item.get("note", ""))
            restored_steps.append(PlanStep(step_id, title, status, note))

        if sum(step.status == "in_progress" for step in restored_steps) > 1:
            raise PlanError("plan snapshot has more than one in_progress step", code="plan.snapshot")

        statuses = {step.status for step in restored_steps}
        if not restored_steps:
            derived_status: PlanStatus = "empty"
        elif all(status in _TERMINAL_STATUSES for status in statuses):
            derived_status = "completed"
        elif "in_progress" in statuses:
            derived_status = "in_progress"
        elif "blocked" in statuses:
            derived_status = "blocked"
        else:
            derived_status = "pending"
        saved_status = snapshot.get("status", derived_status)
        if saved_status != derived_status:
            raise PlanError(
                f"plan snapshot.status={saved_status} does not match derived status {derived_status}",
                code="plan.snapshot",
            )

        with self._lock:
            self.objective = objective
            self.steps = restored_steps
            self.revision = revision
            self._step_counter = highest_step
            return self._snapshot_unlocked()

    def _new_step(self, title: str) -> PlanStep:
        self._step_counter += 1
        return PlanStep(id=f"step_{self._step_counter}", title=title)

    def _find_step(self, step_id: str) -> PlanStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise PlanError(
            f"Unknown step id: {step_id}",
            code="plan.unknown_step",
            step_id=step_id,
        )

    def _require_predecessors_terminal(self, step: PlanStep) -> None:
        step_index = self.steps.index(step)
        predecessor = next(
            (
                previous
                for previous in self.steps[:step_index]
                if previous.status not in _TERMINAL_STATUSES
            ),
            None,
        )
        if predecessor is not None:
            raise PlanError(
                f"Predecessor {predecessor.id} of {step.id} is not completed or skipped. Follow the plan order, or use replan.",
                code="plan.predecessor",
                step_id=step.id,
                predecessor_id=predecessor.id,
            )

    def _derive_status(self) -> PlanStatus:
        if not self.steps:
            return "empty"
        statuses = {step.status for step in self.steps}
        if all(status in _TERMINAL_STATUSES for status in statuses):
            return "completed"
        if "in_progress" in statuses:
            return "in_progress"
        if "blocked" in statuses:
            return "blocked"
        return "pending"

    def _snapshot_unlocked(self) -> dict:
        return {
            "objective": self.objective,
            "status": self._derive_status(),
            "revision": self.revision,
            "steps": [step.to_dict() for step in self.steps],
        }

    @staticmethod
    def _clean_text(value: object, field: str, *, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise PlanError(
                f"{field} must be a non-empty string",
                code="plan.field_required",
                field=field,
            )
        cleaned = value.strip()
        if len(cleaned) > max_length:
            raise PlanError(
                f"{field} cannot exceed {max_length} characters",
                code="plan.field_too_long",
                field=field,
                limit=max_length,
            )
        return cleaned

    @classmethod
    def _clean_note(cls, value: object) -> str:
        if not isinstance(value, str):
            raise PlanError("note must be a string", code="plan.note_type")
        cleaned = value.strip()
        if len(cleaned) > MAX_NOTE_LENGTH:
            raise PlanError(
                f"note cannot exceed {MAX_NOTE_LENGTH} characters",
                code="plan.note_too_long",
                limit=MAX_NOTE_LENGTH,
            )
        return cleaned

    @classmethod
    def _clean_steps(cls, values: object) -> list[str]:
        if not isinstance(values, list):
            raise PlanError("steps must be an array of strings", code="plan.steps_type")
        if not values:
            raise PlanError("steps cannot be empty", code="plan.steps_empty")
        if len(values) > MAX_PLAN_STEPS:
            raise PlanError(
                f"steps cannot exceed {MAX_PLAN_STEPS} items",
                code="plan.steps_limit",
                limit=MAX_PLAN_STEPS,
            )
        return [
            cls._clean_text(
                value, f"steps[{idx}]", max_length=MAX_STEP_TITLE_LENGTH
            )
            for idx, value in enumerate(values)
        ]
