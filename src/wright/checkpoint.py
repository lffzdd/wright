"""Atomic SessionState checkpoints and crash recovery."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any

from .planning import PlanManager
from .coordination import AgentControlError, AgentControlPlane
from .session import (
    MessageRecord,
    SessionState,
    ToolExecutionRecord,
    TurnRecord,
    UsageRecord,
    VerificationRecord,
)
from .tools.base import ToolCall, ToolResult
from .util import build_tool_results_message


CHECKPOINT_VERSION = 1
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SESSION_STATUSES = {"running", "completed", "failed", "max_steps"}
_TURN_ROUTES = {"tool_calls", "final", "invalid"}
_EXECUTION_STATUSES = {"pending", "running", "succeeded", "failed", "timeout"}


class CheckpointError(ValueError):
    """Checkpoint data is missing, corrupt, unsupported, or inconsistent."""


class SessionCheckpointStore:
    """Store one latest, atomic JSON snapshot per session id."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self._save_lock = threading.RLock()

    def path_for(self, session_id: str) -> Path:
        if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise CheckpointError("非法 session_id")
        return self.directory / f"{session_id}.json"

    def save(self, session: SessionState) -> Path:
        with self._save_lock:
            return self._save_unlocked(session)

    def _save_unlocked(self, session: SessionState) -> Path:
        path = self.path_for(session.session_id)
        if not session.workspace_dir.is_dir():
            raise CheckpointError(
                f"不能保存不可恢复的会话，workspace_dir 不存在: {session.workspace_dir}"
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = _serialize_session(session)

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{session.session_id}.",
            suffix=".tmp",
            dir=self.directory,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return path

    def load(self, session_id: str) -> SessionState:
        path = self.path_for(session_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CheckpointError(f"checkpoint 不存在: {session_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"checkpoint 无法读取: {exc}") from exc
        return _deserialize_session(data)

    def latest_session_id(self) -> str | None:
        if not self.directory.is_dir():
            return None
        candidates = [
            path
            for path in self.directory.glob("*.json")
            if _SESSION_ID_PATTERN.fullmatch(path.stem)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime_ns).stem

    def load_latest(self) -> SessionState:
        session_id = self.latest_session_id()
        if session_id is None:
            raise CheckpointError("没有可继续的 checkpoint")
        return self.load(session_id)

    def list_recent_sessions(self, limit: int = 5) -> list[dict[str, Any]]:
        if not self.directory.is_dir():
            return []
        candidates = [
            path
            for path in self.directory.glob("*.json")
            if _SESSION_ID_PATTERN.fullmatch(path.stem)
        ]
        if not candidates:
            return []
        sorted_paths = sorted(
            candidates, key=lambda path: path.stat().st_mtime_ns, reverse=True
        )[:limit]

        results: list[dict[str, Any]] = []
        for path in sorted_paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session_info = data.get("session", {})
                saved_at = data.get("saved_at", "")
                saved_at_str = saved_at
                if saved_at:
                    try:
                        dt = datetime.fromisoformat(saved_at).astimezone()
                        saved_at_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pass
                results.append({
                    "session_id": path.stem,
                    "saved_at": saved_at_str,
                    "status": session_info.get("status", "unknown"),
                    "user_goal": session_info.get("user_goal", ""),
                })
            except Exception:
                continue
        return results



def _serialize_session(session: SessionState) -> dict[str, Any]:
    return {
        "version": CHECKPOINT_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "session": {
            "session_id": session.session_id,
            "status": session.status,
            "user_goal": session.user_goal,
            "workspace_dir": str(session.workspace_dir),
            "cwd": str(session.get_cwd()),
            "message_records": [
                {"id": record.id, "message": _json_safe(record.message)}
                for record in session.message_records
            ],
            "turns": [_serialize_turn(turn) for turn in session.turns],
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
                }
                for call_id, execution in session.tool_executions.items()
            },
            "plan": session.plan_manager.snapshot(),
            "skill_catalog_sent": session.skill_catalog_sent,
            "agent_control": session.control_plane.snapshot(),
            "agent_task_id": session.agent_task_id,
            "agent_root_turn_id": session.agent_root_turn_id,
            "last_usage": _serialize_usage(session.last_usage),
            "total_usage": _serialize_usage(session.total_usage),
            "context_tokens": session.context_tokens,
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
    }


def _serialize_usage(usage: UsageRecord | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def _deserialize_session(payload: Any) -> SessionState:
    root = _object(payload, "checkpoint")
    version = root.get("version")
    if version != CHECKPOINT_VERSION:
        raise CheckpointError(
            f"不支持的 checkpoint version: {version}; 当前只支持 {CHECKPOINT_VERSION}"
        )
    data = _object(root.get("session"), "session")

    session_id = _string(data.get("session_id"), "session_id")
    if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise CheckpointError("非法 session_id")
    status = _string(data.get("status"), "status")
    if status not in _SESSION_STATUSES:
        raise CheckpointError(f"非法 session status: {status}")

    workspace_dir = Path(
        _string(data.get("workspace_dir"), "workspace_dir")
    ).resolve()
    if not workspace_dir.is_dir():
        raise CheckpointError(f"workspace_dir 不存在: {workspace_dir}")
    saved_cwd = Path(_string(data.get("cwd"), "cwd")).resolve()
    cwd = saved_cwd if saved_cwd.is_dir() else workspace_dir

    message_records = _deserialize_messages(data.get("message_records"))
    turns = _deserialize_turns(data.get("turns"))
    tool_executions = _deserialize_executions(data.get("tool_executions"))
    plan_manager = PlanManager.from_snapshot(_object(data.get("plan"), "plan"))
    skill_catalog_sent = _deserialize_skill_catalog_sent(
        data.get("skill_catalog_sent")
    )
    try:
        control_plane = AgentControlPlane.from_snapshot(
            data.get("agent_control", {}), mark_interrupted=True
        )
    except AgentControlError as exc:
        raise CheckpointError(f"agent control snapshot 非法: {exc}") from exc
    agent_task_id = data.get("agent_task_id")
    if agent_task_id is not None and not isinstance(agent_task_id, str):
        raise CheckpointError("agent_task_id 必须是字符串或 null")
    agent_root_turn_id = data.get("agent_root_turn_id", "")
    if not isinstance(agent_root_turn_id, str):
        raise CheckpointError("agent_root_turn_id 必须是字符串")

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

    _validate_links(
        message_records,
        turns,
        tool_executions,
        step_count,
        active_turn_start_step,
        active_turn_start_message_index,
        message_id_counter,
    )

    session = SessionState(
        session_id=session_id,
        status=status,  # type: ignore[arg-type]
        user_goal=_string(data.get("user_goal"), "user_goal", allow_empty=True),
        workspace_dir=workspace_dir,
        cwd=cwd,
        turns=turns,
        message_records=message_records,
        tool_executions=tool_executions,
        # OS processes cannot survive a Python process crash.  Completed tool
        # execution history is restored; live background process handles are not.
        background_tasks={},
        plan_manager=plan_manager,
        skill_catalog_sent=skill_catalog_sent,
        control_plane=control_plane,
        agent_task_id=agent_task_id,
        agent_root_turn_id=agent_root_turn_id,
        active_turn_start_step=active_turn_start_step,
        active_turn_start_message_index=active_turn_start_message_index,
        last_usage=_deserialize_usage(data.get("last_usage"), "last_usage"),
        total_usage=_deserialize_usage(data.get("total_usage"), "total_usage")
        or UsageRecord(),
        context_tokens=context_tokens,
        step_count=step_count,
        max_steps=max_steps,
        message_id_counter=message_id_counter,
    )
    _recover_interrupted_tool_calls(session)
    return session


def _recover_interrupted_tool_calls(session: SessionState) -> None:
    """Close pending calls from a crashed process without replaying side effects."""
    for turn in session.turns:
        interrupted = [
            session.tool_executions[call_id]
            for call_id in turn.tool_execution_ids
            if session.tool_executions[call_id].status in {"pending", "running"}
        ]
        if not interrupted:
            continue

        call_results = []
        for execution in interrupted:
            result = ToolResult.fail(
                "Tool execution was interrupted by process restart; outcome is unknown. "
                "Inspect the current state before deciding whether to retry.",
                data={
                    "error": {
                        "type": "tool_execution_interrupted",
                        "retriable": False,
                    }
                },
            )
            execution.result = result
            execution.status = "failed"
            call_results.append((execution.call, result))
        session.append_message(build_tool_results_message(call_results))


def _deserialize_skill_catalog_sent(value: Any) -> bool:
    # 旧 checkpoint 没有该字段，或仍带着已废弃的 active_skill_ids：当成尚未发送目录。
    if value is None:
        return False
    if not isinstance(value, bool):
        raise CheckpointError("skill_catalog_sent 必须是 boolean")
    return value


def _deserialize_messages(value: Any) -> list[MessageRecord]:
    rows = _array(value, "message_records")
    records: list[MessageRecord] = []
    for index, row in enumerate(rows):
        item = _object(row, f"message_records[{index}]")
        message = _object(item.get("message"), f"message_records[{index}].message")
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool", "developer"}:
            raise CheckpointError(f"非法 message role: {role}")
        records.append(MessageRecord(
            _string(item.get("id"), f"message_records[{index}].id"),
            message,  # type: ignore[arg-type]
        ))
    return records


def _deserialize_turns(value: Any) -> list[TurnRecord]:
    rows = _array(value, "turns")
    turns: list[TurnRecord] = []
    for index, row in enumerate(rows):
        item = _object(row, f"turns[{index}]")
        route = _string(item.get("route"), f"turns[{index}].route")
        if route not in _TURN_ROUTES:
            raise CheckpointError(f"非法 turn route: {route}")
        execution_ids = _array(
            item.get("tool_execution_ids"), f"turns[{index}].tool_execution_ids"
        )
        if not all(isinstance(call_id, str) for call_id in execution_ids):
            raise CheckpointError(f"turns[{index}].tool_execution_ids 必须是字符串数组")

        verification_data = item.get("verification")
        verification = None
        if verification_data is not None:
            verify = _object(verification_data, f"turns[{index}].verification")
            approved = verify.get("approved")
            if not isinstance(approved, bool):
                raise CheckpointError("verification.approved 必须是 boolean")
            issues = _array(verify.get("issues"), "verification.issues")
            if not all(
                isinstance(issue, dict)
                and isinstance(issue.get("code"), str)
                and isinstance(issue.get("message"), str)
                for issue in issues
            ):
                raise CheckpointError("verification.issues 形状非法")
            verification = VerificationRecord(approved, issues)

        turns.append(TurnRecord(
            step=_positive_int(item.get("step"), f"turns[{index}].step"),
            message_id=_string(
                item.get("message_id"), f"turns[{index}].message_id"
            ),
            parsed=_object(item.get("parsed"), f"turns[{index}].parsed"),
            route=route,  # type: ignore[arg-type]
            tool_execution_ids=list(execution_ids),
            error=_optional_string(item.get("error"), f"turns[{index}].error"),
            usage=_deserialize_usage(item.get("usage"), f"turns[{index}].usage"),
            verification=verification,
        ))
    return turns


def _deserialize_executions(value: Any) -> dict[str, ToolExecutionRecord]:
    rows = _object(value, "tool_executions")
    executions: dict[str, ToolExecutionRecord] = {}
    for call_id, row in rows.items():
        if not isinstance(call_id, str) or not call_id:
            raise CheckpointError("tool execution id 必须是非空字符串")
        item = _object(row, f"tool_executions[{call_id}]")
        raw_call = _object(item.get("call"), f"tool_executions[{call_id}].call")
        stored_id = _string(raw_call.get("id"), "tool call id")
        if stored_id != call_id:
            raise CheckpointError(f"tool execution key/id 不一致: {call_id}")
        arguments = _object(raw_call.get("arguments"), "tool call arguments")
        result_data = item.get("result")
        result = None
        if result_data is not None:
            raw_result = _object(result_data, "tool result")
            ok = raw_result.get("ok")
            if not isinstance(ok, bool):
                raise CheckpointError("tool result.ok 必须是 boolean")
            result = ToolResult(
                ok=ok,
                err=_string(raw_result.get("err", ""), "tool result.err", allow_empty=True),
                data=raw_result.get("data"),
            )
        execution_status = _string(item.get("status"), "tool execution status")
        if execution_status not in _EXECUTION_STATUSES:
            raise CheckpointError(f"非法 tool execution status: {execution_status}")
        executions[call_id] = ToolExecutionRecord(
            call=ToolCall(
                name=_string(raw_call.get("name"), "tool call name"),
                arguments=arguments,
                id=stored_id,
            ),
            result=result,
            step=_positive_int(item.get("step"), "tool execution step"),
            status=execution_status,  # type: ignore[arg-type]
            started_at=_optional_number(item.get("started_at"), "started_at"),
            ended_at=_optional_number(item.get("ended_at"), "ended_at"),
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


def _validate_links(
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
        raise CheckpointError(f"{field} 必须是对象")
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise CheckpointError(f"{field} 必须是数组")
    return value


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise CheckpointError(f"{field} 必须是字符串")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field, allow_empty=True)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CheckpointError(f"{field} 必须是非负整数")
    return value


def _positive_int(value: Any, field: str) -> int:
    number = _nonnegative_int(value, field)
    if number == 0:
        raise CheckpointError(f"{field} 必须大于 0")
    return number


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CheckpointError(f"{field} 必须是 number 或 null")
    return float(value)
