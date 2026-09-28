"""Format the shared history projection for a terminal.

Which messages are real user input is decided in
``application.session.history_projection``. This module only truncates and
labels attachments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...application.session.history_projection import project_history, seed_ids


@dataclass(frozen=True)
class AttachmentLabel:
    filename: str
    width: int
    height: int


def _items(session_state: Any, *, max_turns: int | None) -> list[dict[str, Any]]:
    turn_ids, run_ids = seed_ids(session_state)
    items = project_history(session_state, turn_ids, run_ids)
    if max_turns is None:
        return items
    return items[-max_turns:]


def collect_history_pairs(
    session_state: Any,
    *,
    max_turns: int | None = 5,
) -> list[tuple[str, str]]:
    """User/assistant pairs from the shared projection, in turn order."""

    pairs: list[tuple[str, str]] = []
    for item in _items(session_state, max_turns=max_turns):
        user_text = str(item.get("user") or "")
        answer = str(item.get("assistant") or "")
        if user_text or answer:
            pairs.append((user_text, answer))
    return pairs


def collect_history_entries(
    session_state: Any,
    *,
    max_turns: int | None = 5,
) -> list[tuple[str, str, tuple[AttachmentLabel, ...]]]:
    """History pairs plus safe attachment labels. No storage paths."""

    entries: list[tuple[str, str, tuple[AttachmentLabel, ...]]] = []
    for item in _items(session_state, max_turns=max_turns):
        labels = tuple(
            AttachmentLabel(
                filename=str(attachment.get("filename") or "image"),
                width=int(attachment.get("width") or 0),
                height=int(attachment.get("height") or 0),
            )
            for attachment in item.get("attachments") or []
            if isinstance(attachment, dict)
        )
        user_text = str(item.get("user") or "")
        answer = str(item.get("assistant") or "")
        if user_text or answer or labels:
            entries.append((user_text, answer, labels))
    return entries
