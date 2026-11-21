"""MCP servers as configured on disk, for the Settings page.

This reads and edits the files only; whether a server connects is shown per
session. Values of ``env`` and ``headers`` often hold tokens, so they are
never returned: the page sees their names, and an edit that leaves a value
out keeps the stored one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ...domain.gateway.mcp_config import McpConfigStore, McpScope

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TRANSPORTS = ("stdio", "http", "sse")


class McpConfigError(ValueError):
    pass


class McpConfigService:
    def __init__(self, store: McpConfigStore, *, project_trusted: bool) -> None:
        self.store = store
        self.project_trusted = project_trusted

    def describe(self) -> dict[str, Any]:
        layers: dict[str, Any] = {}
        names: dict[str, set[str]] = {}
        for scope in ("user", "project"):
            path = self.store.path(scope)
            if path is None:
                continue
            try:
                servers = self.store.servers(scope)
                error = None
            except ValueError as exc:
                servers, error = {}, str(exc)
            names[scope] = set(servers)
            layers[scope] = {"path": str(path), "error": error, "servers": servers}
        rows = []
        for scope, layer in layers.items():
            for name, entry in layer["servers"].items():
                rows.append({
                    **_public(entry),
                    "name": name,
                    "scope": scope,
                    # A trusted project server of the same name replaces the user one.
                    "shadowed": scope == "user" and self.project_trusted and name in names.get("project", set()),
                    "active": scope == "user" or self.project_trusted,
                })
        return {
            "project_trusted": self.project_trusted,
            "files": {scope: {"path": layer["path"], "error": layer["error"]} for scope, layer in layers.items()},
            "servers": sorted(rows, key=lambda row: (row["name"], row["scope"])),
        }

    def save(self, scope: McpScope, name: str, form: Mapping[str, Any], *, previous: str | None = None) -> dict[str, Any]:
        self._check_scope(scope)
        if not _NAME.match(name):
            raise McpConfigError("Name may use letters, digits, '.', '_' and '-' (64 at most)")
        stored = self._servers(scope)
        old_name = previous or name
        old = stored.get(old_name) if isinstance(stored.get(old_name), dict) else {}
        if previous and previous != name and name in stored:
            raise McpConfigError(f"A server named {name} already exists")
        entry = _entry(form, old)
        self.store.write_server(scope, name, entry)
        if previous and previous != name:
            self.store.write_server(scope, previous, None)
        return self.describe()

    def remove(self, scope: McpScope, name: str) -> dict[str, Any]:
        self._check_scope(scope)
        if name not in self._servers(scope):
            raise McpConfigError(f"No {scope} server named {name}")
        self.store.write_server(scope, name, None)
        return self.describe()

    def _servers(self, scope: McpScope) -> Mapping[str, Any]:
        try:
            return self.store.servers(scope)
        except ValueError as exc:
            raise McpConfigError(str(exc)) from exc

    def _check_scope(self, scope: str) -> None:
        if scope not in {"user", "project"}:
            raise McpConfigError("Scope must be user or project")
        if self.store.path(scope) is None:
            raise McpConfigError("No project is open")


def _transport(entry: Mapping[str, Any]) -> str:
    kind = entry.get("type")
    if kind == "streamable-http":
        return "http"
    if kind in _TRANSPORTS:
        return str(kind)
    return "stdio" if entry.get("command") else "http" if entry.get("url") else "unknown"


def _public(entry: object) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        return {"transport": "unknown", "problem": "The entry is not an object"}
    transport = _transport(entry)
    env = entry.get("env") if isinstance(entry.get("env"), Mapping) else {}
    headers = entry.get("headers") if isinstance(entry.get("headers"), Mapping) else {}
    problem = None
    if transport == "stdio" and not entry.get("command"):
        problem = "stdio needs a command"
    elif transport in {"http", "sse"} and not entry.get("url"):
        problem = f"{transport} needs a url"
    elif transport == "unknown":
        problem = "Set a command or a url"
    return {
        "transport": transport,
        "command": str(entry.get("command") or ""),
        "args": [str(item) for item in entry.get("args") or []] if isinstance(entry.get("args"), list) else [],
        "url": str(entry.get("url") or ""),
        "env": sorted(str(key) for key in env),
        "headers": sorted(str(key) for key in headers),
        "problem": problem,
    }


def _entry(form: Mapping[str, Any], old: Mapping[str, Any]) -> dict[str, Any]:
    transport = form.get("transport")
    if transport not in _TRANSPORTS:
        raise McpConfigError("Transport must be stdio, http or sse")
    entry: dict[str, Any] = {}
    if transport == "stdio":
        command = str(form.get("command") or "").strip()
        if not command:
            raise McpConfigError("stdio needs a command")
        args = form.get("args") or []
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise McpConfigError("args must be a list of strings")
        entry.update(type="stdio", command=command)
        if args:
            entry["args"] = list(args)
        values = _merge(form.get("env"), old.get("env"), "env")
        if values:
            entry["env"] = values
    else:
        url = str(form.get("url") or "").strip()
        if not re.match(r"^https?://\S+$", url):
            raise McpConfigError("url must start with http:// or https://")
        entry.update(type=transport, url=url)
        values = _merge(form.get("headers"), old.get("headers"), "headers")
        if values:
            entry["headers"] = values
    return entry


def _merge(given: object, stored: object, label: str) -> dict[str, str]:
    """Names from the form; a missing value keeps the stored one."""
    if given is None:
        return {}
    if not isinstance(given, Mapping):
        raise McpConfigError(f"{label} must be an object")
    previous = stored if isinstance(stored, Mapping) else {}
    merged: dict[str, str] = {}
    for key, value in given.items():
        key = str(key).strip()
        if not key:
            continue
        if value is None:
            if key not in previous:
                raise McpConfigError(f"{label}.{key} needs a value")
            merged[key] = str(previous[key])
        elif isinstance(value, str):
            merged[key] = value
        else:
            raise McpConfigError(f"{label}.{key} must be text")
    return merged


__all__ = ["McpConfigError", "McpConfigService"]
