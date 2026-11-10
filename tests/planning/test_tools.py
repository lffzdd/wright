from tests.runtime_helpers import tool_runtime_for_session
from wright.domain.model.session import Session
from wright.infrastructure.tools.plan_tools import plan_tools, update_plan


def _runtime(tmp_path):
    session = Session.create("goal", tmp_path)
    return session, tool_runtime_for_session(session, workspace_dir=tmp_path)


def _steps(*pairs):
    return [{"title": title, "status": status} for title, status in pairs]


def test_one_plan_tool_writes_the_whole_list(tmp_path):
    session, runtime = _runtime(tmp_path)
    assert [tool.name for tool in plan_tools] == ["update_plan"]

    created = update_plan(_steps(("inspect", "in_progress"), ("implement", "pending")), objective="goal", runtime=runtime)
    assert created.ok
    assert [step["id"] for step in created.data["steps"]] == ["step_1", "step_2"]

    moved = update_plan(_steps(("inspect", "completed"), ("implement", "in_progress")), runtime=runtime)
    assert moved.ok
    assert [step["id"] for step in moved.data["steps"]] == ["step_1", "step_2"]
    assert moved.data == session.plan_manager.snapshot()


def test_plan_tool_errors_are_data_not_exceptions(tmp_path):
    _, runtime = _runtime(tmp_path)

    missing_objective = update_plan(_steps(("a", "pending")), runtime=runtime)
    out_of_order = update_plan(_steps(("a", "pending"), ("b", "completed")), objective="goal", runtime=runtime)

    assert not missing_objective.ok and "objective" in missing_objective.err
    assert not out_of_order.ok and "Predecessor" in out_of_order.err


def test_changing_the_list_keeps_finished_history(tmp_path):
    _, runtime = _runtime(tmp_path)
    update_plan(_steps(("first", "completed"), ("old second", "pending")), objective="goal", runtime=runtime)

    result = update_plan(_steps(("first", "completed"), ("new second", "in_progress")), runtime=runtime)

    assert result.ok
    assert [(step["title"], step["status"]) for step in result.data["steps"]] == [
        ("old second", "skipped"), ("first", "completed"), ("new second", "in_progress"),
    ]
    reopened = update_plan(_steps(("first", "pending")), runtime=runtime)
    assert not reopened.ok and "terminal" in reopened.err


def test_sessions_have_isolated_plans(tmp_path):
    first = Session.create("first", tmp_path)
    second = Session.create("second", tmp_path)

    first.plan_manager.create_plan("only first", ["a"])

    assert first.plan_manager.has_plan
    assert not second.plan_manager.has_plan
