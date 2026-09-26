"""Capture one user turn's episode facts without reading a later turn.

Recall blocks, hook text, and runtime events are not user input.
"""

from __future__ import annotations

import re
from typing import Any

from ...domain.model.memory import (
    EVIDENCE_ID_RE,
    MAX_EPISODE_AGENTS,
    MAX_EPISODE_EVIDENCE,
    MAX_EPISODE_GOAL_CHARS,
    MAX_EPISODE_OUTCOME_CHARS,
    MAX_EPISODE_TOOLS,
    MAX_EVIDENCE_TEXT,
)
from ...domain.policy.memory import bounded_safe_text

_SUBJECT_KEYS = ("path", "file_path", "directory")

_RECALL_OR_HOOK_PREFIXES = (
    "<system-reminder",
    "<hook-additional-context",
)
_LIVE_AGENT_STATUSES = frozenset({"pending", "running"})


def real_user_texts(session_state: Any) -> list[str]:
    """User-authored text for the active turn only."""
    start = int(getattr(session_state, "active_turn_start_message_index", 0))
    records = getattr(session_state, "message_records", [])
    texts: list[str] = []
    for record in records[start:]:
        if getattr(record, "source", "") != "user_input":
            continue
        message = getattr(record, "message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        stripped = content.lstrip()
        if stripped.startswith(_RECALL_OR_HOOK_PREFIXES):
            continue
        texts.append(content)
    return texts


def live_agent_tasks(session_state: Any, root_turn_id: str) -> bool:
    """True when that turn's task tree still has a non-terminal agent."""

    def live(nodes: list[dict[str, Any]]) -> bool:
        for node in nodes:
            if node.get("status") in _LIVE_AGENT_STATUSES:
                return True
            children = node.get("children")
            if isinstance(children, list) and live(children):
                return True
        return False

    tree = session_state.control_plane.tree(root_turn_id)
    return live(tree)


def flatten_agents(session_state: Any, root_turn_id: str) -> tuple[dict[str, Any], ...]:
    tree = session_state.control_plane.tree_summary(root_turn_id)

    def flatten(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        for node in nodes:
            children = node.get("children", [])
            flattened.append({
                "id": node.get("id"),
                "parent_id": node.get("parent_id"),
                "depth": node.get("depth"),
                "task": str(node.get("task", ""))[:300],
                "status": node.get("status"),
                "steps_used": node.get("steps_used", 0),
                "total_tokens": node.get("total_tokens", 0),
                "result": str(node.get("result", ""))[:500],
                "error": str(node.get("error", ""))[:500],
            })
            if isinstance(children, list):
                flattened.extend(flatten(children))
        return flattened

    return tuple(flatten(tree)[:MAX_EPISODE_AGENTS])


def _evidence_id(prefix: str, raw: str, seen: set[str]) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", str(raw)).strip("-")[:100] or "item"
    base = f"ev-{prefix}-{safe}"
    if len(base) > 161:
        base = base[:161]
    candidate = base
    number = 2
    while candidate in seen or EVIDENCE_ID_RE.fullmatch(candidate) is None:
        suffix = f"-{number}"
        candidate = base[: 161 - len(suffix)] + suffix
        number += 1
        if number > 20:
            return ""
    return candidate


def _message_text(record: Any) -> str:
    message = getattr(record, "message", {})
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""


def _tool_message_id(records: list[Any], start: int, call_id: str) -> str:
    for record in records[start:]:
        message = getattr(record, "message", {})
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
            return str(getattr(record, "id", "") or "")
    return ""


def _command_and_subject(arguments: Any) -> tuple[str, str]:
    if not isinstance(arguments, dict):
        return "", ""
    command = ""
    raw_command = arguments.get("command")
    if isinstance(raw_command, str):
        command = bounded_safe_text(raw_command, 300)
    subject = ""
    for key in _SUBJECT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            subject = bounded_safe_text(value, 300)
            break
    return command, subject


def _exit_code(result: Any) -> int | None:
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return None
    for key in ("returncode", "exit_code"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def turn_evidence(session_state: Any) -> tuple[dict[str, Any], ...]:
    """Locators and short facts for this turn. Secrets and full output stay out.

    ``ok`` is copied only when a tool result exists. It is the tool status,
    not a test verdict and not proof the user's goal was met.
    """
    start_message = int(getattr(session_state, "active_turn_start_message_index", 0))
    start_step = int(getattr(session_state, "active_turn_start_step", 0))
    records = list(getattr(session_state, "message_records", []))
    root_run_id, active_run_id = root_run_identity(session_state)
    session_id = str(getattr(session_state, "session_id", "") or "")
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(payload: dict[str, Any]) -> None:
        if len(refs) >= MAX_EPISODE_EVIDENCE:
            return
        evidence_id = str(payload.get("id") or "")
        if not evidence_id or evidence_id in seen:
            return
        seen.add(evidence_id)
        refs.append(payload)

    for record in records[start_message:]:
        if getattr(record, "source", "") != "user_input":
            continue
        text = _message_text(record)
        stripped = text.lstrip()
        if stripped.startswith(_RECALL_OR_HOOK_PREFIXES):
            continue
        evidence_id = _evidence_id("u", str(getattr(record, "id", "")), seen)
        summary = bounded_safe_text(text, MAX_EVIDENCE_TEXT)
        if not evidence_id:
            continue
        add({
            "id": evidence_id,
            "kind": "user_statement",
            "session_id": session_id,
            "root_run_id": root_run_id,
            "run_id": root_run_id,
            "message_id": str(getattr(record, "id", "") or ""),
            "summary": summary,
        })

    executions = sorted(
        (
            execution
            for execution in getattr(session_state, "tool_executions", {}).values()
            if execution.step > start_step
        ),
        key=lambda execution: (execution.step, execution.call.id),
    )
    for execution in executions:
        if len(refs) >= MAX_EPISODE_EVIDENCE:
            break
        call = execution.call
        evidence_id = _evidence_id("t", call.id, seen)
        if not evidence_id:
            continue
        command, subject = _command_and_subject(call.arguments)
        result = execution.result
        error = ""
        exit_code = None
        ok = None
        if result is not None:
            ok = bool(result.ok)
            error = bounded_safe_text(result.err, 400)
            exit_code = _exit_code(result)
        bits = []
        if command:
            bits.append(f"command={command}")
        if subject:
            bits.append(f"subject={subject}")
        if execution.status:
            bits.append(f"status={execution.status}")
        if exit_code is not None:
            bits.append(f"exit={exit_code}")
        if error:
            bits.append(f"error={error}")
        payload: dict[str, Any] = {
            "id": evidence_id,
            "kind": "tool_observation",
            "session_id": session_id,
            "root_run_id": root_run_id,
            "run_id": str(execution.run_id or active_run_id or ""),
            "message_id": _tool_message_id(records, start_message, call.id),
            "tool_call_id": str(call.id),
            "step_id": str(execution.step_id or ""),
            "summary": " ".join(bits)[:MAX_EVIDENCE_TEXT],
            "command": command,
            "subject": subject,
            "error_excerpt": error,
            "execution_status": str(execution.status or ""),
        }
        if ok is not None:
            payload["ok"] = ok
        if exit_code is not None:
            payload["exit_code"] = exit_code
        add(payload)

    final_turns = [
        turn
        for turn in getattr(session_state, "turns", [])
        if turn.step > start_step and turn.route == "final"
    ]
    if final_turns:
        turn = final_turns[-1]
        evidence_id = _evidence_id("a", turn.message_id, seen)
        text = ""
        for record in records:
            if getattr(record, "id", "") == turn.message_id:
                text = _message_text(record)
                break
        if evidence_id:
            add({
                "id": evidence_id,
                "kind": "assistant_statement",
                "session_id": session_id,
                "root_run_id": root_run_id,
                "run_id": str(turn.run_id or active_run_id or ""),
                "message_id": str(turn.message_id),
                "step_id": str(turn.step_id or ""),
                "summary": bounded_safe_text(text, MAX_EVIDENCE_TEXT),
            })

    for turn in getattr(session_state, "turns", []):
        if turn.step <= start_step or turn.verification is None:
            continue
        if len(refs) >= MAX_EPISODE_EVIDENCE:
            break
        evidence_id = _evidence_id("v", turn.step_id or f"step-{turn.step}", seen)
        if not evidence_id:
            continue
        issues = turn.verification.issues or []
        issue_text = bounded_safe_text(
            "; ".join(
                str(issue.get("message") or issue.get("code") or "")
                if isinstance(issue, dict)
                else str(issue)
                for issue in issues
            ),
            400,
        )
        summary = f"approved={turn.verification.approved}"
        if issue_text:
            summary += f" issues={issue_text}"
        add({
            "id": evidence_id,
            "kind": "verification_record",
            "session_id": session_id,
            "root_run_id": root_run_id,
            "run_id": str(turn.run_id or ""),
            "message_id": str(turn.message_id or ""),
            "step_id": str(turn.step_id or ""),
            "summary": summary[:MAX_EVIDENCE_TEXT],
            "error_excerpt": issue_text,
        })
    return tuple(refs)


def turn_tools(session_state: Any) -> tuple[dict[str, Any], ...]:
    start = int(getattr(session_state, "active_turn_start_step", 0))
    executions = sorted(
        (
            execution
            for execution in getattr(session_state, "tool_executions", {}).values()
            if execution.step > start
        ),
        key=lambda execution: execution.step,
    )[:MAX_EPISODE_TOOLS]
    return tuple(
        {
            "step": execution.step,
            "name": execution.call.name,
            "status": execution.status,
            "ok": execution.result.ok if execution.result is not None else False,
            "error": (
                execution.result.err[:500]
                if execution.result is not None and execution.result.err
                else ""
            ),
        }
        for execution in executions
    )


def turn_verification(session_state: Any) -> tuple[dict[str, Any], ...]:
    start = int(getattr(session_state, "active_turn_start_step", 0))
    return tuple(
        {
            "step": turn.step,
            "approved": turn.verification.approved,
            "issues": turn.verification.issues,
        }
        for turn in getattr(session_state, "turns", [])
        if turn.step > start and turn.verification is not None
    )


def turn_usage(session_state: Any) -> dict[str, int]:
    start = int(getattr(session_state, "active_turn_start_step", 0))
    current_turns = [
        turn for turn in getattr(session_state, "turns", []) if turn.step > start
    ]
    return {
        "prompt_tokens": sum(
            turn.usage.prompt_tokens for turn in current_turns if turn.usage is not None
        ),
        "completion_tokens": sum(
            turn.usage.completion_tokens for turn in current_turns if turn.usage is not None
        ),
        "total_tokens": sum(
            turn.usage.total_tokens for turn in current_turns if turn.usage is not None
        ),
    }


def terminal_status(
    session_state: Any,
    *,
    termination_reason: str | None = None,
) -> tuple[str, str]:
    """Map a run finish onto episode status. Step exhaustion stays failed/max_steps."""
    run = session_state.active_run() or session_state.current_run()
    run_status = run.status if run is not None else "failed"
    error = run.error if run is not None else ""
    reason = termination_reason or ""
    if reason == "max_steps" or error == "max steps reached" or run_status == "max_steps":
        return "failed", "max_steps"
    if run_status == "completed":
        return "completed", reason
    if run_status == "cancelled":
        return "cancelled", reason or "cancelled"
    if run_status == "interrupted":
        return "failed", reason or "interrupted"
    return "failed", reason or "failed"


def bounded_outcome(final_answer: str | None) -> str:
    """Store the delivered answer. Absence is an empty outcome, not a summary."""
    if not isinstance(final_answer, str) or not final_answer.strip():
        return ""
    return final_answer[:MAX_EPISODE_OUTCOME_CHARS]


def current_goal(session_state: Any) -> str:
    goal_reader = getattr(session_state, "current_goal", None)
    goal = str(goal_reader() if callable(goal_reader) else "")
    return goal[:MAX_EPISODE_GOAL_CHARS]


def root_run_identity(session_state: Any) -> tuple[str, str]:
    run = session_state.active_run() or session_state.current_run()
    if run is None:
        return "", ""
    root_run_id = str(run.root_run_id or run.run_id)
    return root_run_id, str(run.run_id)


def sync_snapshot_agents(snapshot: dict[str, Any], session_state: Any) -> None:
    """Refresh agent status from that turn's task tree only.

    Goal, plan, and tool records on the snapshot are left unchanged.
    """
    root_turn_id = str(snapshot.get("root_turn_id") or "")
    current = {
        agent["id"]: agent
        for agent in flatten_agents(session_state, root_turn_id)
        if agent.get("id")
    }
    episode = snapshot.get("episode")
    if not isinstance(episode, dict):
        return
    agents = episode.get("agents")
    if not isinstance(agents, list):
        return
    updated: list[dict[str, Any]] = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        match = current.get(agent.get("id"))
        if match is None:
            updated.append(agent)
            continue
        updated.append({
            **agent,
            "status": match.get("status"),
            "result": match.get("result") or agent.get("result") or "",
            "error": match.get("error") or "",
            "steps_used": match.get("steps_used", agent.get("steps_used", 0)),
        })
    episode["agents"] = updated


def apply_task_update(snapshot: dict[str, Any], task: dict[str, Any]) -> None:
    """Apply one background notification onto the stored agent row."""
    episode = snapshot.get("episode")
    if not isinstance(episode, dict):
        return
    task_id = task.get("id")
    agents = episode.get("agents")
    if not isinstance(agents, list) or not task_id:
        return
    found = False
    updated: list[dict[str, Any]] = []
    for agent in agents:
        if isinstance(agent, dict) and agent.get("id") == task_id:
            found = True
            updated.append({
                **agent,
                "status": task.get("status", agent.get("status")),
                "result": str(task.get("result") or agent.get("result") or "")[:500],
                "error": str(task.get("error") or agent.get("error") or "")[:500],
            })
        elif isinstance(agent, dict):
            updated.append(agent)
    if not found:
        updated.append({
            "id": task_id,
            "parent_id": task.get("parent_id"),
            "depth": task.get("depth", 1),
            "task": str(task.get("task") or "")[:300],
            "status": task.get("status"),
            "steps_used": task.get("steps_used", 0),
            "total_tokens": task.get("total_tokens", 0),
            "result": str(task.get("result") or "")[:500],
            "error": str(task.get("error") or "")[:500],
        })
    episode["agents"] = updated[:MAX_EPISODE_AGENTS]


__all__ = [
    "apply_task_update",
    "bounded_outcome",
    "current_goal",
    "flatten_agents",
    "live_agent_tasks",
    "real_user_texts",
    "root_run_identity",
    "sync_snapshot_agents",
    "terminal_status",
    "turn_evidence",
    "turn_tools",
    "turn_usage",
    "turn_verification",
]
