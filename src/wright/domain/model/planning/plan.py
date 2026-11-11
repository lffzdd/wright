"""Structured plan state machine.

PlanManager does not depend on an LLM, a tool, or a renderer. It stores the
plan and applies transitions. The prompt projection lives in the application
layer. Overall status is derived from step status.
"""

from __future__ import annotations

import math
import re
import threading
import time
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
    started_at: float | None = None
    ended_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "note": self.note,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class PlanManager:
    """线程安全的会话级计划管理器。"""

    def __init__(self) -> None:
        self.objective = ""
        self.steps: list[PlanStep] = []
        self.revision = 0
        # generation 每开始一份新计划加一；carried 表示计划是从上一轮带过来的、
        # 本轮还没更新过。
        self.generation = 0
        self.carried = False
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
        """Clear the plan."""
        with self._lock:
            # revision 不归零：界面靠它判断计划是否变化。
            if self.steps or self.objective:
                self.revision += 1
            self.objective = ""
            self.steps = []
            self.carried = False
            self._step_counter = 0

    def begin_turn(self) -> None:
        """Prepare the plan for a new user turn.

        A finished plan is cleared. An unfinished one is kept and marked as
        carried, so the model can resume or replace it; it stops being carried
        once this turn updates it.
        """
        with self._lock:
            if self._derive_status() in {"empty", "completed"}:
                self.reset()
            elif not self.carried:
                self.carried = True
                self.revision += 1

    def apply_steps(self, steps: list[dict], *, objective: str | None = None) -> dict:
        """Apply a submitted step list atomically.

        A submitted step matches an existing one by ``id`` when it carries one
        (which also renames it), otherwise by title, so matched steps keep their
        ids and timestamps. Existing steps left out of the list keep their
        status and their place relative to the submitted steps; dropping a step
        takes an explicit ``skipped``. Completed steps are final, skipped ones
        may reopen. Steps may start and finish in any order, but at most one is
        in progress across the whole plan. A list
        starts a new plan when there is none, or when it brings a different
        objective and none of its steps match the current plan; a different
        objective with matching steps only renames the goal.
        """
        items = self._clean_items(steps)
        cleaned_objective = (
            self._clean_text(objective, "objective", max_length=MAX_OBJECTIVE_LENGTH)
            if objective is not None else None
        )
        with self._lock:
            fresh = not self.steps or (
                cleaned_objective is not None
                and cleaned_objective != self.objective
                and not any(
                    any(step.id == step_id if step_id is not None else step.title == title for step in self.steps)
                    for step_id, title, _status, _note in items
                )
            )
            if fresh and cleaned_objective is None:
                raise PlanError("objective is required to start a plan", code="plan.field_required", field="objective")
            existing = [] if fresh else [PlanStep(**step.to_dict()) for step in self.steps]
            position = {id(step): index for index, step in enumerate(existing)}
            was_active = {step.id for step in existing if step.status == "in_progress"}
            counter = 0 if fresh else self._step_counter
            now = time.time()
            unmatched = list(existing)
            route: list[PlanStep] = []
            for step_id, title, status, note in items:
                if step_id is not None:
                    step = next((item for item in unmatched if item.id == step_id), None)
                    if step is None:
                        if any(item.id == step_id for item in route):
                            raise PlanError(
                                f"{step_id} appears more than once in steps",
                                code="plan.duplicate_step", step_id=step_id,
                            )
                        raise PlanError(f"Unknown step id: {step_id}", code="plan.unknown_step", step_id=step_id)
                else:
                    step = next((item for item in unmatched if item.title == title), None)
                if step is None:
                    counter += 1
                    step = PlanStep(id=f"step_{counter}", title=title)
                else:
                    if step.status == "completed" and status != "completed":
                        raise PlanError(
                            f"{step.id} is already terminal ({step.status}) and cannot become {status}.",
                            code="plan.terminal_step", step_id=step.id, status=step.status, next_status=status,
                        )
                    unmatched.remove(step)
                    step.title = title
                self._transition(step, status, now)
                if note is not None:
                    step.note = note
                route.append(step)
            # Omitted steps keep their status and stay ahead of the first
            # submitted step that followed them in the old list. A new step
            # also goes after the finished history ahead of its next anchor.
            final: list[PlanStep] = []
            for index, step in enumerate(route):
                if id(step) in position:
                    limit, finished_only = position[id(step)], False
                else:
                    anchor = next((position[id(item)] for item in route[index + 1:] if id(item) in position), None)
                    limit, finished_only = (len(existing) if anchor is None else anchor), True
                for omitted in [
                    item for item in unmatched
                    if position[id(item)] < limit and (not finished_only or item.status in _TERMINAL_STATUSES)
                ]:
                    unmatched.remove(omitted)
                    final.append(omitted)
                final.append(step)
            final.extend(unmatched)
            if len(final) > MAX_TOTAL_PLAN_STEPS:
                raise PlanError(
                    f"Plan history plus new steps cannot exceed {MAX_TOTAL_PLAN_STEPS}. Finish or skip the open steps, then start a new plan with a new objective.",
                    code="plan.too_long", limit=MAX_TOTAL_PLAN_STEPS,
                )
            # Steps may start and finish in any order, but only one runs at a
            # time across the whole plan, including steps the caller left out.
            active = [step for step in final if step.status == "in_progress"]
            if len(active) > 1:
                running = next((step for step in active if step.id in was_active), active[0])
                starting = next(step for step in active if step is not running)
                raise PlanError(
                    f"{running.id} is in progress. Finish, block, or skip it before starting {starting.id}.",
                    code="plan.step_busy", active_id=running.id, step_id=starting.id,
                )
            before = self._snapshot_unlocked()
            if cleaned_objective is not None:
                self.objective = cleaned_objective
            self.steps = final
            self._step_counter = counter
            self.carried = False
            if fresh:
                self.generation += 1
            if fresh or self._snapshot_unlocked() != before:
                self.revision += 1
            return self._snapshot_unlocked()

    @staticmethod
    def _transition(step: PlanStep, status: PlanStepStatus, now: float) -> None:
        if step.status == status:
            return
        if status == "in_progress" and step.started_at is None:
            step.started_at = now
        if status in _TERMINAL_STATUSES:
            step.ended_at = max(now, step.started_at or now)
        else:
            step.ended_at = None
        step.status = status

    @classmethod
    def _clean_items(cls, values: object) -> list[tuple[str | None, str, PlanStepStatus, str | None]]:
        if not isinstance(values, list):
            raise PlanError("steps must be an array of objects", code="plan.steps_type")
        if not values:
            raise PlanError("steps cannot be empty", code="plan.steps_empty")
        if len(values) > MAX_PLAN_STEPS:
            raise PlanError(f"steps cannot exceed {MAX_PLAN_STEPS} items", code="plan.steps_limit", limit=MAX_PLAN_STEPS)
        items = []
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise PlanError("steps must be an array of objects", code="plan.steps_type")
            raw_id = value.get("id")
            step_id = (
                cls._clean_text(raw_id, f"steps[{index}].id", max_length=64)
                if raw_id not in (None, "") else None
            )
            title = cls._clean_text(value.get("title"), f"steps[{index}].title", max_length=MAX_STEP_TITLE_LENGTH)
            status = value.get("status", "pending")
            if status not in _VALID_STEP_STATUSES:
                raise PlanError(
                    "Invalid step status. It must be pending, in_progress, completed, blocked, or skipped.",
                    code="plan.bad_status",
                )
            note = cls._clean_note(value["note"]) if value.get("note") is not None else None
            items.append((step_id, title, status, note))
        active = [title for _id, title, status, _note in items if status == "in_progress"]
        if len(active) > 1:
            raise PlanError(
                f"{active[0]} is in progress. Finish, block, or skip it before starting {active[1]}.",
                code="plan.step_busy", active_id=active[0], step_id=active[1],
            )
        return items

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
        # Checkpoints written before these fields existed omit them.
        generation = snapshot.get("generation", 1 if raw_steps else 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise PlanError("plan snapshot.generation must be a non-negative integer", code="plan.snapshot")
        carried = snapshot.get("carried", False)
        if not isinstance(carried, bool) or (carried and not raw_steps):
            raise PlanError("plan snapshot.carried must be a boolean, true only with steps", code="plan.snapshot")

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
            started = self._timestamp(item.get("started_at"), "started_at")
            ended = self._timestamp(item.get("ended_at"), "ended_at")
            if ended is not None and started is not None and ended < started:
                raise PlanError("ended_at cannot precede started_at", code="plan.snapshot")
            if ended is not None and status not in _TERMINAL_STATUSES:
                raise PlanError("only terminal steps can have ended_at", code="plan.snapshot")
            restored_steps.append(PlanStep(step_id, title, status, note, started, ended))

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
            self.generation = generation
            self.carried = carried
            self._step_counter = highest_step
            return self._snapshot_unlocked()

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
            "generation": self.generation,
            "carried": self.carried,
            "steps": [step.to_dict() for step in self.steps],
        }

    @staticmethod
    def _timestamp(value: object, name: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise PlanError(f"{name} must be a finite non-negative timestamp", code="plan.snapshot")
        return float(value)

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
