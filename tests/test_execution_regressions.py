"""Regressions for command ownership, output paging, and store races."""

from __future__ import annotations

import os
import shlex
import sqlite3
import threading
import time
from pathlib import Path

from wright.application.command.execution import CommandExecution
from wright.application.execution.identity import bind_identity
from wright.application.session.live_resources import RuntimeResources
from wright.application.tool_execution.runtime import tool_runtime_for_session
from wright.domain.model.scheduling import TriggerSpec
from wright.domain.model.session import Session
from wright.infrastructure.persistence.autonomy_store import AutonomyStore
from wright.infrastructure.tools.command import execute_command
from wright.infrastructure.tools.command.control import get_command, terminate_command


def _session(tmp_path: Path) -> Session:
    session = Session.create("root", tmp_path)
    session.begin_user_turn("root")
    return session


def _runtime(session: Session, tmp_path: Path, **kwargs):
    return tool_runtime_for_session(session, workspace_dir=tmp_path, **kwargs)


def _stopped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_parent_exit_does_not_leave_owned_children(tmp_path):
    session = _session(tmp_path)
    runtime = _runtime(session, tmp_path)
    pidfile = tmp_path / "child.pid"
    launched = execute_command(
        f"sleep 30 & echo $! > {shlex.quote(str(pidfile))}",
        run_in_background=True,
        runtime=runtime,
    )
    assert launched.ok
    command_id = launched.data["command_id"]
    deadline = time.monotonic() + 2
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child = int(pidfile.read_text().strip())
    observed = get_command(command_id, runtime=runtime)
    assert observed.data["status"] == "running"
    terminated = terminate_command(command_id, runtime=runtime)
    assert terminated.data["terminated"] is True
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not _stopped(child):
        time.sleep(0.02)
    assert _stopped(child)


def test_ignored_sigterm_is_escalated_on_the_owned_group(tmp_path):
    session = _session(tmp_path)
    runtime = _runtime(session, tmp_path)
    code = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    )
    launched = execute_command(
        f"python3 -c {shlex.quote(code)}",
        run_in_background=True,
        runtime=runtime,
    )
    command_id = launched.data["command_id"]
    time.sleep(0.2)
    terminated = terminate_command(command_id, runtime=runtime)
    assert terminated.data["terminated"] is True
    assert terminated.data["status"] == "cancelled"


def test_output_pages_keep_a_prefix_and_mark_truncation(tmp_path):
    session = _session(tmp_path)
    identity = bind_identity(session)
    commands = CommandExecution(session, identity, max_output_bytes=32)
    runtime = _runtime(
        session,
        tmp_path,
        runtime_resources=RuntimeResources(
            session.session_id, commands=commands, identity=identity
        ),
    )
    plain = execute_command("printf abc", runtime=runtime)
    assert plain.ok
    assert plain.data["output"] == "abc"
    assert plain.data["truncated"] is False

    payload = "x" * 80
    large = execute_command(f"printf {shlex.quote(payload)}", runtime=runtime)
    assert large.ok
    assert large.data["storage_truncated"] is True
    assert "stored output truncated" in large.data["output"]
    command_id = large.data["command_id"]
    first = get_command(command_id, offset=0, limit=10, runtime=runtime)
    assert first.data["output"] == "x" * 10
    assert first.data["next_offset"] == 10
    rest = get_command(command_id, offset=first.data["next_offset"], limit=100, runtime=runtime)
    assert rest.data["output"].startswith("x")
    assert "stored output truncated" in rest.data["output"]
    assert rest.data["next_offset"] is None


def test_cancel_waits_for_the_running_transition(tmp_path):
    store = AutonomyStore(tmp_path / "tasks.db", session_id="s", workspace_dir=tmp_path)
    store.create_job(
        name="race",
        prompt="race",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    run_id = store.materialize_due(now=0)[0]
    claimed = store.claim_next_run(owner_id="worker", now=0)
    assert claimed is not None and claimed.status == "dispatched"

    other = sqlite3.connect(store.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    other.execute(
        "UPDATE durable_runs SET status = 'running', started_at = 1 WHERE id = ?",
        (run_id,),
    )
    box: dict = {}

    def cancel() -> None:
        box["result"] = store.cancel_run(run_id, "stop")

    thread = threading.Thread(target=cancel)
    thread.start()
    time.sleep(0.2)
    assert thread.is_alive()
    other.execute("COMMIT")
    thread.join(2)
    assert not thread.is_alive()
    result = box["result"]
    assert result.cooperative is True
    assert result.run.status == "running"
    assert result.run.cancel_requested is True
    assert result.run.ended_at is None
    other.close()
    store.close()


def test_start_does_not_overwrite_a_committed_cancel(tmp_path):
    store = AutonomyStore(tmp_path / "tasks.db", session_id="s", workspace_dir=tmp_path)
    store.create_job(
        name="race",
        prompt="race",
        trigger=TriggerSpec(type="once", run_at=0),
        now=0,
    )
    run_id = store.materialize_due(now=0)[0]
    store.claim_next_run(owner_id="worker", now=0)
    other = sqlite3.connect(store.path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    other.execute(
        "UPDATE durable_runs SET status = 'cancelled', ended_at = 2 WHERE id = ?",
        (run_id,),
    )
    box: dict = {}

    def start() -> None:
        try:
            box["result"] = store.start_run(run_id, owner_id="worker")
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=start)
    thread.start()
    time.sleep(0.2)
    assert thread.is_alive()
    other.execute("COMMIT")
    thread.join(2)
    assert "error" in box
    assert store.get_run(run_id).status == "cancelled"
    other.close()
    store.close()
