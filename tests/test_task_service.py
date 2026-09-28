import queue

from wright.application.agent import build_agent_tools
from wright.application.agent.assembly import prepare_model_tools
from wright.application.session.dispatch import _task_notification_event
from wright.application.tasks.service import TaskService
from wright.application.tool_execution.runtime import tool_runtime_for_session
from wright.domain.model.session import Session
from wright.infrastructure.tools.agent_tools import (
    cancel_agent_tool,
    get_agent_tool,
    wait_agent_tool,
)
from wright.infrastructure.tools.command import execute_command
from wright.infrastructure.tools.command.control import (
    terminate_command_tool,
    wait_command_tool,
)


def _session(tmp_path):
    session = Session.create("root", tmp_path)
    session.begin_user_turn("root")
    return session


def _agent_task(session, *, status="running"):
    record = session.control_plane.begin_task(
        root_turn_id=session.agent_root_turn_id,
        parent_id=None,
        tool_call_id="call_agent",
        depth=1,
        task="inspect the repository",
        requested_steps=5,
    )
    if status != "running":
        record = session.control_plane.finish_task(
            record.id,
            status=status,
            steps_used=2,
            result="agent result" if status == "completed" else "",
            error="agent error" if status == "failed" else "",
        )
    return record


def test_service_projects_agent_and_shell_without_copying_ownership(tmp_path):
    session = _session(tmp_path)
    agent_record = _agent_task(session, status="completed")
    runtime = tool_runtime_for_session(session, workspace_dir=tmp_path)
    launched = execute_command(
        "echo shell-result",
        run_in_background=True,
        runtime=runtime,
    )

    service = TaskService.for_session(session)
    shell = service.wait(launched.data["command_id"], timeout=2)
    agent = service.get(agent_record.id)

    assert agent.kind == "agent"
    assert agent.status == "completed"
    assert agent.result == "agent result"
    assert agent.details["usage"]["total_tokens"] == 0
    assert shell.kind == "shell"
    assert shell.status == "completed"
    assert shell.returncode == 0
    assert shell.output == "shell-result\n"
    assert {task.id for task in service.list()} == {
        agent_record.id,
        launched.data["command_id"],
    }


def test_agent_tools_query_and_wait(tmp_path):
    session = _session(tmp_path)
    record = _agent_task(session, status="completed")
    runtime = tool_runtime_for_session(session, workspace_dir=tmp_path)

    queried = get_agent_tool.call({"agent_task_id": record.id}, runtime)
    waited = wait_agent_tool.call({"agent_task_id": record.id, "timeout": 0}, runtime)

    assert queried.ok and queried.data["agent_task_id"] == record.id
    assert queried.data["status"] == "completed"
    assert "id" not in queried.data
    assert waited.ok and waited.data["wait_completed"] is True
    assert waited.data["usage"]["total_tokens"] == 0


def test_wait_timeout_observes_without_cancelling_shell_task(tmp_path):
    session = _session(tmp_path)
    runtime = tool_runtime_for_session(session, workspace_dir=tmp_path)
    launched = execute_command(
        "sleep 5 & wait",
        run_in_background=True,
        runtime=runtime,
    )
    command_id = launched.data["command_id"]

    observed = wait_command_tool.call(
        {"command_id": command_id, "timeout": 0}, runtime
    )

    assert observed.ok
    assert observed.data["status"] == "running"
    assert observed.data["wait_timed_out"] is True
    assert observed.data["cancel_requested"] is False
    cancelled = terminate_command_tool.call(
        {"command_id": command_id, "reason": "test cleanup"}, runtime
    )
    assert cancelled.ok
    assert cancelled.data["status"] == "cancelled"


def test_agent_cancel_and_command_terminate_keep_their_semantics(tmp_path):
    session = _session(tmp_path)
    agent_record = _agent_task(session)
    runtime = tool_runtime_for_session(session, workspace_dir=tmp_path)
    launched = execute_command(
        "sleep 5",
        run_in_background=True,
        runtime=runtime,
    )

    agent = cancel_agent_tool.call(
        {"agent_task_id": agent_record.id, "reason": "stop agent"}, runtime
    )
    shell = terminate_command_tool.call(
        {"command_id": launched.data["command_id"], "reason": "stop shell"}, runtime
    )

    assert agent.ok
    assert agent.data["status"] == "running"
    assert agent.data["cancel_requested"] is True
    assert shell.ok and shell.data["status"] == "cancelled"
    assert shell.data["command_id"] == launched.data["command_id"]
    assert TaskService.for_session(session).get(launched.data["command_id"]).terminal


def test_agent_and_shell_completion_share_runtime_event_shape(tmp_path):
    session = _session(tmp_path)
    agent_record = _agent_task(session, status="completed")
    notifications = queue.Queue()
    runtime = tool_runtime_for_session(
        session,
        workspace_dir=tmp_path,
        notify_background_done=notifications.put,
    )
    launched = execute_command(
        "echo done",
        run_in_background=True,
        runtime=runtime,
    )
    shell_id = notifications.get(timeout=2)
    service = TaskService.for_session(session)

    agent_event = _task_notification_event(service.get(agent_record.id))
    shell_event = _task_notification_event(service.get(shell_id))

    assert shell_id == launched.data["command_id"]
    assert agent_event["type"] == shell_event["type"] == "task_notification"
    assert agent_event["task"]["agent_task_id"] == agent_record.id
    assert "get_agent" in agent_event["follow_up"]
    assert shell_event["task"]["command_id"] == shell_id
    assert "get_command" in shell_event["follow_up"]
    assert "id" not in agent_event["task"]
    assert "id" not in shell_event["task"]


def test_execution_controls_are_root_only_and_history_does_not_restore_retired_names(tmp_path):
    class UnusedLLM:
        context_limit = 128_000

    root_names = {
        tool.name
        for tool in build_agent_tools(
            UnusedLLM(), [], depth=0, max_depth=1, enable_autonomy=True
        )
    }
    child_names = {
        tool.name
        for tool in build_agent_tools(UnusedLLM(), [], depth=1, max_depth=1)
    }

    agent_controls = {"get_agent", "wait_agent", "cancel_agent"}
    command_controls = {
        "get_command", "wait_command", "terminate_command", "list_commands",
    }
    autonomy = {
        "create_schedule", "get_schedule", "list_schedules", "pause_schedule",
        "resume_schedule", "cancel_schedule", "list_schedule_runs",
        "get_schedule_run", "wait_schedule_run", "cancel_schedule_run",
    }
    retired = {
        "get_task", "wait_task", "cancel_task", "list_tasks",
        "schedule_task", "list_task_runs",
    }
    assert agent_controls <= root_names
    assert command_controls <= root_names
    assert agent_controls.isdisjoint(child_names)
    assert command_controls.isdisjoint(child_names)
    assert autonomy <= root_names
    assert autonomy.isdisjoint(child_names)
    assert retired.isdisjoint(root_names)
    assert retired.isdisjoint(child_names)

    restored = Session.create("history", tmp_path)
    restored.active_deferred_tools = ["schedule_task", "get_task", "create_schedule"]
    prepared = prepare_model_tools(
        restored,
        build_agent_tools(
            UnusedLLM(), [], depth=0, max_depth=1, enable_autonomy=True
        ),
    )
    schema_names = {item["name"] for item in prepared.schemas}
    assert "schedule_task" not in schema_names
    assert "get_task" not in schema_names
    assert "create_schedule" in schema_names
    assert restored.active_deferred_tools == ["create_schedule"]
