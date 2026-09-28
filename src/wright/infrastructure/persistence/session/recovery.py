"""Interrupted tool-call recovery for a resumed session.

Runs only after link validation and Session construction. It records an
unknown outcome; it does not replay the tool.
"""

from __future__ import annotations

from ....domain.model.session import Session
from ....domain.model.tool import ToolResult
from ....domain.protocol import build_tool_results_messages


def recover_interrupted_tool_calls(session: Session) -> None:
    """Close pending calls from a crashed process without replaying side effects."""
    # Insert missing results beside their assistant call, before any later
    # user/reminder message. Never replay a potentially completed side effect.
    answered = {
        record.message.get("tool_call_id")
        for record in session.message_records if record.message.get("role") == "tool"
    }
    for turn in session.turns:
        missing = []
        for call_id in turn.tool_execution_ids:
            execution = session.tool_executions[call_id]
            if call_id in answered:
                continue
            if execution.status in {"pending", "running"} or execution.result is None:
                execution.result = ToolResult.fail(
                    "Tool execution was interrupted by process restart; outcome is unknown. "
                    "Inspect the current state before deciding whether to retry.",
                    data={"error": {"type": "tool_execution_interrupted", "retriable": False}},
                )
                execution.status = "failed"
            missing.append((execution.call, execution.result))
        if not missing:
            continue
        position = next(
            index + 1 for index, record in enumerate(session.message_records)
            if record.id == turn.message_id
        )
        while (
            position < len(session.message_records)
            and session.message_records[position].message.get("role") == "tool"
        ):
            position += 1
        for message in build_tool_results_messages(missing):
            session.append_message(message)
            record = session.message_records.pop()
            session.message_records.insert(position, record)
            if position < session.active_turn_start_message_index:
                session.active_turn_start_message_index += 1
            position += 1
