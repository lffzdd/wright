"""Prompt and reminder management during Agent run turns."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..planning.prompt import plan_prompt_block
from ..skills import SKILL_LOADER_NAME, catalog_reminder

if TYPE_CHECKING:
    from ...domain.model.session import Session
    from ...infrastructure.tools.base import Tool
    from ..skills import SkillRegistry


class AgentPromptManager:
    """Request-scoped reminders. The skill catalog is not written to the transcript."""

    def __init__(
        self,
        session_state: Session,
        skills: SkillRegistry | None = None,
        schema_tools: Sequence[Tool] = (),
    ) -> None:
        self.session_state = session_state
        self.skills = skills
        self.schema_tools = schema_tools

    def plan_reminder(self) -> dict | None:
        block = plan_prompt_block(self.session_state.plan_manager)
        # 计划字段由模型工具调用产生，最终也可能来自不可信用户文本；保持 user role，
        # 并由 plan_prompt_block 的 JSON 数据边界明确它不具备指令权限。
        return {"role": "user", "content": block} if block else None

    def skill_loader_exposed(self) -> bool:
        """True when this request should both list and be able to call load_skill."""
        if self.skills is None or not self.skills.has_skills():
            return False
        tool = next(
            (item for item in self.schema_tools if item.name == SKILL_LOADER_NAME),
            None,
        )
        if tool is None or not tool.expose_to_model:
            return False
        if tool.defer_to_model and SKILL_LOADER_NAME not in self.session_state.active_deferred_tools:
            return False
        return True

    def visible_schema_tools(self) -> list[Tool]:
        """Schema snapshot for this request. Hides the loader when no skills exist."""
        if self.skill_loader_exposed() or not any(
            tool.name == SKILL_LOADER_NAME for tool in self.schema_tools
        ):
            return list(self.schema_tools)
        return [tool for tool in self.schema_tools if tool.name != SKILL_LOADER_NAME]

    def skill_catalog_message(self) -> dict | None:
        if not self.skill_loader_exposed() or self.skills is None:
            return None
        catalog = catalog_reminder(self.skills.list_metas())
        if not catalog:
            return None
        return {"role": "user", "content": catalog}

    def ephemeral_reminders(self) -> list[dict]:
        """Reminders for this request only. They are not appended to the transcript."""
        reminders: list[dict] = []
        plan = self.plan_reminder()
        if plan is not None:
            reminders.append(plan)
        catalog = self.skill_catalog_message()
        if catalog is not None:
            reminders.append(catalog)
        return reminders
