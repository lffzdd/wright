"""Shared session control commands.

CLI and TUI parse the same verbs and call the same session methods. The
words are control syntax, not model instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class ControlAction:
    name: str
    argument: str = ""


_IMMEDIATE = {
    "/stop": "stop_current",
    "/stop-all": "stop_all",
    "/cancel": "cancel_queued",
    "/status": "status",
    "/model": "model",
    "/mode": "mode",
    "/permission": "permission",
    "/ref": "reference",
    "/doc": "document",
    "/env": "environment",
    "/environment": "environment",
    "/close": "close",
    "/new": "new",
    "/resume": "resume",
    "/exit": "exit",
    "/quit": "exit",
}


def interpret_control(text: str) -> ControlAction | None:
    """Recognize a control line. Ordinary text, including ``@`` mentions, is not one."""

    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, tail = stripped.partition(" ")
    name = _IMMEDIATE.get(head.lower())
    if name is None:
        return None
    return ControlAction(name, tail.strip())


def apply_control(service: Any, action: ControlAction) -> dict[str, Any]:
    """Perform a control against an open session.

    Navigation (``new``, ``resume``, ``close``, ``exit``) is returned to the
    adapter. Stopping, answering policy, and staging references stay here.
    """

    if action.name in {"new", "resume", "close", "exit"}:
        return {"navigation": action.name, "argument": action.argument}
    if action.name == "stop_current":
        return service.stop_current()
    if action.name == "stop_all":
        return service.stop_all()
    if action.name == "cancel_queued":
        if not action.argument:
            raise ValueError("cancel requires the queued command id")
        return service.cancel_queued(uuid4().hex, action.argument)
    if action.name == "status":
        return service.activity()
    if action.name == "model":
        if not action.argument:
            return {"model": service.summary().get("model")}
        return service.set_model(action.argument)
    if action.name == "mode":
        if action.argument not in {"agent", "plan", "ask"}:
            raise ValueError("mode must be agent, plan, or ask")
        return service.set_execution_policy(interaction_mode=action.argument)
    if action.name == "permission":
        if action.argument not in {"default", "acceptEdits", "bypass", "plan"}:
            raise ValueError("permission must be default, acceptEdits, bypass, or plan")
        return service.set_execution_policy(permission_mode=action.argument)
    if action.name == "reference":
        if not action.argument:
            return {"references": service.staged_references()}
        return service.stage_reference(action.argument)
    if action.name == "document":
        path_text, data = _read_user_file(action.argument)
        return service.add_document(service.session_id, path_text, data)
    if action.name == "environment":
        summary = service.summary()
        return {
            "environment": summary.get("environment"),
            "execution_root": summary.get("execution_root"),
            "project_root": summary.get("project_root"),
            "fixed": True,
        }
    raise ValueError(f"unsupported control {action.name}")


def _read_user_file(argument: str) -> tuple[str, bytes]:
    from pathlib import Path

    if not argument:
        raise ValueError("document requires a file path")
    path = Path(argument).expanduser()
    if not path.is_file():
        raise ValueError("document path is not a file")
    return path.name, path.read_bytes()


__all__ = ["ControlAction", "apply_control", "interpret_control"]
