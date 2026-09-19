"""Crash-window tests using only subprocesses created by this test module."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from ...application_host import ApplicationHost
from ...autonomy import AutonomyStore
from ...permission import PermissionSettings
from ...tests.autonomy.test_host_persistence import ScriptLLM


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    source_root = str(Path(__file__).parents[3] / "src")
    env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_child(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-u", "-c", textwrap.dedent(code), *args],
        env=_child_env(),
        text=True,
        capture_output=True,
        timeout=8,
        check=False,
    )


def _store(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, tmp_path / "tasks.sqlite3"


@pytest.mark.parametrize(
    "crash_point,expected_after_recovery",
    [
        ("accepted", "completed"),
        ("claimed", "completed"),
        ("running", "unknown"),
    ],
)
def test_real_process_command_crash_windows_are_recovered_honestly(
    tmp_path, crash_point, expected_after_recovery
):
    workspace, db = _store(tmp_path)
    child = r'''
        import os, queue, sys, threading, time
        from types import SimpleNamespace
        from wright.autonomy import AutonomyStore, TriggerSpec
        from wright.interaction import InteractionBroker
        from wright.session_service import SessionService
        from wright.ui_events import EventPublisher

        from pathlib import Path
        workspace, db, point = sys.argv[1:]
        workspace, db = Path(workspace), Path(db)
        store = AutonomyStore(db, session_id="session", workspace_dir=workspace)
        publisher = EventPublisher(project_id="project", session_id="session")
        runtime = SimpleNamespace(
            session_state=SimpleNamespace(
                session_id="session", status="idle", model_name="fake",
                user_goal="", environment="local", workspace_dir=workspace,
                project_root=workspace, base_commit=None, branch_name=None,
                llm_transport="chat", lifecycle="open",
                attachment_records=lambda _ids: [],
            ),
            publisher=publisher, interaction_broker=InteractionBroker(publisher),
            event_queue=queue.Queue(), agent_idle=threading.Event(),
            cancellation_event=threading.Event(), llm=SimpleNamespace(model="fake"),
            agent=SimpleNamespace(llm=SimpleNamespace(model="fake"), checkpoint_store=None),
            resumed=False, autonomy_store=store,
        )
        runtime.agent_idle.set()
        def consume(current, event_type, payload):
            if event_type == "EXIT":
                return True
            if point == "running":
                current.agent.on_run_started("run-before-crash")
            return False
        service = SessionService(runtime, event_processor=consume, shutdown=lambda _rt: None)
        if point == "accepted":
            def crash_after_accept(*_args, **_kwargs):
                os._exit(41)
            service._queue_input = crash_after_accept
        service.start()
        if point == "claimed":
            original_claim = store.claim_command
            def crash_after_claim(*args, **kwargs):
                original_claim(*args, **kwargs)
                os._exit(42)
            store.claim_command = crash_after_claim
        if point == "running":
            original_start = store.start_command
            def crash_after_start(*args, **kwargs):
                result = original_start(*args, **kwargs)
                os._exit(43)
                return result
            store.start_command = crash_after_start
        service.submit("recoverable", "crash-command")
        if point != "accepted":
            while True:
                time.sleep(1)
    '''
    result = _run_child(child, str(workspace), str(db), crash_point)
    assert result.returncode != 0, (result.returncode, result.stdout, result.stderr)

    reopened = AutonomyStore(db, session_id="session", workspace_dir=workspace)
    runtime = _minimal_runtime(reopened, workspace)
    seen = threading.Event()

    def consume(_runtime, event_type, _payload):
        if event_type == "EXIT":
            return True
        seen.set()
        return False

    from ...session_service import SessionService

    service = SessionService(runtime, event_processor=consume, shutdown=lambda _rt: None)
    service.start()
    if expected_after_recovery == "completed":
        assert seen.wait(2)
        assert service.command_status("crash-command")["status"] == "completed"
    else:
        assert not seen.wait(0.15)
        assert service.command_status("crash-command")["status"] == "unknown"
    service.close(wait_timeout=1)
    reopened.close()


def _minimal_runtime(store, workspace):
    import queue
    import threading
    from types import SimpleNamespace

    from ...interaction import InteractionBroker
    from ...ui_events import EventPublisher

    publisher = EventPublisher(project_id="project", session_id="session")
    return SimpleNamespace(
        session_state=SimpleNamespace(
            session_id="session", status="idle", model_name="fake", user_goal="",
            environment="local", workspace_dir=workspace, project_root=workspace,
            base_commit=None, branch_name=None, llm_transport="chat", lifecycle="open",
            attachment_records=lambda _ids: [],
        ),
        publisher=publisher, interaction_broker=InteractionBroker(publisher),
        event_queue=queue.Queue(), agent_idle=threading.Event(),
        cancellation_event=threading.Event(), llm=SimpleNamespace(model="fake"),
        agent=SimpleNamespace(llm=SimpleNamespace(model="fake"), checkpoint_store=None),
        resumed=False, autonomy_store=store,
    )


def test_real_host_process_lock_is_released_for_takeover_after_crash(tmp_path):
    workspace, db = _store(tmp_path)
    child = r'''
        import sys
        from pathlib import Path
        from wright.application_host import ApplicationHost
        from wright.autonomy import AutonomyStore, TriggerSpec
        from wright.permission import PermissionSettings
        class Fake:
            context_limit = 128000
            model = "fake"
            transport_name = "chat"
            def __call__(self, _messages, **_kwargs):
                if False:
                    yield None
        workspace, db = map(Path, sys.argv[1:])
        store = AutonomyStore(db, session_id="child", workspace_dir=workspace)
        host = ApplicationHost(workspace_dir=workspace, store=store, llm=Fake(),
                               base_tools=[], permission_settings=PermissionSettings())
        host.start()
        print("READY", flush=True)
        sys.stdin.read()
    '''
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", textwrap.dedent(child), str(workspace), str(db)],
        env=_child_env(), text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        second_store = AutonomyStore(db, session_id="parent", workspace_dir=workspace)
        second = ApplicationHost(
            workspace_dir=workspace, store=second_store, llm=ScriptLLM("done"),
            base_tools=[], permission_settings=PermissionSettings(),
        )
        with pytest.raises(RuntimeError, match="already owns"):
            second.start()
        process.kill()
        assert process.wait(timeout=3) == -signal.SIGKILL
        takeover_store = AutonomyStore(db, session_id="takeover", workspace_dir=workspace)
        takeover = ApplicationHost(
            workspace_dir=workspace, store=takeover_store, llm=ScriptLLM("done"),
            base_tools=[], permission_settings=PermissionSettings(),
        )
        takeover.start()
        assert takeover.state == "running"
        assert takeover.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_real_process_child_agent_side_effect_becomes_unknown_without_replay(tmp_path):
    """Kill a real durable parent/child worker after the child side effect starts."""
    workspace, db = _store(tmp_path)
    marker = workspace / "child-effect.txt"
    started = workspace / "child-effect.started"
    child = r'''
        import sys, time
        from pathlib import Path
        from wright.application_host import ApplicationHost
        from wright.autonomy import AutonomyStore, TriggerSpec
        from wright.permission import PermissionSettings
        from wright.tests.responses import event, response
        from wright.tools.base import Tool, ToolResult

        workspace, db, marker, started = map(Path, sys.argv[1:])

        class Model:
            context_limit = 128000
            model = "fake"
            transport_name = "chat"

            def __call__(self, messages, **_kwargs):
                user_text = " ".join(
                    str(item.get("content", ""))
                    for item in messages if item.get("role") == "user"
                )
                if "write child" in user_text:
                    if messages[-1].get("role") == "tool":
                        yield event(response(content="child finished", calls=[]))
                    else:
                        yield event(response(calls=[{"name": "effect", "arguments": {}}]))
                elif messages[-1].get("role") == "tool":
                    yield event(response(content="parent finished", calls=[]))
                else:
                    yield event(response(calls=[{
                        "name": "spawn_agent",
                        "arguments": {"task": "write child"},
                    }]))

        def effect(_args, _runtime):
            with marker.open("a", encoding="utf-8") as output:
                output.write("effect\n")
            started.write_text("started", encoding="utf-8")
            while True:
                time.sleep(0.01)
            return ToolResult.success("unreachable")

        store = AutonomyStore(db, session_id="source", workspace_dir=workspace)
        store.create_automation(
            name="child-crash", prompt="delegate child work",
            trigger=TriggerSpec(type="once", run_at=0),
            recovery_policy="retry", max_retries=1, now=0,
        )
        host = ApplicationHost(
            workspace_dir=workspace, store=store, llm=Model(),
            base_tools=[Tool("effect", "append a marker", {"type": "object"}, effect)],
            permission_settings=PermissionSettings(), poll_interval=0.01,
        )
        host.start()
        while True:
            time.sleep(1)
    '''
    process = subprocess.Popen(
        [
            sys.executable, "-u", "-c", textwrap.dedent(child),
            str(workspace), str(db), str(marker), str(started),
        ],
        env=_child_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not started.exists():
            time.sleep(0.01)
        if not started.exists():
            process.kill()
            stdout, stderr = process.communicate(timeout=3)
            pytest.fail(f"child did not reach effect: stdout={stdout!r} stderr={stderr!r}")
        process.kill()
        assert process.wait(timeout=3) == -signal.SIGKILL

        recovery = r'''
            import json, sys
            from pathlib import Path
            from wright.autonomy import AutonomyStore
            workspace, db = map(Path, sys.argv[1:])
            store = AutonomyStore(db, session_id="source", workspace_dir=workspace)
            recovered = store.recover_interrupted(now=10)
            run = store.list_runs()[0]
            executions = store.list_tool_executions(run.id)
            print(json.dumps({
                "recovered": [item.id for item in recovered],
                "run_status": run.status,
                "executions": executions,
            }), flush=True)
            store.close()
        '''
        resumed = _run_child(recovery, str(workspace), str(db))
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        report = json.loads(resumed.stdout.strip())
        assert report["run_status"] == "unknown"
        assert report["recovered"]
        execution = next(item for item in report["executions"] if item["tool_name"] == "effect")
        assert execution["status"] == "unknown"
        assert execution["agent_task_id"]
        assert execution["parent_agent_task_id"]
        assert marker.read_text(encoding="utf-8") == "effect\n"

        # The retry policy is deliberately present, but an already-started
        # child effect must not be replayed by a fresh host.
        verify = _run_child(recovery, str(workspace), str(db))
        assert verify.returncode == 0
        assert marker.read_text(encoding="utf-8") == "effect\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
