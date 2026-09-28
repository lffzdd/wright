"""Project durable session records into terminal history rows.

This projection reads session data only. It does not import a console renderer.
"""

from __future__ import annotations

import json
from typing import Any


def collect_history_pairs(
    session_state: Any,
    *,
    max_turns: int | None = 5,
) -> list[tuple[str, str]]:
    """User/assistant pairs from final turns, newest-deduped.

    ``max_turns=None`` keeps every final turn; otherwise the last N final turns
    are considered before pairing.
    """
    final_turns = [t for t in session_state.turns if t.route == "final"]
    if not final_turns:
        return []
    recent = final_turns if max_turns is None else final_turns[-max_turns:]
    id_to_idx: dict[str, int] = {
        r.id: i for i, r in enumerate(session_state.message_records)
    }
    pairs: list[tuple[str, str]] = []
    for turn in recent:
        final_answer = turn.parsed.get("final_answer", "")
        if not isinstance(final_answer, str):
            try:
                final_answer = json.dumps(final_answer, ensure_ascii=False)
            except (TypeError, ValueError):
                final_answer = str(final_answer)
        if not final_answer.strip():
            continue
        asst_idx = id_to_idx.get(turn.message_id, -1)
        user_text = ""
        for i in range(asst_idx - 1, -1, -1):
            rec = session_state.message_records[i]
            role = rec.message.get("role", "")
            if role != "user":
                continue
            content = rec.message.get("content", "")
            if not isinstance(content, str):
                continue
            stripped = content.lstrip()
            if stripped.startswith("<system-reminder>"):
                continue
            if stripped.startswith("<task-notification>"):
                continue
            if stripped.startswith("{") and any(
                k in stripped[:120]
                for k in ("tool_results", "verification_feedback")
            ):
                continue
            user_text = content
            break
        if user_text or final_answer:
            pairs.append((user_text.strip(), final_answer.strip()))
    deduped: list[tuple[str, str]] = []
    for user_text, answer_text in pairs:
        if deduped and deduped[-1][0] == user_text:
            deduped[-1] = (user_text, answer_text)
        else:
            deduped.append((user_text, answer_text))
    return deduped


def collect_history_entries(
    session_state: Any,
    *,
    max_turns: int | None = 5,
) -> list[tuple[str, str, tuple[Any, ...]]]:
    """History pairs plus safe, byte-free attachment labels for terminal UIs."""
    final_turns = [turn for turn in session_state.turns if turn.route == "final"]
    recent = final_turns if max_turns is None else final_turns[-max_turns:]
    positions = {record.id: index for index, record in enumerate(session_state.message_records)}
    registry = getattr(session_state, "attachments", {})
    entries: list[tuple[str, str, tuple[Any, ...]]] = []
    for turn in recent:
        answer = turn.parsed.get("final_answer", "")
        if not isinstance(answer, str):
            try:
                answer = json.dumps(answer, ensure_ascii=False)
            except (TypeError, ValueError):
                answer = str(answer)
        if not answer.strip():
            continue
        message: dict[str, Any] | None = None
        for record in reversed(session_state.message_records[:positions.get(turn.message_id, 0)]):
            candidate = record.message
            content = candidate.get("content", "")
            if candidate.get("role") != "user" or not isinstance(content, str):
                continue
            stripped = content.lstrip()
            if stripped.startswith(("<system-reminder>", "<task-notification>")):
                continue
            if stripped.startswith("{") and any(
                key in stripped[:120] for key in ("tool_results", "verification_feedback")
            ):
                continue
            message = candidate
            break
        if message is None:
            continue
        attachment_ids = message.get("attachments", ())
        attachments = tuple(
            registry[attachment_id]
            for attachment_id in attachment_ids
            if isinstance(attachment_ids, list) and attachment_id in registry
        )
        entry = (str(message.get("content", "")).strip(), answer.strip(), attachments)
        if entries and entries[-1][0] == entry[0]:
            entries[-1] = entry
        else:
            entries.append(entry)
    return entries

