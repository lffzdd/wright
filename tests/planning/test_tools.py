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
    assert created.data == "Plan updated: 0/2 steps done, step_1 in progress."

    moved = update_plan(_steps(("inspect", "completed"), ("implement", "in_progress")), runtime=runtime)
    assert moved.ok
    assert moved.data == "Plan updated: 1/2 steps done, step_2 in progress."
    assert [step["id"] for step in session.plan_manager.snapshot()["steps"]] == ["step_1", "step_2"]


def test_plan_tool_acknowledges_without_echoing_the_plan(tmp_path):
    _, runtime = _runtime(tmp_path)
    update_plan(_steps(("a", "pending"), ("b", "pending")), objective="goal", runtime=runtime)

    blocked = update_plan(_steps(("a", "blocked"), ("b", "pending")), runtime=runtime)
    waiting = update_plan(_steps(("a", "skipped"), ("b", "pending")), runtime=runtime)
    done = update_plan(_steps(("a", "skipped"), ("b", "completed")), runtime=runtime)

    assert blocked.data == "Plan updated: 0/2 steps done, step_1 blocked, step_2 next."
    assert waiting.data == "Plan updated: 0/2 steps done, 1 skipped, step_2 next."
    assert done.data == "Plan updated: plan finished, 1/2 steps done, 1 skipped."


def test_plan_tool_errors_are_data_not_exceptions(tmp_path):
    _, runtime = _runtime(tmp_path)

    missing_objective = update_plan(_steps(("a", "pending")), runtime=runtime)
    two_active = update_plan(_steps(("a", "in_progress"), ("b", "in_progress")), objective="goal", runtime=runtime)

    assert not missing_objective.ok and "objective" in missing_objective.err
    assert not two_active.ok and "in progress" in two_active.err


def test_changing_the_list_keeps_omitted_steps_and_finished_history(tmp_path):
    _, runtime = _runtime(tmp_path)
    update_plan(_steps(("first", "completed"), ("old second", "pending")), objective="goal", runtime=runtime)

    result = update_plan(_steps(("first", "completed"), ("new second", "in_progress")), runtime=runtime)

    assert result.ok
    assert [(step["title"], step["status"]) for step in _plan_steps(runtime)] == [
        ("first", "completed"), ("new second", "in_progress"), ("old second", "pending"),
    ]
    dropped = update_plan(_steps(("new second", "completed"), ("old second", "skipped")), runtime=runtime)
    assert dropped.ok and dropped.data == "Plan updated: plan finished, 2/3 steps done, 1 skipped."
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

    assert replaced.data == "New plan started (the previous unfinished plan was replaced): 0/1 steps done, step_1 next."


def test_acknowledgement_counts_skipped_steps_apart_from_done(tmp_path):
    _, runtime = _runtime(tmp_path)
    update_plan(_steps(("a", "pending"), ("b", "pending"), ("c", "pending"), ("d", "pending")), objective="goal", runtime=runtime)

    partial = update_plan(_steps(("a", "blocked"), ("b", "completed"), ("c", "skipped")), runtime=runtime)
    closed = update_plan(_steps(("a", "skipped"), ("d", "completed")), runtime=runtime)

    assert partial.data == "Plan updated: 1/4 steps done, 1 skipped, step_1 blocked, step_4 next."
    assert closed.data == "Plan updated: plan finished, 2/4 steps done, 2 skipped."


def test_busy_error_names_only_real_statuses(tmp_path):
    _, runtime = _runtime(tmp_path)

    busy = update_plan(_steps(("a", "in_progress"), ("b", "in_progress")), objective="goal", runtime=runtime)

    assert not busy.ok and "pause" not in busy.err and "skip" in busy.err


def test_plan_tool_attaches_the_full_plan_for_display_only(tmp_path):
    from wright.domain.model.tool import ToolCall
    from wright.domain.protocol import build_tool_results_messages

    _, runtime = _runtime(tmp_path)
    result = update_plan(_steps(("a", "completed"), ("b", "in_progress")), objective="goal", runtime=runtime)

    assert result.display["objective"] == "goal"
    assert [(s["id"], s["title"], s["status"]) for s in result.display["steps"]] == [
        ("step_1", "a", "completed"), ("step_2", "b", "in_progress"),
    ]
    assert result.to_dict()["display"] == result.display
    assert "display" not in result.to_model_dict()
    message = build_tool_results_messages([(ToolCall("update_plan", {}, "c1"), result)])[0]
    assert "display" not in message["content"] and "step_2" not in message["content"].replace("step_2 in progress", "")


def test_rejected_update_shows_the_unchanged_plan_and_why(tmp_path):
    _, runtime = _runtime(tmp_path)
    update_plan(_steps(("a", "completed"), ("b", "in_progress"), ("c", "pending")), objective="goal", runtime=runtime)

    rejected = update_plan([{"id": "step_3", "title": "c", "status": "in_progress"}], runtime=runtime)

    assert not rejected.ok and "step_2 is in progress" in rejected.err
    assert [(s["id"], s["status"]) for s in rejected.display["steps"]] == [
        ("step_1", "completed"), ("step_2", "in_progress"), ("step_3", "pending"),
    ]
    assert rejected.display["rejected"] == {
        "code": "plan.step_busy",
        "params": {"active_id": "step_2", "step_id": "step_3"},
    }
    assert "display" not in rejected.to_model_dict()


def test_plan_failures_outside_the_rules_carry_no_display():
    failed = update_plan(_steps(("a", "pending")), objective="goal", runtime=None)

    assert not failed.ok and failed.display is None
