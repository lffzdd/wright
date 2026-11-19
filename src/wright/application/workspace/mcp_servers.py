"""The conversation's view of its MCP servers: state, tools, and context cost."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ...domain.model.mcp import mcp_server_of
from ...utils.token_counter import estimate_tools_tokens


def _schema_name(schema: Mapping[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "")
    return str(schema.get("name") or "")


def project_mcp_servers(
    servers: Iterable[Mapping[str, Any]],
    disabled: Sequence[str],
    tool_schemas: Sequence[Mapping[str, Any]] = (),
    tool_names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One row per configured server.

    ``tokens`` counts the server's tools in the schema list the next request
    sends. MCP tools are deferred, so that is their stubs (name and one
    sentence); full schemas reach the model through tool_search results, and
    tools past the stub limit are not listed at all. Turning a server off
    does not remove its stubs, so its row still counts them.
    """
    # Schemas carry API-safe aliases; ``tool_names`` maps them back.
    aliases = tool_names or {}
    by_server: dict[str, list[Mapping[str, Any]]] = {}
    for schema in tool_schemas:
        alias = _schema_name(schema)
        server = mcp_server_of(aliases.get(alias, alias))
        if server is not None:
            by_server.setdefault(server, []).append(schema)
    rows = []
    for server in servers:
        name = str(server.get("name") or "")
        connected = server.get("status") == "connected"
        status = "disabled" if connected and name in disabled else str(server.get("status") or "failed")
        # A turned-off server keeps its stubs in the list (the prompt cache
        # survives the toggle), so they still count.
        loaded = by_server.get(name, []) if connected else []
        rows.append({
            "name": name,
            "status": status,
            "tool_count": len(server.get("tools") or ()),
            "tokens": estimate_tools_tokens(list(loaded)) if loaded else 0,
            "error": server.get("error"),
            # A server that never connected has nothing to switch on.
            "toggleable": connected,
        })
    return rows


__all__ = ["project_mcp_servers"]
