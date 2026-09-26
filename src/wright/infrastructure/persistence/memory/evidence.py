"""Load episode evidence from the session that recorded it.

The reader never executes tools, resumes a run, or searches for a similar
message when the registered id is missing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ....domain.gateway.memory.evidence import EvidenceRead, IEpisodeEvidenceSource
from ....domain.model.memory.episode import EvidenceRef
from ....domain.policy.memory import bounded_safe_text, is_safe_memory

_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_READ_CHARS = 1_500
_MAX_READ_TOTAL = 6_000
_TRUNCATED = "…(已截断)"


class SessionEvidenceSource(IEpisodeEvidenceSource):
    """Read registered locators through a session repository."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def load(self, refs: Sequence[EvidenceRef]) -> list[EvidenceRead]:
        loaded: dict[str, tuple[str, Any]] = {}
        for session_id in dict.fromkeys(ref.session_id for ref in refs):
            loaded[session_id] = self._load_session(session_id)
        reads: list[EvidenceRead] = []
        remaining = _MAX_READ_TOTAL
        for ref in refs:
            state, session = loaded.get(ref.session_id, ("unavailable", None))
            if state != "ok":
                reads.append(EvidenceRead(
                    evidence_id=ref.id,
                    status="rejected" if state == "rejected" else "unavailable",
                    kind=ref.kind,
                    reason="invalid_session" if state == "rejected" else "session_unavailable",
                ))
                continue
            read = _read_ref(session, ref, remaining)
            if read.status == "available":
                remaining = max(0, remaining - len(read.text))
            reads.append(read)
        return reads

    def _load_session(self, session_id: str) -> tuple[str, Any]:
        if _SESSION_ID.fullmatch(session_id or "") is None:
            return "rejected", None
        try:
            session = self._repository.load(session_id)
        except Exception:
            return "unavailable", None
        if session is None:
            return "unavailable", None
        if str(getattr(session, "session_id", "")) != session_id:
            return "rejected", None
        return "ok", session


def _read_ref(session: Any, ref: EvidenceRef, remaining: int) -> EvidenceRead:
    if ref.kind == "tool_observation":
        return _read_tool(session, ref, remaining)
    if ref.kind == "assistant_statement":
        return _read_assistant(session, ref, remaining)
    if ref.kind == "user_statement":
        return _read_user(session, ref, remaining)
    if ref.kind == "verification_record":
        return _read_verification(session, ref, remaining)
    return EvidenceRead(
        evidence_id=ref.id, status="rejected", kind=ref.kind, reason="unknown_kind"
    )


def _read_tool(session: Any, ref: EvidenceRef, remaining: int) -> EvidenceRead:
    if not ref.tool_call_id:
        return _rejected(ref, "missing_tool_call")
    execution = getattr(session, "tool_executions", {}).get(ref.tool_call_id)
    if execution is None:
        return _unavailable(ref, "source_missing")
    root = _execution_root(session, execution)
    if ref.root_run_id and root != ref.root_run_id:
        return _rejected(ref, "run_mismatch")
    if ref.run_id and execution.run_id and ref.run_id not in {execution.run_id, root}:
        return _rejected(ref, "run_mismatch")
    if ref.step_id and execution.step_id and ref.step_id != execution.step_id:
        return _rejected(ref, "step_mismatch")
    if ref.message_id:
        record = _message(session, ref.message_id)
        if record is None:
            return _unavailable(ref, "source_missing")
        message = record.message if isinstance(record.message, dict) else {}
        if message.get("tool_call_id") != ref.tool_call_id:
            return _rejected(ref, "message_mismatch")
    return _available(ref, _tool_text(execution), remaining)


def _read_assistant(session: Any, ref: EvidenceRef, remaining: int) -> EvidenceRead:
    turn = _turn_for_message(session, ref.message_id)
    if turn is None:
        return _unavailable(ref, "source_missing")
    root = _turn_root(session, turn)
    if ref.root_run_id and root != ref.root_run_id:
        return _rejected(ref, "run_mismatch")
    if ref.run_id and turn.run_id and ref.run_id not in {turn.run_id, root}:
        return _rejected(ref, "run_mismatch")
    if ref.step_id and turn.step_id and ref.step_id != turn.step_id:
        return _rejected(ref, "step_mismatch")
    record = _message(session, ref.message_id)
    if record is None:
        return _unavailable(ref, "source_missing")
    return _available(ref, _record_text(record), remaining)


def _read_user(session: Any, ref: EvidenceRef, remaining: int) -> EvidenceRead:
    record = _message(session, ref.message_id)
    if record is None:
        return _unavailable(ref, "source_missing")
    if getattr(record, "source", "") != "user_input":
        return _rejected(ref, "message_mismatch")
    message = record.message if isinstance(record.message, dict) else {}
    if message.get("role") != "user":
        return _rejected(ref, "message_mismatch")
    index = _message_index(session, ref.message_id)
    if index is None or not _user_in_root(session, index, ref.root_run_id):
        return _rejected(ref, "run_mismatch")
    return _available(ref, _record_text(record), remaining)


def _read_verification(session: Any, ref: EvidenceRef, remaining: int) -> EvidenceRead:
    turn = None
    if ref.step_id:
        turn = next(
            (
                item
                for item in getattr(session, "turns", [])
                if item.step_id == ref.step_id
            ),
            None,
        )
    if turn is None and ref.message_id:
        turn = _turn_for_message(session, ref.message_id)
    if turn is None or turn.verification is None:
        return _unavailable(ref, "source_missing")
    root = _turn_root(session, turn)
    if ref.root_run_id and root != ref.root_run_id:
        return _rejected(ref, "run_mismatch")
    issues = turn.verification.issues or []
    text = f"approved={turn.verification.approved}"
    rendered = bounded_safe_text(
        "; ".join(
            str(issue.get("message") or issue.get("code") or "")
            if isinstance(issue, dict)
            else str(issue)
            for issue in issues
        ),
        400,
    )
    if rendered:
        text += f" issues={rendered}"
    return _available(ref, text, remaining)


