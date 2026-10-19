"""Agent, Plan, and Ask ceilings.

These names are execution limits. They are not system-prompt labels.
Ask and Plan hide mutating tools and the permission resolver denies those
operations even when the session permission mode is bypass.
"""

from __future__ import annotations

from ...domain.model.session.authorization import InteractionMode
from ...domain.policy.permission.limits import ASK_OPERATIONS, PLAN_OPERATIONS

ASK_TOOLS = frozenset({
    "read_file",
    "list_directory",
    "glob",
    "grep",
    "web_search",
    "knowledge_search",
    "ask_user",
    "get_core_memory",
    "search_memory",
    "get_memory",
    "search_episodes",
    "get_episode",
    "get_plan",
    "tool_search",
    "get_agent",
    "get_command",
    "list_commands",
    "get_schedule",
    "list_schedules",
    "list_schedule_runs",
    "get_schedule_run",
})

PLAN_TOOLS = ASK_TOOLS | frozenset({
    "create_plan",
    "update_plan",
    "replan",
})



def interaction_mode_of(session: object | None) -> InteractionMode:
    value = session.authorization.interaction_mode if session is not None else "agent"
    if value in {"agent", "plan", "ask"}:
        return value
    return "agent"


def permission_mode_of(session: object | None) -> str | None:
    if session is None:
        return None
    value = session.authorization.permission_mode
    if value in {"default", "acceptEdits", "bypass", "plan"}:
        return value
    return None


def ceiling_for(session: object | None) -> InteractionMode:
    """The stricter of the interaction mode and a plan-only permission mode."""

    if session is None:
        return "agent"
    permission, interaction = session.authorization.effective_modes("default")
    if interaction == "ask":
        return "ask"
    if interaction == "plan" or permission == "plan":
        return "plan"
    return "agent"


def tool_visible(name: str, session: object | None) -> bool:
    ceiling = ceiling_for(session)
    if ceiling == "agent":
        return True
    if ceiling == "ask":
        return name in ASK_TOOLS
    return name in PLAN_TOOLS


def operations_allowed(operations: frozenset[str] | set[str], session: object | None) -> bool:
    ceiling = ceiling_for(session)
    if ceiling == "agent":
        return True
    allowed = ASK_OPERATIONS if ceiling == "ask" else PLAN_OPERATIONS
    if "unknown" in operations:
        return False
    return set(operations) <= allowed


__all__ = [
    "ASK_OPERATIONS",
    "ASK_TOOLS",
    "PLAN_OPERATIONS",
    "PLAN_TOOLS",
    "ceiling_for",
    "interaction_mode_of",
    "operations_allowed",
    "permission_mode_of",
    "tool_visible",
]
