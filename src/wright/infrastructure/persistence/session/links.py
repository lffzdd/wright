"""Checkpoint association checks.

These run after records are decoded and before a Session is constructed.
They do not repair data.
"""

from __future__ import annotations

import re

from ....domain.model.session import MessageRecord, ToolExecutionRecord, TurnRecord
from .errors import CheckpointError


def validate_session_links(
    messages: list[MessageRecord],
    turns: list[TurnRecord],
    executions: dict[str, ToolExecutionRecord],
    step_count: int,
    active_turn_start_step: int,
    active_turn_start_message_index: int,
    message_id_counter: int,
) -> None:
    message_ids = [record.id for record in messages]
    if len(message_ids) != len(set(message_ids)):
        raise CheckpointError("message id 重复")
    message_by_id = {record.id: record for record in messages}

    turn_steps = [turn.step for turn in turns]
    if turn_steps != sorted(turn_steps) or len(turn_steps) != len(set(turn_steps)):
        raise CheckpointError("turn step 必须严格递增且唯一")
    if turn_steps and step_count < turn_steps[-1]:
        raise CheckpointError("step_count 小于已记录 turn")
    if active_turn_start_step > step_count:
        raise CheckpointError("active_turn_start_step 不能大于 step_count")
    if active_turn_start_message_index > len(messages):
        raise CheckpointError(
            "active_turn_start_message_index 不能大于 message 数量"
        )

    for turn in turns:
        message = message_by_id.get(turn.message_id)
        if message is None or message.message.get("role") != "assistant":
            raise CheckpointError(f"turn 引用的 assistant message 不存在: {turn.message_id}")
        if turn.route == "tool_calls" and not turn.tool_execution_ids:
            raise CheckpointError("tool_calls turn 缺少 tool execution")
        if turn.route != "tool_calls" and turn.tool_execution_ids:
            raise CheckpointError("非 tool_calls turn 不能关联 tool execution")
        for call_id in turn.tool_execution_ids:
            execution = executions.get(call_id)
            if execution is None or execution.step != turn.step:
                raise CheckpointError(f"turn/tool execution 关联不一致: {call_id}")

    numeric_message_ids = [
        int(match.group(1))
        for message_id in message_ids
        if (match := re.fullmatch(r"msg_(\d+)", message_id))
    ]
    if numeric_message_ids and message_id_counter < max(numeric_message_ids):
        raise CheckpointError("message_id_counter 小于已分配 message id")
