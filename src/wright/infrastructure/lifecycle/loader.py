"""Load an explicit hook config and connect it to a lifecycle manager."""

from __future__ import annotations

import json
from pathlib import Path

from ...application.lifecycle.contracts import (
    HOOKABLE_EVENT_NAMES,
    HookRegistration,
    LifecycleConfigError,
)
from ...application.lifecycle.manager import LifecycleManager
from .command_hook import CommandHook
from .trace_recorder import TraceRecorder


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
    path = config_path.resolve()
    if not path.exists():
        raise LifecycleConfigError(f"hook config does not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleConfigError(f"cannot load hook config {path}: {exc}") from exc
    hooks = raw.get("hooks") if isinstance(raw, dict) else None
    if not isinstance(hooks, dict):
        raise LifecycleConfigError("hook config requires an object field 'hooks'")
    for event, rows in hooks.items():
        if event not in HOOKABLE_EVENT_NAMES:
            raise LifecycleConfigError(f"unknown hook event: {event}")
        if not isinstance(rows, list):
            raise LifecycleConfigError(f"hooks.{event} must be an array")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise LifecycleConfigError(f"hooks.{event}[{index}] must be an object")
            command = row.get("command")
            if not isinstance(command, list):
                raise LifecycleConfigError(
                    f"hooks.{event}[{index}].command must be an argv array"
                )
            name = str(row.get("name") or f"{event}[{index}]")[:100]
            try:
                timeout = float(row.get("timeout", 5))
            except (TypeError, ValueError) as exc:
                raise LifecycleConfigError(
                    f"hooks.{event}[{index}].timeout must be a number"
                ) from exc
            manager.register(
                HookRegistration(
                    event=event,
                    callback=CommandHook(
                        command,
                        cwd=workspace,
                        timeout=timeout,
                    ),
                    matcher=str(row.get("matcher") or "*"),
                    name=name,
                )
            )
    return manager
