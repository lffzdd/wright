"""Which MCP server a tool belongs to, and whether a conversation has it on.

MCP tools are named ``mcp__{server}__{tool}`` when they are registered.
"""

from __future__ import annotations

from collections.abc import Iterable

MCP_TOOL_PREFIX = "mcp__"


def mcp_server_of(tool_name: str) -> str | None:
    if not tool_name.startswith(MCP_TOOL_PREFIX):
        return None
    server, separator, tool = tool_name[len(MCP_TOOL_PREFIX):].partition("__")
    return server if separator and server and tool else None


def mcp_tool_enabled(tool_name: str, disabled_servers: Iterable[str]) -> bool:
    """False only for a tool of a server the conversation turned off."""
    server = mcp_server_of(tool_name)
    return server is None or server not in set(disabled_servers)


__all__ = ["MCP_TOOL_PREFIX", "mcp_server_of", "mcp_tool_enabled"]
