"""Prompt and reminder management during Agent run turns."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..skills.prompt import catalog_reminder

if TYPE_CHECKING:
    from ..domain.session import Session
    from ..skills.registry import SkillRegistry
    from ..tools.base import Tool


class AgentPromptManager:
    """Manages ephemeral reminders and skill catalogs injected into turns."""

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
        block = self.session_state.plan_manager.to_prompt_block()
        # 计划字段由模型工具调用产生，最终也可能来自不可信用户文本；保持 user role，
        # 并由 to_prompt_block 的 JSON 数据边界明确它不具备指令权限。
        return {"role": "user", "content": block} if block else None

    def ensure_skill_catalog(self, active_deferred_tools: Sequence[str] = ()) -> None:
        """会话里只把 skill 目录写入 transcript 一次。"""
        # load_skill 走按需发现时，普通对话不应背整个技能目录；它被 tool_search
        # 激活后的下一次模型调用，才需要目录来选择具体 skill_id。
        load_skill = next(
            (tool for tool in self.schema_tools if tool.name == "load_skill"),
            None,
        )
        if (
            load_skill is not None
            and load_skill.defer_to_model
            and "load_skill" not in active_deferred_tools
        ):
            return
        if self.skills is None or self.session_state.skill_catalog_sent:
            return
        catalog = catalog_reminder(self.skills.list_metas())
        if catalog:
            self.session_state.append_message({"role": "user", "content": catalog})
        self.session_state.mark_skill_catalog_sent()

    def ephemeral_reminders(self) -> list[dict]:
        """本轮才需要、不能落进会话记录的提醒。目前只有最新计划块。"""
        reminders: list[dict] = []
        plan = self.plan_reminder()
        if plan is not None:
            reminders.append(plan)
        return reminders
