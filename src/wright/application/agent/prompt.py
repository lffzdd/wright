"""Prompt and reminder management during Agent run turns."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ...domain.model.mcp import mcp_server_of
from ..planning.prompt import plan_prompt_block
from ..skills import SKILL_LOADER_NAME, catalog_reminder
from ..workspace.modes import mode_notice

if TYPE_CHECKING:
    from ...domain.gateway.skills import ISkillCatalog
    from ...domain.model.session import Session
    from ...domain.model.tool_definition import Tool


class AgentPromptManager:
    """Request-scoped reminders. The skill catalog is not written to the transcript."""

    def __init__(
        self,
        session_state: Session,
        skills: ISkillCatalog | None = None,
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
        # A deferred loader would never be in the schema list.
        return tool is not None and tool.expose_to_model and not tool.defer_to_model

    def visible_schema_tools(self) -> list[Tool]:
        """Schema snapshot for this request. Hides the loader when no skills exist.

        Neither the interaction mode nor the MCP server switches filter this
        list: either would change ``tools`` and invalidate the whole prompt
        cache. The dispatcher refuses blocked tools, and ``mode_reminder`` and
        ``mcp_reminder`` tell the model at the end of the request.
        """
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

    def mode_reminder(self) -> dict | None:
        notice = mode_notice(self.session_state)
        return {"role": "user", "content": notice} if notice else None

    def mcp_reminder(self) -> dict | None:
        servers = {mcp_server_of(tool.name) for tool in self.schema_tools}
        off = [
            name for name in dict.fromkeys(self.session_state.disabled_mcp_servers)
            if name in servers
        ]
        if not off:
            return None
        return {
            "role": "user",
            "content": (
                "<mcp-servers-off>Turned off for this conversation: "
                + ", ".join(off)
                + ". Their tools (mcp__<server>__*) are refused; do not call them."
                "</mcp-servers-off>"
            ),
        }

    def ephemeral_reminders(self) -> list[dict]:
        """Reminders for this request only. They are not appended to the transcript."""
        reminders: list[dict] = []
        plan = self.plan_reminder()
        if plan is not None:
            reminders.append(plan)
        catalog = self.skill_catalog_message()
        if catalog is not None:
            reminders.append(catalog)
        # Last, so the newest constraints sit right before the model's reply.
        mcp = self.mcp_reminder()
        if mcp is not None:
            reminders.append(mcp)
        mode = self.mode_reminder()
        if mode is not None:
            reminders.append(mode)
        return reminders
