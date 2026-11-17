"""Persistent pointers back to the session messages a memory was learned from."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class LocatorError(ValueError):
    pass


@dataclass(frozen=True)
class SourceLocator:
    """A bare local id such as ``ev-u-msg_1`` is not stored: it repeats across
    sessions. Callers map it onto this locator first. Parsing never accepts a
    token without a session.
    """

    session_id: str = ""
    root_run_id: str = ""
    run_id: str = ""
    kind: str = ""
    message_id: str = ""
    tool_call_id: str = ""
    step_id: str = ""
    episode_id: str = ""
    local_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "root_run_id": self.root_run_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "message_id": self.message_id,
            "tool_call_id": self.tool_call_id,
            "step_id": self.step_id,
            "episode_id": self.episode_id,
            "local_id": self.local_id,
        }


_FIELDS = (
    ("sess", "session_id"),
    ("root", "root_run_id"),
    ("run", "run_id"),
    ("kind", "kind"),
    ("msg", "message_id"),
    ("tool", "tool_call_id"),
    ("step", "step_id"),
    ("ep", "episode_id"),
    ("local", "local_id"),
)


def encode_locator(locator: SourceLocator) -> str:
    if not locator.session_id:
        raise LocatorError("locator must include a session")
    parts: list[str] = []
    for key, attribute in _FIELDS:
        value = getattr(locator, attribute)
        if not value:
            continue
        if any(char in value for char in ";=\n"):
            raise LocatorError("locator contains a separator")
        parts.append(f"{key}={value}")
    return ";".join(parts)


def parse_locator(token: str) -> SourceLocator:
    text = str(token or "").strip()
    fields: dict[str, str] = {}
    for part in text.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value
    if not fields.get("sess"):
        raise LocatorError("locator must include a session")
    return SourceLocator(**{attribute: fields.get(key, "") for key, attribute in _FIELDS})


def is_locator(token: str) -> bool:
    return isinstance(token, str) and token.startswith("sess=")


__all__ = ["LocatorError", "SourceLocator", "encode_locator", "is_locator", "parse_locator"]
