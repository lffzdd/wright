"""The MCP server files: ``~/.wright/mcp.json`` and ``{project}/.wright/mcp.json``."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

McpScope = Literal["user", "project"]


class McpConfigUnreadable(ValueError):
    """An ``mcp.json`` that does not parse or has the wrong shape."""


class McpConfigStore(Protocol):
    def path(self, scope: McpScope) -> Path | None: ...

    def servers(self, scope: McpScope) -> Mapping[str, Any]:
        """The raw ``mcpServers`` object; raises McpConfigUnreadable when the file cannot be read."""
        ...

    def write_server(self, scope: McpScope, name: str, entry: Mapping[str, Any] | None) -> None:
        """Replace one server entry; ``None`` removes it. Other keys stay."""
        ...


__all__ = ["McpConfigStore", "McpConfigUnreadable", "McpScope"]
