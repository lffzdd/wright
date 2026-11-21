"""Load an explicit hook config and connect it to a lifecycle manager."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...domain.model.lifecycle import (
    HOOKABLE_EVENT_NAMES,
    HookRegistration,
    LifecycleConfigError,
)
from ...infrastructure.lifecycle import CommandHook, TraceRecorder
from ..lifecycle.manager import LifecycleManager


@dataclass(frozen=True)
class HookEntry:
    event: str
    name: str
    matcher: str
    command: tuple[str, ...]
    timeout: float


def read_hook_config(config_path: Path) -> list[HookEntry]:
    """Parse a hooks file. Raises LifecycleConfigError on anything unusable."""
    path = config_path.expanduser().resolve()
    if not path.exists():
        raise LifecycleConfigError(f"hook config does not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleConfigError(f"cannot load hook config {path}: {exc}") from exc
    hooks = raw.get("hooks") if isinstance(raw, dict) else None
    if not isinstance(hooks, dict):
        raise LifecycleConfigError("hook config requires an object field 'hooks'")
    entries: list[HookEntry] = []
    for event, rows in hooks.items():
        if event not in HOOKABLE_EVENT_NAMES:
            raise LifecycleConfigError(f"unknown hook event: {event}")
        if not isinstance(rows, list):
            raise LifecycleConfigError(f"hooks.{event} must be an array")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise LifecycleConfigError(f"hooks.{event}[{index}] must be an object")
            command = row.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
                raise LifecycleConfigError(
                    f"hooks.{event}[{index}].command must be a non-empty argv array"
                )
            try:
                timeout = float(row.get("timeout", 5))
            except (TypeError, ValueError) as exc:
                raise LifecycleConfigError(
                    f"hooks.{event}[{index}].timeout must be a number"
                ) from exc
            if timeout <= 0 or timeout > 60:
                raise LifecycleConfigError(f"hooks.{event}[{index}].timeout must be in (0, 60] seconds")
            entries.append(HookEntry(
                event=event,
                name=str(row.get("name") or f"{event}[{index}]")[:100],
                matcher=str(row.get("matcher") or "*"),
                command=tuple(command),
                timeout=timeout,
            ))
    return entries


def describe_hooks(config_path: str) -> dict:
    """The hooks file named by ``hooks.config``, for the Settings page."""
    if not config_path:
        return {"path": "", "hooks": [], "error": None}
    try:
        entries = read_hook_config(Path(config_path))
    except LifecycleConfigError as exc:
        return {"path": config_path, "hooks": [], "error": str(exc)}
    return {
        "path": config_path,
        "error": None,
        "hooks": [
            {"event": item.event, "name": item.name, "matcher": item.matcher,
             "command": list(item.command), "timeout": item.timeout}
            for item in entries
        ],
    }


def load_lifecycle_manager(
    workspace_dir: Path,
    session_id: str,
    *,
    config_path: Path | None = None,
    trace_dir: Path | None = None,
) -> LifecycleManager:
    """Create tracing and load only an explicitly selected command-hook file."""
    workspace = workspace_dir.resolve()
    traces = (trace_dir or (workspace / ".wright_traces")).resolve()
    manager = LifecycleManager(
        session_id,
        TraceRecorder(traces / f"{session_id}.jsonl"),
    )
    if config_path is None:
        return manager
    for hook in read_hook_config(config_path):
        manager.register(
            HookRegistration(
                event=hook.event,
                callback=CommandHook(list(hook.command), cwd=workspace, timeout=hook.timeout),
                matcher=hook.matcher,
                name=hook.name,
            )
        )
    return manager
