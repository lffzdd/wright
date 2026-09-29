"""Contract tests for the three model-facing execution tool families."""

import os
import subprocess
import time

from wright.application.command.execution import CommandExecution
from wright.application.composition.services import RuntimeServices
from wright.application.execution.identity import bind_identity
from wright.application.session.live_resources import RuntimeResources
from wright.application.tool_execution.capabilities import assemble_tool_capabilities
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.domain.model.command import CommandRecord
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall
from wright.domain.policy import PermissionPolicy, PermissionSettings
from wright.infrastructure.persistence.autonomy_store import AutonomyStore
from wright.infrastructure.tools.agent_tools import agent_tools
from wright.infrastructure.tools.schedule import schedule_tools
from wright.infrastructure.tools.command import execute_command_tool
from wright.infrastructure.tools.command.control import command_tools


def _executor(session, services=None):
    store = services.durable_store if services is not None else None
    identity = bind_identity(session, store)
    resources = RuntimeResources(
        session.session_id,
        commands=CommandExecution(session, identity),
        identity=identity,
    )
    return ToolDispatchService(
        {
            tool.name: tool
            for tool in [*agent_tools, *command_tools, *schedule_tools, execute_command_tool]
        },
        assemble_tool_capabilities(
            session,
            services,
            resources,
            expose_scheduling=store is not None,
        ),
        session=session,
        permission_policy=PermissionPolicy(PermissionSettings(mode="bypass")),
    )


def _call(executor, tool_name, **arguments):
    return executor.execute([ToolCall(tool_name, arguments, tool_name)])[0].result


def _session(tmp_path, name="root"):
    session = Session.create(name, tmp_path)
    session.begin_user_turn(name)
    return session


def test_command_tools_follow_background_and_timeout_handoff(tmp_path):
    session = _session(tmp_path)
    executor = _executor(session)
    launched = _call(
        executor, "execute_command", command="echo handed-off", run_in_background=True
    )
    assert launched.ok
    command_id = launched.data["command_id"]
    assert "task_id" not in launched.data
    assert "get_command" in launched.data["message"]

    observed = _call(executor, "get_command", command_id=command_id)
    assert observed.ok and observed.data["command_id"] == command_id
    waited = _call(executor, "wait_command", command_id=command_id, timeout=2)
    assert waited.ok and waited.data["status"] == "completed"
    assert waited.data["returncode"] == 0
    assert "handed-off" in waited.data["output"]
    again = _call(executor, "terminate_command", command_id=command_id)
    assert again.ok and again.data["already_terminal"] is True
    assert again.data["status"] == "completed"

    timed = _call(
        executor,
        "execute_command",
        command="sleep 2",
        timeout=0,
    )
    assert timed.ok and timed.data["timed_out"] is True
    assert "command_id" in timed.data
    assert "terminate_command" in timed.data["message"]
    _call(executor, "terminate_command", command_id=timed.data["command_id"])


def test_wait_timeout_does_not_terminate_and_list_stays_in_session(tmp_path):
    session = _session(tmp_path, "turn-one")
    current = _executor(session)
    first = _call(
        current, "execute_command", command="sleep 5", run_in_background=True
    )
    session.append_message({"role": "user", "content": "next turn"})
    session.begin_user_turn("turn-two")
    second = Session.create("other", tmp_path)
    second.begin_user_turn("other")
    other = _executor(second)
    launched = _call(
        current, "execute_command", command="echo current-turn", run_in_background=True
    )
    command_id = launched.data["command_id"]
    waited = _call(current, "wait_command", command_id=first.data["command_id"], timeout=0)
    assert waited.ok and waited.data["wait_timed_out"] is True
    assert waited.data["cancel_requested"] is False
    still = _call(current, "get_command", command_id=first.data["command_id"])
    assert still.data["status"] == "running"

    finished = _call(current, "wait_command", command_id=command_id, timeout=2)
    assert finished.data["status"] == "completed"
    current_only = _call(current, "list_commands")
    assert current_only.data["scope"] == "current_user_turn"
    assert [row["command_id"] for row in current_only.data["commands"]] == [command_id]
    whole_session = _call(current, "list_commands", scope="session")
    assert whole_session.data["scope"] == "session"
    assert {row["command_id"] for row in whole_session.data["commands"]} == {
        first.data["command_id"],
        command_id,
    }
    assert _call(other, "list_commands", scope="session").data["count"] == 0
    running = _call(current, "list_commands", status="running", scope="session")
    assert [row["command_id"] for row in running.data["commands"]] == [
        first.data["command_id"]
    ]
    _call(current, "terminate_command", command_id=first.data["command_id"])


def test_list_commands_caps_count_and_output(tmp_path):
    session = _session(tmp_path)
    turn_id = session.agent_root_turn_id
    for index in range(101):
        session.register_command(CommandRecord(
            command_id=f"cmd_{index:03d}",
            command="x" * 2_000,
            root_turn_id=turn_id,
            created_at=index,
        ))
    listed = _call(_executor(session), "list_commands")
    assert listed.ok
    assert listed.data["count"] == 100
    assert listed.data["next_cursor"]
    sample = listed.data["commands"][0]
    assert len(sample["description"]) <= 500
    assert len(sample["output"]) <= 500
    assert sample["status"] == "unknown"
    assert sample["outcome"] == "unconfirmed"
    assert "note" not in sample or "successful completion" in sample.get("note", "")


