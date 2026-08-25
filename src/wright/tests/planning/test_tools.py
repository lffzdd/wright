from ...session import SessionState
from ...tools.base import ToolRuntime
from ...tools.plan_tools import create_plan, get_plan, replan, update_plan


def _runtime(tmp_path):
    session = SessionState.create("goal", tmp_path)
    return session, ToolRuntime(
        workspace_dir=tmp_path,
        session_state=session,
    )


def test_plan_tools_share_session_manager(tmp_path):
    session, runtime = _runtime(tmp_path)

    created = create_plan("goal", ["inspect", "implement"], runtime=runtime)
    assert created.ok
    assert session.plan_manager.has_plan

    started = update_plan("step_1", "in_progress", runtime=runtime)
    assert started.ok
    assert started.data["steps"][0]["status"] == "in_progress"

    current = get_plan(runtime=runtime)
    assert current.ok
    assert current.data == session.plan_manager.snapshot()


def test_plan_tool_errors_are_data_not_exceptions(tmp_path):
    _, runtime = _runtime(tmp_path)

    result = update_plan("step_404", "completed", runtime=runtime)

    assert not result.ok
    assert "未知步骤 id" in result.err


def test_replan_tool_keeps_completed_history(tmp_path):
    _, runtime = _runtime(tmp_path)
    create_plan("goal", ["first", "old second"], runtime=runtime)
    update_plan("step_1", "completed", runtime=runtime)

    result = replan(["new second"], "requirements changed", runtime=runtime)

    assert result.ok
    assert result.data["steps"][0]["status"] == "completed"
    assert result.data["steps"][1]["status"] == "skipped"
    assert result.data["steps"][2]["title"] == "new second"


def test_sessions_have_isolated_plans(tmp_path):
    first = SessionState.create("first", tmp_path)
    second = SessionState.create("second", tmp_path)

    first.plan_manager.create_plan("only first", ["a"])

    assert first.plan_manager.has_plan
    assert not second.plan_manager.has_plan
