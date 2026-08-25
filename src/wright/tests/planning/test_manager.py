import pytest

from ...planning import PlanError, PlanManager


def test_create_plan_builds_stable_ordered_steps():
    manager = PlanManager()

    plan = manager.create_plan("实现计划能力", ["检查边界", "实现", "测试"])

    assert plan["objective"] == "实现计划能力"
    assert plan["status"] == "pending"
    assert plan["revision"] == 1
    assert [step["id"] for step in plan["steps"]] == [
        "step_1",
        "step_2",
        "step_3",
    ]
    assert all(step["status"] == "pending" for step in plan["steps"])


@pytest.mark.parametrize(
    ("objective", "steps", "error"),
    [
        ("", ["a"], "objective"),
        ("goal", [], "steps 不能为空"),
        ("goal", ["  "], "steps"),
        ("goal", "not-a-list", "字符串数组"),
    ],
)
def test_create_plan_rejects_invalid_input(objective, steps, error):
    with pytest.raises(PlanError, match=error):
        PlanManager().create_plan(objective, steps)


def test_active_plan_requires_explicit_replace():
    manager = PlanManager()
    manager.create_plan("first", ["a"])

    with pytest.raises(PlanError, match="已有未完成计划"):
        manager.create_plan("second", ["b"])

    replaced = manager.create_plan("second", ["b"], replace=True)
    assert replaced["objective"] == "second"
    assert [step["title"] for step in replaced["steps"]] == ["b"]


def test_only_one_step_can_be_in_progress():
    manager = PlanManager()
    manager.create_plan("goal", ["a", "b"])
    manager.update_step("step_1", "in_progress")

    with pytest.raises(PlanError, match="step_1 正在进行"):
        manager.update_step("step_2", "in_progress")

    manager.update_step("step_1", "completed")
    plan = manager.update_step("step_2", "in_progress")
    assert plan["status"] == "in_progress"


def test_steps_cannot_start_or_finish_before_open_predecessors():
    manager = PlanManager()
    manager.create_plan("goal", ["first", "second"])

    with pytest.raises(PlanError, match="前置步骤 step_1"):
        manager.update_step("step_2", "in_progress")
    with pytest.raises(PlanError, match="前置步骤 step_1"):
        manager.update_step("step_2", "completed")

    manager.update_step("step_1", "skipped")
    assert manager.update_step("step_2", "in_progress")["status"] == "in_progress"


def test_completed_and_skipped_steps_are_terminal():
    manager = PlanManager()
    manager.create_plan("goal", ["a", "b"])
    manager.update_step("step_1", "completed", note="done")
    plan = manager.update_step("step_2", "skipped")

    assert plan["status"] == "completed"
    with pytest.raises(PlanError, match="已是终态 completed"):
        manager.update_step("step_1", "pending")


def test_blocked_plan_can_resume():
    manager = PlanManager()
    manager.create_plan("goal", ["a"])

    blocked = manager.update_step("step_1", "blocked", note="waiting")
    assert blocked["status"] == "blocked"

    resumed = manager.update_step("step_1", "in_progress", note="unblocked")
    assert resumed["status"] == "in_progress"
    assert resumed["steps"][0]["note"] == "unblocked"


def test_replan_preserves_history_and_replaces_open_route():
    manager = PlanManager()
    manager.create_plan("goal", ["done work", "old active", "old pending"])
    manager.update_step("step_1", "completed")
    manager.update_step("step_2", "in_progress", note="partial")

    plan = manager.replan(["new route", "verify"], reason="new constraint")

    assert [step["id"] for step in plan["steps"]] == [
        "step_1",
        "step_2",
        "step_3",
        "step_4",
        "step_5",
    ]
    assert plan["steps"][0]["status"] == "completed"
    assert plan["steps"][1]["status"] == "skipped"
    assert plan["steps"][2]["status"] == "skipped"
    assert plan["steps"][1]["note"] == "partial; Replanned: new constraint"
    assert [step["title"] for step in plan["steps"][-2:]] == [
        "new route",
        "verify",
    ]
    assert plan["status"] == "pending"


def test_snapshot_is_detached_from_internal_state():
    manager = PlanManager()
    snapshot = manager.create_plan("goal", ["a"])

    snapshot["steps"][0]["title"] = "mutated outside"

    assert manager.snapshot()["steps"][0]["title"] == "a"


def test_restore_preserves_plan_history_and_next_step_id():
    manager = PlanManager()
    manager.create_plan("goal", ["old one", "old two"])
    manager.update_step("step_1", "completed")
    snapshot = manager.replan(["current"], reason="changed")

    restored = PlanManager.from_snapshot(snapshot)
    replanned = restored.replan(["next"], reason="changed again")

    assert restored.revision == snapshot["revision"] + 1
    assert replanned["steps"][-1]["id"] == "step_4"


def test_restore_rejects_inconsistent_derived_status():
    manager = PlanManager()
    snapshot = manager.create_plan("goal", ["pending"])
    snapshot["status"] = "completed"

    with pytest.raises(PlanError, match="派生状态"):
        PlanManager.from_snapshot(snapshot)


def test_prompt_block_is_empty_and_serializes_plan_as_data():
    manager = PlanManager()
    assert manager.to_prompt_block() == ""

    manager.create_plan("ship", ["code", "test"])
    manager.update_step("step_1", "in_progress", note="working")
    block = manager.to_prompt_block()

    assert block.startswith("<system-reminder>")
    assert "<plan-state>" in block
    assert '"objective": "ship"' in block
    assert '"id": "step_1"' in block
    assert '"status": "in_progress"' in block
    assert block.endswith("</system-reminder>")


@pytest.mark.parametrize(
    ("objective", "steps", "error"),
    [
        ("x" * 241, ["a"], "objective 不能超过"),
        ("goal", ["x" * 161], r"steps\[0\] 不能超过"),
        ("goal", [str(index) for index in range(13)], "steps 不能超过"),
    ],
)
def test_plan_size_is_bounded(objective, steps, error):
    with pytest.raises(PlanError, match=error):
        PlanManager().create_plan(objective, steps)


def test_note_size_is_bounded():
    manager = PlanManager()
    manager.create_plan("goal", ["a"])

    with pytest.raises(PlanError, match="note 不能超过"):
        manager.update_step("step_1", "in_progress", note="x" * 241)


def test_replan_has_a_bounded_total_history():
    manager = PlanManager()
    manager.create_plan("goal", [str(index) for index in range(12)])

    manager.replan([str(index) for index in range(12)], reason="new route")

    with pytest.raises(PlanError, match="合计不能超过"):
        manager.replan(["one more"], reason="another route")
