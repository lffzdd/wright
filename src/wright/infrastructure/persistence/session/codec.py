"""Session checkpoint codec.

File location, reading, and atomic replacement stay in FileSessionRepository.
Link checks and interrupted-tool recovery are separate steps called from
deserialize, in that order.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar, cast

from ....domain.model.agent.control import AgentControlError, AgentControlPlane
from ....domain.model.llm import UsageRecord
from ....domain.model.planning import PlanManager
from ....domain.model.session import (
    MessageRecord,
    Session,
    SessionAuthorization,
    SessionLifecycle,
    ToolExecutionRecord,
    ToolExecutionStatus,
    TurnRecord,
    TurnRoute,
    VerificationRecord,
)
from ....domain.model.session.errors import CheckpointError
from ....domain.model.session.run import RunRecord
from ....domain.model.tool import ArtifactRef, ToolCall, ToolResult
from ....domain.model.tool_names import canonical_tool_name
from ...storage.attachments import AttachmentError, AttachmentRecord
from .links import validate_session_links
from .recovery import recover_interrupted_tool_calls

CHECKPOINT_VERSION = 8
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_T = TypeVar("_T", bound=str)
_TURN_ROUTES: frozenset[TurnRoute] = frozenset({"tool_calls", "final", "invalid"})
_EXECUTION_STATUSES: frozenset[ToolExecutionStatus] = frozenset(
    {"pending", "running", "succeeded", "failed", "timeout"}
)
_MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool", "developer"})


def _serialize_session(session: Session) -> dict[str, Any]:
    authorization = session.authorization.snapshot()
    return {
        "version": CHECKPOINT_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "session": {
            "session_id": session.session_id,
            "lifecycle": session.lifecycle,
            "session_label": session.session_label,
            "workspace_dir": str(session.workspace_dir),
            "cwd": str(session.get_cwd()),
            "project_root": str(session.project_root or session.workspace_dir),
            "authorization": {
                "rules": [rule.to_persistent() for rule in authorization.rules],
                "interaction_mode": authorization.interaction_mode,
                "permission_mode": authorization.permission_mode,
            },
            "environment": session.environment,
            "execution_mode": session.execution_mode,
            "base_commit": session.base_commit,
            "branch_name": session.branch_name,
            "message_records": [
                {
                    "id": record.id,
                    "message": _json_safe(record.message),
                    "source": record.source,
                }
                for record in session.message_records
            ],
            "turns": [_serialize_turn(turn) for turn in session.turns],
            "runs": {
                run_id: {
                    "source": run.source, "goal": run.goal, "parent_run_id": run.parent_run_id,
                    "root_run_id": run.root_run_id, "status": run.status,
                    "started_at": run.started_at, "ended_at": run.ended_at, "result": run.result,
                    "error": run.error, "model_config": _json_safe(run.model_config),
                    "plan": _json_safe(run.plan), "usage": _json_safe(run.usage),
                    "step_ids": list(run.step_ids), "tool_execution_ids": list(run.tool_execution_ids),
                } for run_id, run in session.runs.items()
            },
            "active_run_id": session.active_run_id,
            "tool_executions": {
                call_id: {
                    "call": {
                        "id": execution.call.id,
                        "name": execution.call.name,
                        "arguments": _json_safe(execution.call.arguments),
                    },
                    "result": (
                        _json_safe(execution.result.to_dict())
                        if execution.result is not None
                        else None
                    ),
                    "step": execution.step,
                    "status": execution.status,
                    "started_at": execution.started_at,
                    "ended_at": execution.ended_at,
                    "run_id": execution.run_id,
                    "step_id": execution.step_id,
                }
                for call_id, execution in session.tool_executions.items()
            },
            "plan": session.plan_manager.snapshot(),
            "active_deferred_tools": list(session.active_deferred_tools),
            "agent_control": session.control_plane.snapshot(),
            "agent_task_id": session.agent_task_id,
            "agent_root_turn_id": session.agent_root_turn_id,
            "committed_turn_ids": list(session.committed_turn_ids),
            "pending_episode_finalizes": _json_safe(session.pending_episode_finalizes),
            "semantic_extract_receipts": _json_safe(
                getattr(session, "semantic_extract_receipts", {}) or {}
            ),
            "last_usage": _serialize_usage(session.last_usage),
            "total_usage": _serialize_usage(session.total_usage),
            "task_usage_start": _serialize_usage(session.task_usage_start),
            "model_name": session.model_name,
            "llm_transport": session.llm_transport,
            "request_context_breakdown": _json_safe(
                getattr(session, "request_context_breakdown", {}) or {}
            ),
            "documents": _json_safe(getattr(session, "documents", {}) or {}),
            "attachments": {
                attachment_id: record.to_dict()
                for attachment_id, record in session.attachments.items()
            },
            "context_tokens": session.context_tokens,
            "request_context_tokens": session.request_context_tokens,
            "step_count": session.step_count,
            "active_turn_start_step": session.active_turn_start_step,
            "active_turn_start_message_index": session.active_turn_start_message_index,
            "max_steps": session.max_steps,
            "message_id_counter": session.message_id_counter,
        },
    }


def _serialize_turn(turn: TurnRecord) -> dict[str, Any]:
    return {
        "step": turn.step,
        "message_id": turn.message_id,
        "parsed": _json_safe(turn.parsed),
        "route": turn.route,
        "tool_execution_ids": list(turn.tool_execution_ids),
        "error": turn.error,
        "usage": _serialize_usage(turn.usage),
        "verification": (
            {
                "approved": turn.verification.approved,
                "issues": _json_safe(turn.verification.issues),
            }
            if turn.verification is not None
            else None
        ),
        "run_id": turn.run_id,
        "step_id": turn.step_id,
    }


def _serialize_usage(usage: UsageRecord | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def _deserialize_session(payload: Any) -> Session:
    root = _object(payload, "checkpoint")
    version = root.get("version")
    if type(version) is not int or version != CHECKPOINT_VERSION:
        raise CheckpointError(f"Unsupported checkpoint version: {version}; only version {CHECKPOINT_VERSION} is accepted. Start a new session.")
    data = _object(root.get("session"), "session")

    session_id = _string(data.get("session_id"), "session_id")
    if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise CheckpointError("invalid session_id")
    lifecycle = _one_of(
        data.get("lifecycle", "open"),
        frozenset({"open", "closing", "closed"}),
        "session lifecycle",
    )
    workspace_dir = Path(_string(data.get("workspace_dir"), "workspace_dir"))
    cwd = Path(_string(data.get("cwd"), "cwd"))
    project_root = Path(_string(data.get("project_root"), "project_root"))
    if not all(path.is_absolute() for path in (workspace_dir, cwd, project_root)):
        raise CheckpointError("Checkpoint execution paths must be absolute")
    if any(key in data for key in ("permission_rules", "permission_data_version", "permission_parent", "permission_mode", "interaction_mode", "additional_working_directories")):
        raise CheckpointError("Flat session authorization fields are unsupported")
    authorization = _deserialize_authorization(data.get("authorization"), session_id)
    if "task_usage_start" not in data:
        raise CheckpointError("task_usage_start is required")
    execution_mode = data.get("execution_mode", "sandbox")
    if execution_mode not in {"sandbox", "host"}:
        raise CheckpointError("execution_mode must be sandbox or host")
    environment = data.get("environment", "local")
    if environment not in {"local", "worktree"}:
        raise CheckpointError("environment must be local or worktree")
    base_commit = _optional_string(data.get("base_commit"), "base_commit")
    branch_name = _optional_string(data.get("branch_name"), "branch_name")

    message_records = _deserialize_messages(data.get("message_records"))
    turns = _deserialize_turns(data.get("turns"))
    tool_executions = _deserialize_executions(data.get("tool_executions"))
    plan_manager = PlanManager.from_snapshot(_object(data.get("plan"), "plan"))
    active_deferred_value = data.get("active_deferred_tools", [])
    if not isinstance(active_deferred_value, list) or not all(
        isinstance(item, str) and item for item in active_deferred_value
    ):
        raise CheckpointError("active_deferred_tools must be an array of non-empty strings")
    active_deferred_tools = list(dict.fromkeys(active_deferred_value))
    try:
        control_plane = AgentControlPlane.from_snapshot(
            data.get("agent_control", {}), mark_interrupted=True
        )
    except AgentControlError as exc:
        raise CheckpointError(f"invalid agent control snapshot: {exc}") from exc
    agent_task_id = data.get("agent_task_id")
    if agent_task_id is not None and not isinstance(agent_task_id, str):
        raise CheckpointError("agent_task_id must be a string or null")
    agent_root_turn_id = data.get("agent_root_turn_id", "")
    if not isinstance(agent_root_turn_id, str):
        raise CheckpointError("agent_root_turn_id must be a string")
    committed_value = data.get("committed_turn_ids", [])
    if not isinstance(committed_value, list) or not all(
        isinstance(item, str) and item for item in committed_value
    ):
        raise CheckpointError("committed_turn_ids must be an array of non-empty strings")
    committed_turn_ids = list(dict.fromkeys(committed_value))[-1_000:]
    pending_episode_finalizes = _deserialize_pending_episodes(
        data.get("pending_episode_finalizes", [])
    )
    semantic_extract_receipts = _deserialize_extract_receipts(
        data.get("semantic_extract_receipts", {})
    )

    step_count = _nonnegative_int(data.get("step_count"), "step_count")
    active_turn_start_step = _nonnegative_int(
        data.get("active_turn_start_step", 0), "active_turn_start_step"
    )
    active_turn_start_message_index = _nonnegative_int(
        data.get("active_turn_start_message_index", 0),
        "active_turn_start_message_index",
    )
    max_steps = _positive_int(data.get("max_steps"), "max_steps")
    message_id_counter = _nonnegative_int(
        data.get("message_id_counter"), "message_id_counter"
    )
    context_tokens = _nonnegative_int(
        data.get("context_tokens"), "context_tokens"
    )
    request_context_tokens = _nonnegative_int(
        data.get("request_context_tokens", 0), "request_context_tokens"
    )
    attachments = _deserialize_attachments(data.get("attachments", {}))
    for record in message_records:
        attachment_ids = record.message.get("attachments", ())
        if attachment_ids and (
            not isinstance(attachment_ids, list)
            or not all(isinstance(item, str) and item in attachments for item in attachment_ids)
        ):
            raise CheckpointError("message attachment references are invalid")

    validate_session_links(
        message_records,
        turns,
        tool_executions,
        step_count,
        active_turn_start_step,
        active_turn_start_message_index,
        message_id_counter,
    )

    session = Session(
        session_id=session_id,
        lifecycle=cast(SessionLifecycle, lifecycle),
        session_label=_string(
            data.get("session_label"),
            "session_label",
            allow_empty=True,
        ),
        workspace_dir=workspace_dir,
        cwd=cwd,
        project_root=project_root,
        authorization=authorization,
        environment=environment,
        execution_mode=execution_mode,
        base_commit=base_commit,
        branch_name=branch_name,
        turns=turns,
        message_records=message_records,
        tool_executions=tool_executions,
        # OS processes cannot survive a Python process crash.  Completed tool
        # execution history is restored; live background process handles are not.
        commands={},
        plan_manager=plan_manager,
        active_deferred_tools=active_deferred_tools,
        control_plane=control_plane,
        agent_task_id=agent_task_id,
        agent_root_turn_id=agent_root_turn_id,
        committed_turn_ids=committed_turn_ids,
        pending_episode_finalizes=pending_episode_finalizes,
        semantic_extract_receipts=semantic_extract_receipts,
        active_turn_start_step=active_turn_start_step,
        active_turn_start_message_index=active_turn_start_message_index,
        last_usage=_deserialize_usage(data.get("last_usage"), "last_usage"),
        total_usage=_deserialize_usage(data.get("total_usage"), "total_usage")
        or UsageRecord(),
        task_usage_start=_deserialize_usage(data["task_usage_start"], "task_usage_start")
        or UsageRecord(),
        model_name=_optional_string(data.get("model_name"), "model_name"),
        llm_transport=_optional_string(data.get("llm_transport"), "llm_transport"),
        request_context_breakdown=_object(
            data.get("request_context_breakdown", {}), "request_context_breakdown"
        ),
        documents=_documents(data.get("documents", {})),
        attachments=attachments,
        context_tokens=context_tokens,
        request_context_tokens=request_context_tokens,
        step_count=step_count,
        max_steps=max_steps,
        message_id_counter=message_id_counter,
    )
    _restore_runs(session, data.get("runs"), data.get("active_run_id"))
    recover_interrupted_tool_calls(session)
    return session


def _restore_runs(session: Session, value: Any, active_run_id: Any) -> None:
    rows = _object(value, "runs")
    for run_id, row in rows.items():
        if not isinstance(run_id, str) or not isinstance(row, dict):
            raise CheckpointError("Invalid run record")
        run = RunRecord(run_id, session.session_id, _string(row.get("source"), "run source"), _string(row.get("goal"), "run goal", allow_empty=True))
        for key in ("parent_run_id", "root_run_id", "status", "started_at", "ended_at", "result", "error", "model_config", "plan", "usage", "step_ids", "tool_execution_ids"):
            if key in row:
                setattr(run, key, row[key])
        session.runs[run_id] = run
    if active_run_id is not None and active_run_id not in session.runs:
        raise CheckpointError("Active run does not exist")
    session.active_run_id = active_run_id


def _checkpoint_run_status(session_info: dict[str, Any]) -> str:
    """Project new run records for the existing session-list API."""
    active_id = session_info.get("active_run_id")
    runs = session_info.get("runs")
    if isinstance(active_id, str) and isinstance(runs, dict):
        active = runs.get(active_id)
        if isinstance(active, dict) and isinstance(active.get("status"), str):
            return active["status"]
    return "idle"


def _deserialize_extract_receipts(value: Any) -> dict[str, dict[str, Any]]:
    """Decode bounded optional extraction receipts."""
    if not isinstance(value, dict):
        return {}
    receipts: dict[str, dict[str, Any]] = {}
    for key, item in list(value.items())[:200]:
        if not isinstance(key, str) or not key or not isinstance(item, dict):
            continue
        status = item.get("status")
        reasons = item.get("reason_codes", [])
        written = item.get("written", 0)
        if not isinstance(status, str) or not isinstance(reasons, list):
            continue
        if isinstance(written, bool) or not isinstance(written, int):
            written = 0
        receipts[key[:200]] = {
            "status": status[:40],
            "reason_codes": [str(reason)[:80] for reason in reasons[:20]],
            "written": max(0, written),
            "root_run_id": str(item.get("root_run_id") or key)[:200],
        }
    return receipts


def _deserialize_pending_episodes(value: Any) -> list[dict[str, Any]]:
    """Old checkpoints omit the field; callers pass ``[]`` in that case."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise CheckpointError("pending_episode_finalizes must be an array")
    pending: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise CheckpointError("pending episode finalize must be an object")
        episode = item.get("episode")
        if episode is not None and not isinstance(episode, dict):
            raise CheckpointError("pending episode must be an object")
        pending.append(item)
    return pending


