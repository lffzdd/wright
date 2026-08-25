"""会话级计划管理。

PlanManager 保存当前任务的结构化计划；planning tools 负责把它暴露给模型。
计划属于 SessionState，因此主 Agent 和每个子 Agent 天然相互隔离。
"""

from .manager import (
    PlanError,
    PlanManager,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
)

__all__ = [
    "PlanError",
    "PlanManager",
    "PlanStatus",
    "PlanStep",
    "PlanStepStatus",
]
