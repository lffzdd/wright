"""Edit ``mcp.json`` files one server at a time."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...core.file_lock import FileLock
from ...core.paths import project_mcp_config_path, user_mcp_config_path
from ...domain.gateway.mcp_config import McpConfigUnreadable, McpScope
from .files import replace_text

_LOCK = threading.RLock()


class JsonMcpConfigStore:
    def __init__(self, project_root: Path | None) -> None:
        self.project_root = project_root

    def path(self, scope: McpScope) -> Path | None:
        if scope == "user":
            return user_mcp_config_path()
        return project_mcp_config_path(self.project_root) if self.project_root is not None else None

    def servers(self, scope: McpScope) -> Mapping[str, Any]:
        target = self.path(scope)
        if target is None or not target.is_file():
            return {}
        servers = _load(target).get("mcpServers", {})
        if not isinstance(servers, dict):
            raise McpConfigUnreadable(f"{target}: mcpServers must be an object")
        return servers

    def write_server(self, scope: McpScope, name: str, entry: Mapping[str, Any] | None) -> None:
        target = self.path(scope)
        if target is None:
            raise McpConfigUnreadable("No project is open")
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, FileLock(target.with_name(f"{target.name}.lock")):
            data = dict(_load(target)) if target.is_file() else {}
            servers = data.get("mcpServers")
            servers = dict(servers) if isinstance(servers, dict) else {}
            if entry is None:
                servers.pop(name, None)
            else:
                servers[name] = dict(entry)
            data["mcpServers"] = servers
            # The user file may hold tokens in env or headers.
            replace_text(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                         mode=0o600 if scope == "user" else 0o644)


def _load(path: Path) -> Mapping[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise McpConfigUnreadable(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise McpConfigUnreadable(f"{path} must contain a JSON object")
    return data


__all__ = ["JsonMcpConfigStore"]
