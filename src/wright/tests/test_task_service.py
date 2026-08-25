import queue

from ..agent import Agent
from ..main import _task_notification_event
from ..renderer import SilentRenderer
from ..session import SessionState
from ..subagent import (
    build_agent_tools,
    cancel_agent_task_tool,
    get_agent_task_tool,
)
from ..tasks import TaskService
from ..tools.base import ToolRuntime
from ..tools.command_tools import (
    execute_command,
    get_task_output,
    get_task_output_tool,
)
from ..tools.task_tools import (
    cancel_task_tool,
    get_task_tool,
    list_tasks_tool,
    wait_task_tool,
)


def _session(tmp_path):
    session = SessionState.create("root", tmp_path)
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
    runtime = ToolRuntime(session_state=session, workspace_dir=tmp_path)
    launched = execute_command(
        "echo shell-result",
        run_in_background=True,
        runtime=runtime,
    )

    service = TaskService.for_session(session)
    shell = service.wait(launched.data["task_id"], timeout=2)
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
        launched.data["task_id"],
    }


def test_unified_tools_query_wait_list_and_keep_legacy_aliases(tmp_path):
    session = _session(tmp_path)
    record = _agent_task(session, status="completed")
    runtime = ToolRuntime(session_state=session, workspace_dir=tmp_path)

    queried = get_task_tool.call({"task_id": record.id}, runtime)
    waited = wait_task_tool.call({"task_id": record.id, "timeout": 0}, runtime)
    listed = list_tasks_tool.call({}, runtime)
    legacy = get_agent_task_tool.call({"task_id": record.id}, runtime)

    assert queried.ok and queried.data["kind"] == "agent"
    assert waited.ok and waited.data["wait_completed"] is True
    assert listed.ok and listed.data["tasks"][0]["id"] == record.id
    assert legacy.ok and legacy.data["result"] == "agent result"


def test_wait_timeout_observes_without_cancelling_shell_task(tmp_path):
    session = _session(tmp_path)
    runtime = ToolRuntime(session_state=session, workspace_dir=tmp_path)
    launched = execute_command(
        "sleep 5 & wait",
        run_in_background=True,
        runtime=runtime,
    )
    task_id = launched.data["task_id"]

    observed = wait_task_tool.call({"task_id": task_id, "timeout": 0}, runtime)

    assert observed.ok
    assert observed.data["status"] == "running"
    assert observed.data["wait_timed_out"] is True
    assert observed.data["cancel_requested"] is False
    cancelled = cancel_task_tool.call(
        {"task_id": task_id, "reason": "test cleanup"}, runtime
    )
    assert cancelled.ok
    assert cancelled.data["status"] == "cancelled"


def test_unified_cancel_routes_agent_and_shell_with_legacy_views(tmp_path):
    session = _session(tmp_path)
    agent_record = _agent_task(session)
    runtime = ToolRuntime(session_state=session, workspace_dir=tmp_path)
    launched = execute_command(
        "sleep 5",
        run_in_background=True,
        runtime=runtime,
    )

    agent = cancel_task_tool.call(
        {"task_id": agent_record.id, "reason": "stop agent"}, runtime
    )
    shell = cancel_task_tool.call(
        {"task_id": launched.data["task_id"], "reason": "stop shell"}, runtime
    )

    assert agent.ok and agent.data["kind"] == "agent"
    assert agent.data["status"] == "running"  # cooperative cancellation
    assert agent.data["cancel_requested"] is True
    assert shell.ok and shell.data["status"] == "cancelled"
    assert get_task_output(launched.data["task_id"], runtime).data["done"] is True
    legacy_cancel = cancel_agent_task_tool.call(
        {"task_id": agent_record.id, "reason": "again"}, runtime
    )
    assert legacy_cancel.ok and legacy_cancel.data["cancel_requested"] is True


def test_agent_and_shell_completion_share_runtime_event_shape(tmp_path):
    session = _session(tmp_path)
    agent_record = _agent_task(session, status="completed")
    notifications = queue.Queue()
    runtime = ToolRuntime(
        session_state=session,
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

    assert shell_id == launched.data["task_id"]
    assert agent_event["type"] == shell_event["type"] == "task_notification"
    assert set(agent_event["task"]) == set(shell_event["task"])
    assert agent_event["task"]["kind"] == "agent"
    assert shell_event["task"]["kind"] == "shell"


def test_legacy_alias_stays_executable_but_is_hidden_from_new_prompt(tmp_path):
    class UnusedLLM:
        context_limit = 128_000

        def __call__(self, messages):
            raise AssertionError("not called")

    session = _session(tmp_path)
    Agent(
        UnusedLLM(),
        [get_task_tool, get_task_output_tool],
        session,
        SilentRenderer(),
    )
    system_prompt = str(session.messages[0]["content"])

    assert '"name": "get_task"' in system_prompt
    assert '"name": "get_task_output"' not in system_prompt
    assert get_task_output_tool.expose_to_model is False


def test_unified_task_control_is_root_only():
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

    unified = {"get_task", "wait_task", "cancel_task", "list_tasks"}
    autonomy = {
        "create_task", "get_schedule", "list_schedules", "pause_schedule",
        "resume_schedule", "cancel_schedule", "list_task_runs",
    }
    assert unified <= root_names
    assert unified.isdisjoint(child_names)
    assert autonomy <= root_names
    assert autonomy.isdisjoint(child_names)