def test_unknown_command_is_not_a_successful_completion(tmp_path):
    session = _session(tmp_path)
    session.register_command(CommandRecord(
        command_id="cmd_lost",
        command="lost",
        root_turn_id=session.agent_root_turn_id,
    ))
    result = _call(_executor(session), "get_command", command_id="cmd_lost")
    assert result.ok
    assert result.data["status"] == "unknown"
    assert result.data["outcome"] == "unconfirmed"
    assert result.data["outcome"] != "completed"
    assert "not a successful completion" in result.data["note"]


def test_terminate_command_kills_the_process_tree(tmp_path):
    session = _session(tmp_path)
    pidfile = tmp_path / "child.pid"
    executor = _executor(session)
    launched = _call(
        executor,
        "execute_command",
        command=f"sleep 30 & echo $! > '{pidfile}'; wait",
        run_in_background=True,
    )
    command_id = launched.data["command_id"]
    deadline = time.monotonic() + 2
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child_pid = int(pidfile.read_text().strip())
    terminated = _call(executor, "terminate_command", command_id=command_id)
    assert terminated.ok
    assert terminated.data["status"] == "cancelled"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not _process_stopped(child_pid):
        time.sleep(0.02)
    assert _process_stopped(child_pid)


def _process_stopped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return not state or state.startswith("Z")


def test_cross_type_ids_fail_before_any_control(tmp_path):
    store = AutonomyStore(tmp_path / "tasks.db", session_id="session", workspace_dir=tmp_path)
    session = Session.create("root", tmp_path, session_id="session")
    session.begin_user_turn("root")
    services = RuntimeServices(durable_store=store)
    executor = _executor(session, services)
    agent = session.control_plane.begin_task(
        root_turn_id=session.agent_root_turn_id,
        parent_id=None,
        tool_call_id="call_agent",
        depth=1,
        task="stay running",
        requested_steps=3,
    )
    child = session.control_plane.begin_task(
        root_turn_id=session.agent_root_turn_id,
        parent_id=agent.id,
        tool_call_id="call_child",
        depth=2,
        task="descendant",
        requested_steps=3,
    )
    launched = _call(
        executor, "execute_command", command="sleep 5", run_in_background=True
    )
    command_id = launched.data["command_id"]
    schedule = _call(
        executor,
        "create_schedule",
        name="interval",
        prompt="later",
        trigger={"type": "interval", "every_seconds": 60, "start_in_seconds": 0},
    )
    schedule_id = schedule.data["schedule_id"]
    run_id = store.materialize_due()[0]
    assert store.get_run(run_id).status == "queued"

    wrong_agent = _call(executor, "cancel_agent", agent_task_id=command_id)
    wrong_command = _call(executor, "terminate_command", command_id=agent.id)
    wrong_run = _call(executor, "cancel_schedule_run", run_id=agent.id)
    assert not wrong_agent.ok and "command execution" in wrong_agent.err
    assert not wrong_command.ok and "delegated agent execution" in wrong_command.err
    assert not wrong_run.ok and "delegated agent execution" in wrong_run.err
    assert session.get_command(command_id).cancel_requested is False
    assert _call(executor, "get_command", command_id=command_id).data["status"] == "running"
    assert session.control_plane.get(agent.id).cancel_requested is False
    assert store.get_run(run_id).status == "queued"
    assert store.get_job(schedule_id).status == "active"

    paused = _call(executor, "pause_schedule", schedule_id=schedule_id)
    assert paused.ok and paused.data["status"] == "paused"
    assert store.get_run(run_id).status == "queued"
    cancelled_run = _call(executor, "cancel_schedule_run", run_id=run_id)
    assert cancelled_run.ok
    assert cancelled_run.data["status"] == "cancelled"
    assert cancelled_run.data["schedule_changed"] is False
    assert store.get_job(schedule_id).status == "paused"

    fresh = _call(
        executor,
        "create_schedule",
        name="again",
        prompt="again",
        trigger={"type": "interval", "every_seconds": 60, "start_in_seconds": 0},
    )
    fresh_id = fresh.data["schedule_id"]
    fresh_run = store.materialize_due()[0]
    claimed = store.claim_next_run(owner_id="test")
    assert claimed is not None and claimed.status == "dispatched"
    seen = _call(executor, "get_schedule_run", run_id=claimed.id)
    assert seen.data["status"] == "dispatched"
    assert seen.data["outcome"] == "waiting"
    rule = _call(executor, "cancel_schedule", schedule_id=fresh_id)
    assert rule.ok and rule.data["status"] == "cancelled"
    assert store.get_run(fresh_run).status == "cancelled"

    parent = _call(executor, "cancel_agent", agent_task_id=agent.id, reason="stop tree")
    assert parent.ok and parent.data["status"] == "running"
    assert session.control_plane.get(child.id).cancel_requested is True
    assert session.control_plane.get(child.id).status == "running"
    _call(executor, "terminate_command", command_id=command_id)
    store.close()
