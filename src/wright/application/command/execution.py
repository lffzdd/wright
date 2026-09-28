"""Lifecycle owner for one session's shell executions.

Starts processes, stores bounded output, promotes foreground work to the
background, waits, terminates the owned process group, notifies completion,
and deletes logs on close. Session records stay free of handles and callbacks.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...core.logger import get_logger
from ...domain.model.command import CommandRecord
from ...domain.policy.permission.scope import AccessScope
from ...infrastructure.runtime.command_log import CommandOutputLog, OutputSlice
from ...infrastructure.runtime.protocols import ProcessHandle
from ...infrastructure.runtime.types import ExecutionPath
from ..execution.identity import (
    ExecutionIdentity,
    ExecutionKindMismatch,
    ExecutionNotFound,
    ExecutionWaitCancelled,
)
from ..execution.paging import bounded_page_limit, decode_cursor, encode_cursor

logger = get_logger(__name__)

DEFAULT_OUTPUT_LIMIT = 8_000
MAX_OUTPUT_LIMIT = 32_000
_STORAGE_NOTE = (
    "\n[stored output truncated; earlier bytes remain readable from offset 0]"
)


class CommandCancelled(RuntimeError):
    """The invoking turn was cancelled. The owned process group was signaled."""


@dataclass(frozen=True)
class CommandOutcome:
    ok: bool
    error: str = ""
    data: dict | None = None
    cancelled: bool = False


@dataclass
class _LiveCommand:
    process: ProcessHandle
    log: CommandOutputLog
    done: threading.Event
    reader: threading.Thread | None = None
    background: bool = False
    notified: bool = False
    cwd_reset: bool = False
    cwd_message: str = ""


class CommandExecution:
    def __init__(
        self,
        session,
        identity: ExecutionIdentity,
        *,
        allow_background: bool = True,
        notify: Callable[[str], None] | None = None,
        max_output_bytes: int = 1_048_576,
    ) -> None:
        self._session = session
        self._identity = identity
        self._allow_background = allow_background
        self._notify = notify
        self._max_output_bytes = max_output_bytes
        self._live: dict[str, _LiveCommand] = {}
        self._lock = threading.RLock()
        self._log_dir = Path(tempfile.mkdtemp(prefix=f"wright-cmd-{session.session_id}-"))
        os.chmod(self._log_dir, 0o700)
        self._closed = False

    def execute(
        self,
        *,
        command: str,
        timeout: int,
        run_in_background: bool,
        execution,
        allow_background: bool,
        emit_output: Callable[[str], None] | None,
        is_cancelled: Callable[[], bool],
        root_turn_id: str,
        run_id: str,
        access_scope: AccessScope | None,
        set_cwd: Callable[[ExecutionPath], None] | None,
    ) -> CommandOutcome:
        if self._closed:
            return CommandOutcome(False, "command runtime is closed", {"cwd": "."})
        background_allowed = self._allow_background and allow_background
        if run_in_background and not background_allowed:
            return CommandOutcome(
                False,
                "This Agent cannot create background tasks",
                self._cwd_payload(execution),
            )
        if execution is None:
            return CommandOutcome(False, "command tool requires an invocation authorization")
        active = self._session.active_run()
        root_turn_id = self._session.agent_root_turn_id or root_turn_id
        if active is not None:
            run_id = active.run_id
        try:
            process = execution.start_shell(command)
        except FileNotFoundError:
            program = command.split()[0] if command.split() else command
            return CommandOutcome(False, f"command not found: {program}")
        except Exception as exc:
            return CommandOutcome(False, f"{type(exc).__name__}: {exc}")

        command_id = f"cmd_{uuid.uuid4().hex[:8]}"
        record = CommandRecord(
            command_id=command_id,
            command=command,
            root_turn_id=root_turn_id,
            run_id=run_id,
        )
        log = CommandOutputLog(
            self._log_dir, command_id, max_bytes=self._max_output_bytes
        )
        live = _LiveCommand(process, log, threading.Event())
        with self._lock:
            self._live[command_id] = live
        live.reader = threading.Thread(
            target=self._read_until_group_ends,
            args=(live, record, emit_output, execution, access_scope, set_cwd),
            daemon=True,
        )
        live.reader.start()

        if run_in_background:
            self._publish(command_id, record, live)
            return CommandOutcome(True, data=self._background_payload(
                command_id, execution, timed_out=False
            ))

        deadline = time.monotonic() + timeout
        while not live.done.is_set():
            if is_cancelled():
                record.cancel_requested = True
                record.cancel_reason = "execute_command cancelled"
                process.terminate()
                live.done.wait(timeout=2)
                self._drop_unpublished(command_id, live)
                return CommandOutcome(False, cancelled=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            live.done.wait(timeout=min(0.05, remaining))

        if not live.done.is_set():
            if not background_allowed:
                record.cancel_requested = True
                record.cancel_reason = f"Command exceeded {timeout}s"
                process.terminate()
                live.done.wait(timeout=2)
                page = self._page(live, 0, DEFAULT_OUTPUT_LIMIT)
                self._drop_unpublished(command_id, live)
                return CommandOutcome(
                    False,
                    f"Command exceeded {timeout}s; this Agent cannot convert it to a background task",
                    {**self._output_fields(page), **self._cwd_payload(execution), "timed_out": True},
                )
            self._publish(command_id, record, live)
            data = self._background_payload(command_id, execution, timed_out=True)
            data["message"] = (
                f"Command exceeded {timeout}s and is running in the background "
                f"as command_id {command_id}. Use get_command, wait_command, "
                "list_commands, or terminate_command."
            )
            data["output_so_far"] = data.pop("output")
            return CommandOutcome(True, data=data)

        page = self._page(live, 0, DEFAULT_OUTPUT_LIMIT)
        if page.truncated:
            self._publish(command_id, record, live)
        else:
            self._drop_unpublished(command_id, live)
        data = {
            "returncode": process.returncode,
            **self._output_fields(page),
            **self._cwd_payload(execution),
        }
        if live.cwd_reset:
            data["cwd_reset"] = True
            data["message"] = live.cwd_message
        if page.truncated:
            data["command_id"] = command_id
        if record.disposition == "unknown":
            data["outcome"] = "unconfirmed"
        if process.returncode == 0:
            return CommandOutcome(True, data=data)
        return CommandOutcome(
            False,
            f"Command exited with code {process.returncode}",
            data,
        )

    def get(
        self, command_id: str, *, offset: int = 0, limit: int = DEFAULT_OUTPUT_LIMIT
    ) -> dict:
        self._identity.require(command_id, "command")
        record = self._require_record(command_id)
        return self._view(record, offset=_bounded_offset(offset), limit=_bounded_limit(limit))

    def wait(
        self,
        command_id: str,
        *,
        timeout: float,
        offset: int = 0,
        limit: int = DEFAULT_OUTPUT_LIMIT,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> dict:
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        self._identity.require(command_id, "command")
        record = self._require_record(command_id)
        live = self._live.get(command_id)
        offset = _bounded_offset(offset)
        limit = _bounded_limit(limit)
        if live is None:
            view = self._view(record, offset=offset, limit=limit)
            view["wait_completed"] = view["terminal"]
            view["wait_timed_out"] = not view["terminal"]
            return view
        deadline = time.monotonic() + timeout
        timed_out = False
        while not live.done.is_set():
            if cancellation_check is not None and cancellation_check():
                raise ExecutionWaitCancelled(command_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            live.done.wait(timeout=min(0.05, remaining))
        view = self._view(record, offset=offset, limit=limit)
        view["wait_completed"] = bool(view["terminal"])
        view["wait_timed_out"] = timed_out and not view["terminal"]
        return view

    def terminate(self, command_id: str, reason: str = "terminated by tool") -> dict:
        self._identity.require(command_id, "command")
        record = self._require_record(command_id)
        live = self._live.get(command_id)
        already = record.disposition != "running"
        if live is None or live.done.is_set():
            view = self._view(record, offset=0, limit=DEFAULT_OUTPUT_LIMIT)
            view["already_terminal"] = already or view["terminal"]
            view["terminated"] = False
            return view
        record.cancel_requested = True
        record.cancel_reason = reason[:1_000]
        stopped = live.process.terminate()
        live.done.wait(timeout=2)
        view = self._view(record, offset=0, limit=DEFAULT_OUTPUT_LIMIT)
        view["already_terminal"] = False
        view["terminated"] = bool(stopped and view["terminal"])
        if not view["terminated"]:
            view["message"] = (
                "Termination was requested. The command has not exited yet."
            )
        return view

    def list(
        self,
        *,
        scope: str = "current_user_turn",
        status: str | None = None,
        root_turn_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        if scope not in {"current_user_turn", "session"}:
            raise ValueError("scope must be current_user_turn or session")
        if scope == "session":
            root_turn_id = None
        elif root_turn_id is None:
            root_turn_id = self._session.agent_root_turn_id
        limit = bounded_page_limit(limit)
        records = self._session.list_commands()
        if status is not None:
            records = [
                record for record in records
                if self._status(record) == status
            ]
        if root_turn_id is not None:
            records = [
                record for record in records if record.root_turn_id == root_turn_id
            ]
        records.sort(key=lambda record: (record.created_at, record.command_id), reverse=True)
        if cursor is not None:
            created_at, row_id = decode_cursor(cursor)
            records = [
                record for record in records
                if (record.created_at, record.command_id) < (created_at, row_id)
            ]
        page = records[:limit]
        next_cursor = None
        if len(records) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(last.created_at, last.command_id)
        return {
            "count": len(page),
            "next_cursor": next_cursor,
            "scope": scope,
            "commands": [
                self._view(record, offset=0, limit=500, summary=True) for record in page
            ],
        }

    def notice(self, command_id: str) -> dict | None:
        record = self._session.get_command(command_id)
        if record is None:
            return None
        view = self._view(record, offset=0, limit=2_000)
        body = {
            "command_id": command_id[:100],
            "object": "command",
            "run_id": record.run_id,
            "status": view["status"],
            "root_turn_id": record.root_turn_id[:180],
            "description": record.command[:500],
            "result": "",
            "output": view["output"][:2_000],
            "error": view["error"][:1_000],
            "returncode": view["returncode"],
            "cancel_requested": view["cancel_requested"],
            "cancel_reason": view["cancel_reason"][:500],
        }
        if view["status"] == "unknown":
            body["outcome"] = "unconfirmed"
            body["note"] = (
                "Status cannot be confirmed. This is not a successful completion."
            )
        return {
            "type": "task_notification",
            "follow_up": (
                "Use get_command, wait_command, list_commands, or terminate_command "
                "with command_id."
            ),
            "task": body,
        }

    def close(self) -> tuple[str, ...]:
        with self._lock:
            if self._closed:
                return ()
            self._closed = True
            items = list(self._live.items())
        terminated: list[str] = []
        for command_id, live in items:
            record = self._session.get_command(command_id)
            if not live.done.is_set():
                if record is not None and record.disposition == "running":
                    record.cancel_requested = True
                    record.cancel_reason = "runtime shutdown"
                live.process.terminate()
                live.done.wait(timeout=2)
                terminated.append(command_id)
            live.log.discard()
        with self._lock:
            self._live.clear()
        shutil.rmtree(self._log_dir, ignore_errors=True)
        return tuple(terminated)

    def _publish(self, command_id: str, record: CommandRecord, live: _LiveCommand) -> None:
        self._session.register_command(record)
        notify = False
        with self._lock:
            live.background = True
            if live.done.is_set() and not live.notified:
                live.notified = True
                notify = True
        if notify:
            self._emit_done(command_id)

    def _drop_unpublished(self, command_id: str, live: _LiveCommand) -> None:
        if self._session.get_command(command_id) is not None:
            return
        with self._lock:
            self._live.pop(command_id, None)
        live.log.discard()

    def _read_until_group_ends(
        self,
        live: _LiveCommand,
        record: CommandRecord,
        emit_output: Callable[[str], None] | None,
        execution,
        access_scope: AccessScope | None,
        set_cwd: Callable[[ExecutionPath], None] | None,
    ) -> None:
        process = live.process
        try:
            while True:
                chunk = process.read_output(65_536)
                if chunk:
                    live.log.append(chunk)
                    if emit_output is not None:
                        emit_output(chunk.decode("utf-8", errors="replace"))
                    continue
                # The leader can exit before its children. An empty read is not
                # the end of the execution while any owned group member remains.
                if process.poll() is None:
                    time.sleep(0.02)
                    continue
                if not process.group_alive():
                    break
                time.sleep(0.05)
            try:
                process.wait(timeout=2)
            except Exception:
                logger.debug("command wait failed", exc_info=True)
            self._settle(record, process)
            if not live.background:
                self._apply_cwd(process, execution, access_scope, set_cwd, record)
        finally:
            live.done.set()
            notify = False
            with self._lock:
                if live.background and not live.notified:
                    live.notified = True
                    notify = True
            if notify:
                self._emit_done(record.command_id)

    def _settle(self, record: CommandRecord, process: ProcessHandle) -> None:
        record.ended_at = time.time()
        record.returncode = process.returncode
        if record.disposition == "unknown":
            return
        if process.group_alive() or process.returncode is None:
            record.disposition = "unknown"
        elif record.cancel_requested:
            record.disposition = "cancelled"
        elif process.returncode == 0:
            record.disposition = "completed"
        else:
            record.disposition = "failed"

    def _apply_cwd(self, process, execution, access_scope, set_cwd, record) -> None:
        new_cwd = process.cwd_result()
        if new_cwd is None or set_cwd is None:
            return
        live = next(
            (item for item in self._live.values() if item.process is process),
            None,
        )
        if access_scope is not None and not access_scope.contains(new_cwd.value):
            origin = ExecutionPath(new_cwd.environment_id, str(access_scope.origin))
            set_cwd(origin)
            if live is not None:
                live.cwd_reset = True
                live.cwd_message = f"Shell cwd was reset to {access_scope.origin}"
            return
        set_cwd(new_cwd)

    def _view(
        self,
        record: CommandRecord,
        *,
        offset: int,
        limit: int,
        summary: bool = False,
    ) -> dict:
        live = self._live.get(record.command_id)
        status = self._status(record)
        if live is None and record.disposition == "running":
            page = OutputSlice("", offset, None, 0, False)
        else:
            page = self._page(live, 0 if summary else offset, 500 if summary else limit)
        text_limit = 500 if summary else limit
        fields = self._output_fields(page)
        if summary:
            fields["output"] = fields["output"][:text_limit]
        view = {
            "command_id": record.command_id,
            "status": status,
            "terminal": status != "running",
            "description": record.command[: 500 if summary else 2_000],
            "error": (
                f"Command exited with code {record.returncode}"
                if status == "failed" and record.returncode is not None else ""
            )[:text_limit],
            "returncode": record.returncode if status != "running" else None,
            "cancel_requested": record.cancel_requested,
            "cancel_reason": record.cancel_reason[:text_limit],
            "created_at": record.created_at,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            **fields,
        }
        if status == "unknown":
            view["outcome"] = "unconfirmed"
            if not summary:
                view["note"] = (
                    "Status cannot be confirmed. This is not a successful completion."
                )
        elif view["terminal"]:
            view["outcome"] = status
        else:
            view["outcome"] = "waiting"
        if record.cancel_requested and status == "running":
            view["message"] = (
                "Termination was requested. The command has not exited yet."
            )
        return view

    def _status(self, record: CommandRecord) -> str:
        live = self._live.get(record.command_id)
        if live is None and record.disposition == "running":
            return "unknown"
        if live is not None and not live.done.is_set():
            return "running"
        return record.disposition

    def _page(self, live: _LiveCommand | None, offset: int, limit: int) -> OutputSlice:
        if live is None:
            return OutputSlice("", offset, None, 0, False)
        return live.log.read(offset, limit)

    def _require_record(self, command_id: str) -> CommandRecord:
        record = self._session.get_command(command_id)
        if record is None:
            raise ExecutionNotFound("command", command_id)
        return record

    def _background_payload(self, command_id: str, execution, *, timed_out: bool) -> dict:
        live = self._live[command_id]
        page = self._page(live, 0, DEFAULT_OUTPUT_LIMIT)
        data = {
            "command_id": command_id,
            "timed_out": timed_out,
            "message": (
                f"Command is running in the background as command_id {command_id}. "
                "Use get_command, wait_command, list_commands, or terminate_command."
            ),
            **self._output_fields(page),
            **self._cwd_payload(execution),
        }
        return data

    def _emit_done(self, command_id: str) -> None:
        if self._notify is None:
            return
        try:
            self._notify(command_id)
        except Exception:
            logger.debug("command completion notification failed", exc_info=True)

    @staticmethod
    def _output_fields(page: OutputSlice) -> dict:
        text = page.text
        if page.storage_truncated and page.next_offset is None:
            text += _STORAGE_NOTE
        return {
            "output": text,
            "offset": page.offset,
            "next_offset": page.next_offset,
            "truncated": page.truncated,
            "storage_truncated": page.storage_truncated,
        }

    @staticmethod
    def _cwd_payload(execution) -> dict:
        if execution is None:
            return {"cwd": "."}
        return {"cwd": execution.display_path(execution.cwd())}


def _bounded_offset(offset: int) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be an integer >= 0")
    return offset


def _bounded_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if limit < 1 or limit > MAX_OUTPUT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_OUTPUT_LIMIT}")
    return limit


# Re-export mismatch so command tools can catch one module's errors.
__all__ = [
    "DEFAULT_OUTPUT_LIMIT",
    "MAX_OUTPUT_LIMIT",
    "CommandCancelled",
    "CommandExecution",
    "CommandOutcome",
    "ExecutionKindMismatch",
    "ExecutionNotFound",
    "ExecutionWaitCancelled",
]
