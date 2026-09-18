from ...autonomy import AutonomyStore
from ...services import RuntimeServices
from ...session import Session
from ...tools.autonomy_tools import (
    cancel_schedule_tool,
    get_schedule_tool,
    list_schedules_tool,
    list_task_runs_tool,
    pause_schedule_tool,
    resume_schedule_tool,
    schedule_task_tool,
)
from ...tools.base import tool_runtime_for_session


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = AutonomyStore(
        tmp_path / "tasks.sqlite3",
        session_id="session",
        workspace_dir=workspace,
    )
    session = Session.create("root", workspace)
    session.session_id = "session"
    return store, tool_runtime_for_session(
        session,
        workspace_dir=workspace,
        services=RuntimeServices(durable_store=store),
    )


def test_schedule_tools_cover_definition_lifecycle_and_history(tmp_path):
    store, runtime = _runtime(tmp_path)
    created = schedule_task_tool.call(
        {
            "name": "later",
            "prompt": "inspect later",
            "trigger": {"type": "once", "delay_seconds": 60},
        },
        runtime,
    )
    assert created.ok
    schedule_id = created.data["id"]
    assert get_schedule_tool.call({"schedule_id": schedule_id}, runtime).ok
    assert list_schedules_tool.call({}, runtime).data["count"] == 1

    paused = pause_schedule_tool.call({"schedule_id": schedule_id}, runtime)
    assert paused.ok and paused.data["status"] == "paused"
    resumed = resume_schedule_tool.call({"schedule_id": schedule_id}, runtime)
    assert resumed.ok and resumed.data["status"] == "active"
    cancelled = cancel_schedule_tool.call(
        {"schedule_id": schedule_id, "reason": "changed mind"}, runtime
    )
    assert cancelled.ok and cancelled.data["status"] == "cancelled"
    assert list_task_runs_tool.call(
        {"schedule_id": schedule_id}, runtime
    ).data["count"] == 0
    store.close()


def test_schedule_task_is_model_visible():
    assert schedule_task_tool.name == "schedule_task"
    assert schedule_task_tool.expose_to_model is True


def test_schedule_task_reports_trigger_shape_error_cleanly(tmp_path):
    store, runtime = _runtime(tmp_path)
    result = schedule_task_tool.call(
        {
            "name": "broken",
            "prompt": "broken",
            "trigger": {"type": "interval"},
        },
        runtime,
    )
    assert not result.ok
    assert "every_seconds" in result.err
    store.close()


def test_durable_mutations_require_explicit_permission(tmp_path):
    store, runtime = _runtime(tmp_path)
    decision = schedule_task_tool.check_permission({}, runtime)
    assert decision.decision == "ask"
    assert "persistent_automation" in decision.risk_flags
    store.close()
