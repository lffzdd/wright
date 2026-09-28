"""Project session records into the shared web history shape.

The event ring is not the history store. A turn or terminal run becomes
visible only after its completion event is published, or because it already
existed when this process started.
"""

from __future__ import annotations

from typing import Any

from ...domain.model.session.run import TERMINAL_RUN_STATUSES

_PHASES = {
    "pending": "planned",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "timeout": "failed",
}


def turn_history_id(turn: Any) -> str:
    step_id = str(getattr(turn, "step_id", "") or "")
    if step_id:
        return step_id
    return f"{getattr(turn, 'run_id', '')}:{getattr(turn, 'step', 0)}"


def current_run_id(session: Any) -> str:
    current = getattr(session, "current_run", None)
    run = current() if callable(current) else None
    if run is None:
        active = getattr(session, "active_run", None)
        run = active() if callable(active) else None
    return str(getattr(run, "run_id", "") or "")


def latest_turn_history_id(session: Any, run_id: str = "") -> str | None:
    turns = list(getattr(session, "turns", None) or [])
    if run_id:
        turns = [turn for turn in turns if str(getattr(turn, "run_id", "") or "") == run_id]
    if not turns:
        return None
    return turn_history_id(turns[-1])


def seed_ids(session: Any) -> tuple[set[str], set[str]]:
    turn_ids = {turn_history_id(turn) for turn in getattr(session, "turns", None) or []}
    run_ids = {
        str(run.run_id)
        for run in getattr(session, "runs", {}).values()
        if getattr(run, "status", "") in TERMINAL_RUN_STATUSES
    }
    return turn_ids, run_ids


def project_history(
    session: Any,
    published_turns: set[str],
    published_runs: set[str],
) -> list[dict[str, Any]]:
    records = getattr(session, "message_records", None)
    if not isinstance(records, list):
        records = []
    id_to_index = {record.id: index for index, record in enumerate(records)}
    history: list[dict[str, Any]] = []
    covered_runs: set[str] = set()
    for turn in getattr(session, "turns", None) or []:
        if turn.route == "tool_calls":
            continue
        identity = turn_history_id(turn)
        run_id = str(getattr(turn, "run_id", "") or "")
        if identity not in published_turns and run_id not in published_runs:
            continue
        covered_runs.add(run_id)
        answer = turn.parsed.get("final_answer", "") if isinstance(turn.parsed, dict) else ""
        if not isinstance(answer, str):
            answer = str(answer)
        status = "failed" if turn.route == "invalid" or turn.error else "completed"
        assistant = answer.strip() or str(turn.error or "")
        user_text = _user_text(records, id_to_index.get(turn.message_id, 0))
        attachments = attachment_summaries(session, user_text["attachment_ids"])
        item: dict[str, Any] = {
            "turn_id": identity,
            "run_id": run_id,
            "status": status,
            "user": user_text["text"],
            "assistant": assistant,
        }
        if attachments:
            item["attachments"] = attachments
        tools = _run_tools(session, run_id)
        if tools:
            item["tools"] = tools
        history.append(item)
    for run in getattr(session, "runs", {}).values():
        run_id = str(run.run_id)
        if run_id in covered_runs or run_id not in published_runs:
            continue
        if run.status not in TERMINAL_RUN_STATUSES:
            continue
        assistant = str(run.result or run.error or "")
        item = {
            "turn_id": run_id,
            "run_id": run_id,
            "status": run.status,
            "user": str(run.goal or ""),
            "assistant": assistant,
        }
        tools = _run_tools(session, run_id)
        if tools:
            item["tools"] = tools
        history.append(item)
    return history


def _user_text(records: list[Any], start: int) -> dict[str, Any]:
    for index in range(start - 1, -1, -1):
        message = records[index].message
        if message.get("role") != "user":
            continue
        text = message.get("content", "")
        if not isinstance(text, str) or text.lstrip().startswith("<"):
            continue
        attachment_ids = message.get("attachments", [])
        return {"text": text.strip(), "attachment_ids": attachment_ids if isinstance(attachment_ids, list) else []}
    return {"text": "", "attachment_ids": []}


def _run_tools(session: Any, run_id: str) -> list[dict[str, Any]]:
    run = getattr(session, "runs", {}).get(run_id)
    if run is None:
        return []
    tools: list[dict[str, Any]] = []
    executions = getattr(session, "tool_executions", {})
    for call_id in getattr(run, "tool_execution_ids", []):
        execution = executions.get(call_id)
        if execution is None:
            continue
        tool: dict[str, Any] = {
            "call_id": execution.call.id,
            "name": execution.call.name,
            "arguments": dict(execution.call.arguments),
            "phase": _PHASES.get(str(execution.status), "failed"),
        }
        if execution.result is not None:
            result = execution.result.to_dict()
            result.pop("storage_path", None)
            for artifact in result.get("artifacts") or []:
                if isinstance(artifact, dict):
                    artifact.pop("storage_path", None)
            tool.update(result)
        tools.append(tool)
    return tools


def attachment_summaries(session: Any, attachment_ids: list[str]) -> list[dict[str, Any]]:
    if not attachment_ids:
        return []
    records = session.attachment_records(attachment_ids)
    return [record.to_dict() for record in records]
