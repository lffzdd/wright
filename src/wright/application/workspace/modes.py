"""Agent, Plan, and Ask ceilings.

These names are execution limits. They are not system-prompt labels.
Ask and Plan refuse mutating tools at execution, and the permission resolver
denies those operations even when the session permission mode is bypass.
The tool schema list does not change with the mode, so switching modes never
invalidates the provider's prompt cache; the model learns the mode from
``mode_notice`` at the end of each request instead.
"""

from __future__ import annotations

from ...domain.model.session.authorization import InteractionMode
from ...domain.policy.permission.limits import ASK_OPERATIONS, PLAN_OPERATIONS

ASK_TOOLS = frozenset({
    "read",
    "ls",
    "glob",
    "grep",
    "web_search",
    "web_fetch",
    "knowledge_search",
    "ask_user",
    "memory_search",
    "memory_read",
    "tool_search",
    # The wrapper is visible; the dispatcher gates the tool it unwraps to.
    "call_tool",
    "get_agent",
    "shell_output",
    "list_commands",
    "get_schedule",
    "list_schedules",
    "list_schedule_runs",
    "get_schedule_run",
})

PLAN_TOOLS = ASK_TOOLS | frozenset({
    "update_plan",
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


_MODE_SUMMARY = {
    "ask": "Ask mode is read-only: tools that write files, run commands or change state are refused.",
    "plan": (
        "Plan mode is read-only except update_plan: investigate and write the plan; "
        "tools that write files, run commands or change state are refused."
    ),
}


def mode_notice(session: object | None) -> str | None:
    """Request-tail notice while a restricted mode is active."""
    ceiling = ceiling_for(session)
    if ceiling == "agent":
        return None
    return (
        f"<interaction-mode>{ceiling}. {_MODE_SUMMARY[ceiling]} Do not try them; "
        "if the task needs changes, say so and ask the user to switch to agent "
        "mode.</interaction-mode>"
    )


def mode_blocked_message(name: str, session: object | None) -> str:
    ceiling = ceiling_for(session)
    if ceiling == "agent":
        return f"{name} is not allowed in this mode"
    return (
        f"{name} is not available in {ceiling} mode. {_MODE_SUMMARY[ceiling]} "
        "Use a read-only tool, or ask the user to switch to agent mode."
    )


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
    "mode_blocked_message",
    "mode_notice",
    "operations_allowed",
    "permission_mode_of",
    "tool_visible",
]