def _deserialize_attachments(value: Any) -> dict[str, AttachmentRecord]:
    rows = _object(value, "attachments")
    records: dict[str, AttachmentRecord] = {}
    for attachment_id, item in rows.items():
        if not isinstance(attachment_id, str) or not attachment_id:
            raise CheckpointError("attachment id must be a non-empty string")
        try:
            record = AttachmentRecord.from_dict(
                _object(item, f"attachments[{attachment_id}]")
            )
        except AttachmentError as exc:
            raise CheckpointError(str(exc)) from exc
        if record.id != attachment_id:
            raise CheckpointError("attachment key/id mismatch")
        records[attachment_id] = record
    return records


def _deserialize_messages(value: Any) -> list[MessageRecord]:
    rows = _array(value, "message_records")
    records: list[MessageRecord] = []
    for index, row in enumerate(rows):
        item = _object(row, f"message_records[{index}]")
        message = _object(item.get("message"), f"message_records[{index}].message")
        source = _string(item.get("source"), f"message_records[{index}].source")
        records.append(MessageRecord(
            _string(item.get("id"), f"message_records[{index}].id"),
            _message_param(message),
            source,
        ))
    return records


def _deserialize_turns(value: Any) -> list[TurnRecord]:
    rows = _array(value, "turns")
    turns: list[TurnRecord] = []
    for index, row in enumerate(rows):
        item = _object(row, f"turns[{index}]")
        route = _one_of(item.get("route"), _TURN_ROUTES, f"turns[{index}].route")
        execution_ids = _array(
            item.get("tool_execution_ids"), f"turns[{index}].tool_execution_ids"
        )
        if not all(isinstance(call_id, str) for call_id in execution_ids):
            raise CheckpointError(f"turns[{index}].tool_execution_ids must be an array of strings")

        verification_data = item.get("verification")
        verification = None
        if verification_data is not None:
            verify = _object(verification_data, f"turns[{index}].verification")
            approved = verify.get("approved")
            if not isinstance(approved, bool):
                raise CheckpointError("verification.approved must be a boolean")
            issues = _array(verify.get("issues"), "verification.issues")
            if not all(
                isinstance(issue, dict)
                and isinstance(issue.get("code"), str)
                and isinstance(issue.get("message"), str)
                for issue in issues
            ):
                raise CheckpointError("verification.issues has an invalid shape")
            verification = VerificationRecord(approved, issues)

        turns.append(TurnRecord(
            step=_positive_int(item.get("step"), f"turns[{index}].step"),
            message_id=_string(
                item.get("message_id"), f"turns[{index}].message_id"
            ),
            parsed=_object(item.get("parsed"), f"turns[{index}].parsed"),
            route=route,
            tool_execution_ids=list(execution_ids),
            error=_optional_string(item.get("error"), f"turns[{index}].error"),
            usage=_deserialize_usage(item.get("usage"), f"turns[{index}].usage"),
            verification=verification,
            run_id=_optional_string(item.get("run_id"), "turn.run_id") or "",
            step_id=_optional_string(item.get("step_id"), "turn.step_id") or "",
        ))
    return turns