def _user_in_root(session: Any, index: int, root_run_id: str) -> bool:
    if not root_run_id or not _has_root(session, root_run_id):
        return False
    start, end = _span(session, index)
    roots = _assistant_roots(session, start, end)
    if not roots:
        # A span with no assistant turn cannot be tied to one root when the
        # session has several. Refuse instead of picking a nearby message.
        return len(_known_roots(session)) == 1
    return roots == {root_run_id}


def _span(session: Any, index: int) -> tuple[int, int]:
    records = list(getattr(session, "message_records", []))
    start = index
    for cursor in range(index, -1, -1):
        if getattr(records[cursor], "source", "") == "user_input":
            start = cursor
            break
    end = len(records)
    for cursor in range(index + 1, len(records)):
        if getattr(records[cursor], "source", "") == "user_input":
            end = cursor
            break
    return start, end


def _assistant_roots(session: Any, start: int, end: int) -> set[str]:
    roots: set[str] = set()
    for turn in getattr(session, "turns", []):
        index = _message_index(session, turn.message_id)
        if index is None or not start <= index < end:
            continue
        root = _turn_root(session, turn)
        if root:
            roots.add(root)
    return roots


def _known_roots(session: Any) -> set[str]:
    roots: set[str] = set()
    for run in getattr(session, "runs", {}).values():
        root = str(getattr(run, "root_run_id", "") or getattr(run, "run_id", ""))
        if root:
            roots.add(root)
    return roots


def _has_root(session: Any, root_run_id: str) -> bool:
    return root_run_id in _known_roots(session)


def _execution_root(session: Any, execution: Any) -> str:
    run = getattr(session, "runs", {}).get(getattr(execution, "run_id", ""))
    if run is None:
        return str(getattr(execution, "run_id", "") or "")
    return str(getattr(run, "root_run_id", "") or getattr(run, "run_id", ""))


def _turn_root(session: Any, turn: Any) -> str:
    run = getattr(session, "runs", {}).get(getattr(turn, "run_id", ""))
    if run is None:
        return str(getattr(turn, "run_id", "") or "")
    return str(getattr(run, "root_run_id", "") or getattr(run, "run_id", ""))


def _turn_for_message(session: Any, message_id: str) -> Any:
    if not message_id:
        return None
    return next(
        (turn for turn in getattr(session, "turns", []) if turn.message_id == message_id),
        None,
    )


def _message(session: Any, message_id: str) -> Any:
    if not message_id:
        return None
    for record in getattr(session, "message_records", []):
        if getattr(record, "id", "") == message_id:
            return record
    return None


def _message_index(session: Any, message_id: str) -> int | None:
    for index, record in enumerate(getattr(session, "message_records", [])):
        if getattr(record, "id", "") == message_id:
            return index
    return None


def _record_text(record: Any) -> str:
    """Return the message text. The caller applies the size limit and marker.

    A credential pattern in the portion that would be returned withholds the
    text. The reader does not search for a shorter safe excerpt.
    """
    message = record.message if isinstance(getattr(record, "message", None), dict) else {}
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    flat = " ".join(content.split())
    if not flat:
        return ""
    probe = flat[: _MAX_READ_CHARS + 1]
    safe, _reason = is_safe_memory(probe)
    if not safe:
        return "[redacted]"
    return flat


def _tool_text(execution: Any) -> str:
    """Return status, command, and error. Do not copy raw stdout or arguments."""
    call = execution.call
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    command = ""
    raw_command = arguments.get("command")
    if isinstance(raw_command, str):
        command = bounded_safe_text(raw_command, 300)
    subject = ""
    for key in ("path", "file_path", "directory"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            subject = bounded_safe_text(value, 300)
            break
    result = execution.result
    error = ""
    exit_code = None
    if result is not None:
        error = bounded_safe_text(getattr(result, "err", "") or "", 400)
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            for key in ("returncode", "exit_code"):
                value = data.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    exit_code = value
                    break
    bits = [f"tool={call.name}", f"status={execution.status}"]
    if command:
        bits.append(f"command={command}")
    if subject:
        bits.append(f"subject={subject}")
    if exit_code is not None:
        bits.append(f"exit={exit_code}")
    if error:
        bits.append(f"error={error}")
    if result is None:
        bits.append("result=unknown")
    return " ".join(bits)


def _available(ref: EvidenceRef, text: str, remaining: int) -> EvidenceRead:
    if remaining <= 0:
        return EvidenceRead(
            evidence_id=ref.id,
            status="available",
            kind=ref.kind,
            text=_TRUNCATED,
            truncated=True,
            reason="budget",
        )
    limit = min(_MAX_READ_CHARS, remaining)
    truncated = len(text) > limit
    body = text[:limit]
    if truncated:
        marker = _TRUNCATED
        if len(body) + len(marker) > limit:
            body = body[: max(0, limit - len(marker))]
        body += marker
    return EvidenceRead(
        evidence_id=ref.id,
        status="available",
        kind=ref.kind,
        text=body,
        truncated=truncated,
        reason="",
    )


def _unavailable(ref: EvidenceRef, reason: str) -> EvidenceRead:
    return EvidenceRead(
        evidence_id=ref.id, status="unavailable", kind=ref.kind, reason=reason
    )


def _rejected(ref: EvidenceRef, reason: str) -> EvidenceRead:
    return EvidenceRead(
        evidence_id=ref.id, status="rejected", kind=ref.kind, reason=reason
    )


__all__ = ["SessionEvidenceSource"]
