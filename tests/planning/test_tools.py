from tests.runtime_helpers import tool_runtime_for_session
from wright.domain.model.session import Session
from wright.infrastructure.tools.plan_tools import plan_tools, update_plan


def _runtime(tmp_path):
    session = Session.create("goal", tmp_path)
    return session, tool_runtime_for_session(session, workspace_dir=tmp_path)


def _plan_steps(runtime):
    return runtime.capabilities.plan_manager.snapshot()["steps"]


def _steps(*pairs):
    return [{"title": title, "status": status} for title, status in pairs]


def test_one_plan_tool_writes_the_whole_list(tmp_path):
    session, runtime = _runtime(tmp_path)
    assert [tool.name for tool in plan_tools] == ["update_plan"]

    created = update_plan(_steps(("inspect", "in_progress"), ("implement", "pending")), objective="goal", runtime=runtime)
    assert created.ok
    assert created.data == "Plan updated: 0/2 steps finished, step_1 in progress."

    moved = update_plan(_steps(("inspect", "completed"), ("implement", "in_progress")), runtime=runtime)
    assert moved.ok
    assert moved.data == "Plan updated: 1/2 steps finished, step_2 in progress."
    assert [step["id"] for step in session.plan_manager.snapshot()["steps"]] == ["step_1", "step_2"]


def test_plan_tool_acknowledges_without_echoing_the_plan(tmp_path):
    _, runtime = _runtime(tmp_path)
    update_plan(_steps(("a", "pending"), ("b", "pending")), objective="goal", runtime=runtime)

    blocked = update_plan(_steps(("a", "blocked"), ("b", "pending")), runtime=runtime)
    waiting = update_plan(_steps(("a", "skipped"), ("b", "pending")), runtime=runtime)
    done = update_plan(_steps(("a", "skipped"), ("b", "completed")), runtime=runtime)

    assert blocked.data == "Plan updated: 0/2 steps finished, step_1 blocked, step_2 next."
    assert waiting.data == "Plan updated: 1/2 steps finished, step_2 next."
    assert done.data == "Plan updated: all 2 steps finished."


def test_plan_tool_errors_are_data_not_exceptions(tmp_path):
    _, runtime = _runtime(tmp_path)

    missing_objective = update_plan(_steps(("a", "pending")), runtime=runtime)
    out_of_order = update_plan(_steps(("a", "pending"), ("b", "completed")), objective="goal", runtime=runtime)

    assert not missing_objective.ok and "objective" in missing_objective.err
    assert not out_of_order.ok and "Predecessor" in out_of_order.err


def test_changing_the_list_keeps_omitted_steps_and_finished_history(tmp_path):
    _, runtime = _runtime(tmp_path)
    update_plan(_steps(("first", "completed"), ("old second", "pending")), objective="goal", runtime=runtime)

    result = update_plan(_steps(("first", "completed"), ("new second", "in_progress")), runtime=runtime)

    assert result.ok
    assert [(step["title"], step["status"]) for step in _plan_steps(runtime)] == [
        ("first", "completed"), ("new second", "in_progress"), ("old second", "pending"),
    ]
    dropped = update_plan(_steps(("new second", "completed"), ("old second", "skipped")), runtime=runtime)
    assert dropped.ok and dropped.data == "Plan updated: all 3 steps finished."
    assert [(step["title"], step["status"]) for step in _plan_steps(runtime)] == [
        ("first", "completed"), ("new second", "completed"), ("old second", "skipped"),
    ]
    reopened = update_plan(_steps(("first", "pending")), runtime=runtime)
    assert not reopened.ok and "completed" in reopened.err


def test_plan_tool_schema_accepts_step_ids():
    schema = plan_tools[0].parameters["properties"]["steps"]["items"]

    assert "id" in schema["properties"]
    assert "default" not in schema["properties"]["status"]


def test_sessions_have_isolated_plans(tmp_path):
    first = Session.create("first", tmp_path)
    second = Session.create("second", tmp_path)

    first.plan_manager.apply_steps(_steps(("a", "pending")), objective="only first")

    assert first.plan_manager.has_plan
    assert not second.plan_manager.has_plan


def test_plan_tool_reports_a_replaced_plan(tmp_path):
    _, runtime = _runtime(tmp_path)
    update_plan(_steps(("a", "in_progress")), objective="old goal", runtime=runtime)

    replaced = update_plan(_steps(("x", "pending")), objective="new goal", runtime=runtime)

    assert replaced.data == "New plan started (the previous unfinished plan was replaced): 0/1 steps finished, step_1 next."