def _deserialize_executions(value: Any) -> dict[str, ToolExecutionRecord]:
    rows = _object(value, "tool_executions")
    executions: dict[str, ToolExecutionRecord] = {}
    for call_id, row in rows.items():
        if not isinstance(call_id, str) or not call_id:
            raise CheckpointError("tool execution id must be a non-empty string")
        item = _object(row, f"tool_executions[{call_id}]")
        raw_call = _object(item.get("call"), f"tool_executions[{call_id}].call")
        stored_id = _string(raw_call.get("id"), "tool call id")
        if stored_id != call_id:
            raise CheckpointError(f"tool execution key/id mismatch: {call_id}")
        arguments = _object(raw_call.get("arguments"), "tool call arguments")
        result_data = item.get("result")
        result = None
        if result_data is not None:
            raw_result = _object(result_data, "tool result")
            ok = raw_result.get("ok")
            if not isinstance(ok, bool):
                raise CheckpointError("tool result.ok must be a boolean")
            retention = raw_result.get("retention", "")
            retention_key = raw_result.get("retention_key", "")
            if not isinstance(retention, str) or retention not in {"", "instruction"}:
                retention = ""
            if not isinstance(retention_key, str):
                retention_key = ""
            result = ToolResult(
                ok=ok,
                err=_string(raw_result.get("err", ""), "tool result.err", allow_empty=True),
                data=raw_result.get("data"),
                summary=_string(raw_result.get("summary", ""), "tool result.summary", allow_empty=True),
                content=tuple(_array(raw_result.get("content", []), "tool result.content")),
                retention=retention,
                retention_key=retention_key,
                artifacts=tuple(
                    ArtifactRef(
                        id=_string(_object(row, "tool artifact").get("id"), "tool artifact.id"),
                        media_type=_string(_object(row, "tool artifact").get("media_type"), "tool artifact.media_type"),
                        name=_string(_object(row, "tool artifact").get("name"), "tool artifact.name"),
                        size=_nonnegative_int(_object(row, "tool artifact").get("size"), "tool artifact.size"),
                        run_id=_string(_object(row, "tool artifact").get("run_id", ""), "tool artifact.run_id", allow_empty=True),
                        call_id=_string(_object(row, "tool artifact").get("call_id", ""), "tool artifact.call_id", allow_empty=True),
                        storage_path=_string(_object(row, "tool artifact").get("storage_path", ""), "tool artifact.storage_path", allow_empty=True),
                    )
                    for row in _array(raw_result.get("artifacts", []), "tool result.artifacts")
                ),
            )
        execution_status = _one_of(
            item.get("status"), _EXECUTION_STATUSES, "tool execution status"
        )
        executions[call_id] = ToolExecutionRecord(
            call=ToolCall(
                # Older checkpoints carry legacy tool names; readers see one vocabulary.
                name=canonical_tool_name(_string(raw_call.get("name"), "tool call name")),
                arguments=arguments,
                id=stored_id,
            ),
            result=result,
            step=_positive_int(item.get("step"), "tool execution step"),
            status=execution_status,
            started_at=_optional_number(item.get("started_at"), "started_at"),
            ended_at=_optional_number(item.get("ended_at"), "ended_at"),
            run_id=_optional_string(item.get("run_id"), "tool.run_id") or "",
            step_id=_optional_string(item.get("step_id"), "tool.step_id") or "",
        )
    return executions


