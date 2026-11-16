"""The ``/settings`` slash command shared by the CLI REPL and the TUI."""

from __future__ import annotations

import json
from typing import Any

from ...domain.model.settings import SPECS_BY_KEY, SettingsError
from .service import SettingsService

USAGE = "/settings [set [--project] KEY VALUE | unset [--project] KEY]"


def run_settings_command(text: str, service: SettingsService) -> str:
    """Run one ``/settings`` line and return the text to show."""
    words = text.strip().split()[1:]
    if not words:
        return format_settings(service.describe())
    action, rest = words[0].lower(), words[1:]
    if action not in {"set", "unset"}:
        raise SettingsError(f"Usage: {USAGE}")
    scope = "user"
    if rest and rest[0] == "--project":
        scope, rest = "project", rest[1:]
    if (action == "set" and len(rest) < 2) or (action == "unset" and len(rest) != 1):
        raise SettingsError(f"Usage: {USAGE}")
    key = rest[0]
    if key not in SPECS_BY_KEY:
        raise SettingsError(f"Unknown setting {key}; run /settings to list them")
    value = " ".join(rest[1:]) if action == "set" else None
    service.update(scope, key, value)  # type: ignore[arg-type]
    view = service.describe()
    item = next(entry for entry in view["settings"] if entry["key"] == key)
    path = view["paths"][scope]
    if value is None:
        lines = [f"Removed {key} from {path}."]
    else:
        lines = [f"Saved {key} = {_show(item['user_value' if scope == 'user' else 'project_value'])} to {path}."]
    lines.append(f"Effective: {_show(item['value'])} ({_source(item)}). New sessions use it.")
    if item["env_override"]:
        lines.append(f"Note: {' / '.join(item['env'])} is set and overrides the files.")
    return "\n".join(lines)


def format_settings(view: dict[str, Any]) -> str:
    items = view["settings"]
    width = max(len(item["key"]) for item in items)
    value_width = min(32, max(len(_show(item["value"])) for item in items))
    lines = [
        "Settings (new sessions use these; environment variables override the files)",
        f"  user file     {view['paths']['user']}",
        f"  project file  {view['paths']['project'] or '(no project)'}",
        "",
    ]
    for item in items:
        project_note = "" if "project" in item["scopes"] else "  user-only"
        lines.append(
            f"  {item['key']:<{width}}  {_show(item['value']):<{value_width}}  "
            f"{_source(item)}{project_note}"
        )
    for warning in view["warnings"]:
        lines.append(f"! {warning}")
    lines.append("")
    lines.append(f"Change one with {USAGE}")
    return "\n".join(lines)


def _source(item: dict[str, Any]) -> str:
    if item["source"] == "env":
        return f"env {' / '.join(item['env'])}"
    return str(item["source"])


def _show(value: Any) -> str:
    if value is None or value == "":
        return "(empty)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item) for item in value) or "(empty)"
    if isinstance(value, str):
        return value
    return json.dumps(value)


__all__ = ["USAGE", "format_settings", "run_settings_command"]
