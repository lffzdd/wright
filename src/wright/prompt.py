"""System instructions; tool schemas are sent through the API tools parameter."""

SYSTEM_PROMPT = """You are a coding assistant. Use the available tools to inspect,
implement, and verify the user's request. Base claims about completed work on
actual tool results. Continue after tool results until the task is complete or
requires user input. Respond directly to the user in natural language.
"""


def build_system_prompt(memory_section: str = "") -> str:
    if memory_section:
        return f"{SYSTEM_PROMPT}\n{memory_section}\n"
    return SYSTEM_PROMPT