def _deserialize_usage(value: Any, field: str) -> UsageRecord | None:
    if value is None:
        return None
    item = _object(value, field)
    return UsageRecord(
        _nonnegative_int(item.get("prompt_tokens"), f"{field}.prompt_tokens"),
        _nonnegative_int(
            item.get("completion_tokens"), f"{field}.completion_tokens"
        ),
        _nonnegative_int(item.get("total_tokens"), f"{field}.total_tokens"),
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    return repr(value)


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CheckpointError(f"{field} must be an object")
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise CheckpointError(f"{field} must be an array")
    return value


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise CheckpointError(f"{field} must be a string")
    return value


def _one_of(value: Any, allowed: frozenset[_T], field: str) -> _T:
    text = _string(value, field)
    if text not in allowed:
        raise CheckpointError(f"invalid {field}: {text}")
    return cast(_T, text)


def _message_param(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("role") not in _MESSAGE_ROLES:
        raise CheckpointError(f"invalid message role: {value.get('role')}")
    return value


def _deserialize_authorization(value: Any, session_id: str) -> SessionAuthorization:
    from ....domain.policy.permission.settings import PermissionRule

    data = _object(value, "authorization")
    if set(data) != {"rules", "interaction_mode", "permission_mode"}:
        raise CheckpointError("Invalid authorization fields")
    records = _array(data["rules"], "authorization.rules")
    try:
        return SessionAuthorization(
            session_id,
            (PermissionRule.from_mapping(_object(item, "authorization rule")) for item in records),
            interaction_mode=data["interaction_mode"],
            permission_mode=data["permission_mode"],
        )
    except (ValueError, TypeError) as exc:
        raise CheckpointError(str(exc)) from exc


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field, allow_empty=True)


def _documents(value: Any) -> dict[str, dict[str, Any]]:
    if value in (None, {}):
        return {}
    raw = _object(value, "documents")
    documents: dict[str, dict[str, Any]] = {}
    for key, item in raw.items():
        if not isinstance(item, dict):
            raise CheckpointError("documents values must be objects")
        documents[str(key)] = dict(item)
    return documents


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CheckpointError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: Any, field: str) -> int:
    number = _nonnegative_int(value, field)
    if number == 0:
        raise CheckpointError(f"{field} must be greater than 0")
    return number


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CheckpointError(f"{field} must be a number or null")
    return float(value)
