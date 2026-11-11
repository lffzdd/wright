import pytest

from wright.application.planning.prompt import plan_prompt_block
from wright.domain.model.planning import PlanError, PlanManager


def _pending(*titles):
    return [{"title": title, "status": "pending"} for title in titles]


def test_new_plan_builds_stable_ordered_steps():
    plan = PlanManager().apply_steps(_pending("检查边界", "实现", "测试"), objective="实现计划能力")

    assert plan["objective"] == "实现计划能力"
    assert plan["status"] == "pending"
    assert plan["revision"] == 1
    assert [step["id"] for step in plan["steps"]] == ["step_1", "step_2", "step_3"]
    assert all(step["status"] == "pending" for step in plan["steps"])


@pytest.mark.parametrize(
    ("objective", "steps", "error"),
    [
        ("", _pending("a"), "objective"),
        (None, _pending("a"), "objective is required"),
        ("goal", [], "steps cannot be empty"),
        ("goal", _pending("  "), "steps"),
        ("goal", "not-a-list", "array of objects"),
        ("goal", ["a"], "array of objects"),
        ("goal", [{"title": "a", "status": "done"}], "Invalid step status"),
    ],
)
def test_new_plan_rejects_invalid_input(objective, steps, error):
    with pytest.raises(PlanError, match=error):
        PlanManager().apply_steps(steps, objective=objective)


def test_only_one_step_can_be_in_progress():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "in_progress"), ("b", "pending")), objective="goal")

    with pytest.raises(PlanError, match="a is in progress") as error:
        manager.apply_steps(_s(("a", "in_progress"), ("b", "in_progress")))
    assert error.value.code == "plan.step_busy"

    plan = manager.apply_steps(_s(("a", "completed"), ("b", "in_progress")))
    assert plan["status"] == "in_progress"


def test_steps_cannot_start_or_finish_before_open_predecessors():
    manager = PlanManager()
    manager.apply_steps(_pending("first", "second"), objective="goal")

    with pytest.raises(PlanError, match="Predecessor step_1"):
        manager.apply_steps(_s(("first", "pending"), ("second", "in_progress")))
    with pytest.raises(PlanError, match="Predecessor step_1"):
        manager.apply_steps(_s(("first", "pending"), ("second", "completed")))

    plan = manager.apply_steps(_s(("first", "skipped"), ("second", "in_progress")))
    assert plan["status"] == "in_progress"


def test_blocked_plan_can_resume():
    manager = PlanManager()
    manager.apply_steps(_pending("a"), objective="goal")

    blocked = manager.apply_steps([{"title": "a", "status": "blocked", "note": "waiting"}])
    assert blocked["status"] == "blocked"

    resumed = manager.apply_steps([{"title": "a", "status": "in_progress", "note": "unblocked"}])
    assert resumed["status"] == "in_progress"
    assert resumed["steps"][0]["note"] == "unblocked"


def test_snapshot_is_detached_from_internal_state():
    manager = PlanManager()
    snapshot = manager.apply_steps(_pending("a"), objective="goal")

    snapshot["steps"][0]["title"] = "mutated outside"

    assert manager.snapshot()["steps"][0]["title"] == "a"


def test_restore_preserves_plan_history_and_next_step_id():
    manager = PlanManager()
    manager.apply_steps(_s(("old one", "completed"), ("old two", "skipped"), ("current", "pending")), objective="goal")
    snapshot = manager.snapshot()

    restored = PlanManager.from_snapshot(snapshot)
    extended = restored.apply_steps(_s(("current", "completed"), ("next", "pending")))

    assert restored.revision == snapshot["revision"] + 1
    assert extended["steps"][-1]["id"] == "step_4"


def test_restore_rejects_inconsistent_derived_status():
    snapshot = PlanManager().apply_steps(_pending("pending"), objective="goal")
    snapshot["status"] = "completed"

    with pytest.raises(PlanError, match="derived status"):
        PlanManager.from_snapshot(snapshot)


def test_prompt_block_is_empty_and_serializes_plan_as_data():
    manager = PlanManager()

    assert plan_prompt_block(manager) == ""

    manager.apply_steps(
        [{"title": "code", "status": "in_progress", "note": "working"}, {"title": "test", "status": "pending"}],
        objective="ship",
    )
    block = plan_prompt_block(manager)

    assert block.startswith("<system-reminder>")
    assert "<plan-state>" in block
    assert '"objective": "ship"' in block
    assert '"id": "step_1"' in block
    assert '"status": "in_progress"' in block
    assert '"note": "working"' in block
    assert block.endswith("</system-reminder>")
    # Display-only fields and empty notes stay out of the model's context.
    for noise in ("started_at", "ended_at", "revision", '"note": ""'):
        assert noise not in block


@pytest.mark.parametrize(
    ("objective", "steps", "error"),
    [
        ("x" * 241, _pending("a"), "objective cannot exceed"),
        ("goal", _pending("x" * 161), r"steps\[0\].title cannot exceed"),
        ("goal", _pending(*[str(index) for index in range(13)]), "steps cannot exceed"),
        ("goal", [{"title": "a", "status": "pending", "note": "x" * 241}], "note cannot exceed"),
    ],
)
def test_plan_size_is_bounded(objective, steps, error):
    with pytest.raises(PlanError, match=error):
        PlanManager().apply_steps(steps, objective=objective)


def test_apply_steps_matches_titles_and_is_atomic():
    manager = PlanManager()
    first = manager.apply_steps(
        [{"title": "a", "status": "in_progress"}, {"title": "b", "status": "pending"}], objective="goal"
    )
    started_at = first["steps"][0]["started_at"]
    assert first["revision"] == 1

    unchanged = manager.apply_steps([{"title": "a", "status": "in_progress"}, {"title": "b", "status": "pending"}])
    assert unchanged["revision"] == 1

    with pytest.raises(PlanError):
        manager.apply_steps([{"title": "a", "status": "in_progress"}, {"title": "b", "status": "in_progress"}])
    with pytest.raises(PlanError):
        manager.apply_steps([{"title": "a", "status": "pending"}, {"title": "b", "status": "completed"}])
    assert manager.snapshot() == unchanged

    done = manager.apply_steps([{"title": "a", "status": "completed"}, {"title": "b", "status": "completed"}])
    assert done["status"] == "completed"
    assert done["steps"][0]["started_at"] == started_at
    assert done["steps"][0]["ended_at"] is not None


def test_new_steps_after_a_completed_plan_follow_its_history():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "completed"), ("b", "completed")), objective="goal")

    plan = manager.apply_steps(_s(("c", "in_progress")))

    assert [(step["id"], step["status"]) for step in plan["steps"]] == [
        ("step_1", "completed"), ("step_2", "completed"), ("step_3", "in_progress"),
    ]


def test_plan_history_is_bounded():
    manager = PlanManager()
    manager.apply_steps(_s(*[(str(index), "completed") for index in range(12)]), objective="goal")
    manager.apply_steps(_pending(*[f"x{index}" for index in range(12)]))

    with pytest.raises(PlanError, match="cannot exceed 24") as error:
        manager.apply_steps(_pending("one more"))
    assert error.value.code == "plan.too_long"


def _s(*pairs):
    return [{"title": title, "status": status} for title, status in pairs]


def _rows(plan):
    return [(step["id"], step["title"], step["status"]) for step in plan["steps"]]


def test_apply_steps_keeps_omitted_open_steps_instead_of_skipping_them():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "in_progress"), ("b", "pending"), ("c", "pending")), objective="goal")

    plan = manager.apply_steps(_s(("a", "completed")))

    assert _rows(plan) == [
        ("step_1", "a", "completed"), ("step_2", "b", "pending"), ("step_3", "c", "pending"),
    ]
    assert plan["status"] == "pending"


def test_apply_steps_keeps_history_in_original_order():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "completed"), ("b", "pending"), ("c", "pending")), objective="goal")

    plan = manager.apply_steps(_s(("a", "completed"), ("b", "skipped"), ("d", "in_progress")))
    assert _rows(plan) == [
        ("step_1", "a", "completed"), ("step_2", "b", "skipped"),
        ("step_4", "d", "in_progress"), ("step_3", "c", "pending"),
    ]

    # Only the remaining route is sent: finished history stays in front.
    plan = manager.apply_steps(_s(("d", "completed"), ("c", "in_progress")))
    assert _rows(plan) == [
        ("step_1", "a", "completed"), ("step_2", "b", "skipped"),
        ("step_4", "d", "completed"), ("step_3", "c", "in_progress"),
    ]


def test_skipping_an_open_step_by_omission_is_rejected():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "pending"), ("b", "pending")), objective="goal")

    with pytest.raises(PlanError, match="Predecessor step_1") as error:
        manager.apply_steps(_s(("b", "in_progress")))
    assert error.value.code == "plan.predecessor"


def test_completed_plan_accepts_repeats_and_appended_steps_without_objective():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "in_progress")), objective="goal")
    done = manager.apply_steps(_s(("a", "completed")))

    repeated = manager.apply_steps(_s(("a", "completed")))
    assert repeated == done

    same_objective = manager.apply_steps(_s(("a", "completed")), objective="goal")
    assert same_objective == done

    extended = manager.apply_steps(_s(("a", "completed"), ("b", "in_progress")))
    assert extended["objective"] == "goal"
    assert _rows(extended) == [("step_1", "a", "completed"), ("step_2", "b", "in_progress")]
    assert extended["steps"][0]["started_at"] == done["steps"][0]["started_at"]


def test_completed_plan_with_a_new_objective_starts_over():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "completed")), objective="goal")

    fresh = manager.apply_steps(_s(("x", "pending")), objective="next goal")

    assert fresh["objective"] == "next goal"
    assert _rows(fresh) == [("step_1", "x", "pending")]


def test_steps_after_a_blocked_step_can_proceed():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "blocked"), ("b", "pending")), objective="goal")

    plan = manager.apply_steps(_s(("a", "blocked"), ("b", "in_progress")))
    assert plan["status"] == "in_progress"

    plan = manager.apply_steps(_s(("a", "blocked"), ("b", "completed")))
    assert plan["status"] == "blocked"


def test_step_id_matches_a_renamed_step():
    manager = PlanManager()
    manager.apply_steps(_s(("read code", "completed"), ("fix", "pending")), objective="goal")

    plan = manager.apply_steps([
        {"id": "step_1", "title": "read the code", "status": "completed"},
        {"title": "fix", "status": "in_progress"},
    ])

    assert _rows(plan) == [("step_1", "read the code", "completed"), ("step_2", "fix", "in_progress")]
    with pytest.raises(PlanError, match="Unknown step id"):
        manager.apply_steps([{"id": "step_9", "title": "x", "status": "pending"}])
    with pytest.raises(PlanError, match="more than once"):
        manager.apply_steps([
            {"id": "step_2", "title": "fix", "status": "in_progress"},
            {"id": "step_2", "title": "fix again", "status": "pending"},
        ])


def test_skipped_steps_can_reopen_but_completed_steps_cannot():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "completed"), ("b", "skipped")), objective="goal")

    reopened = manager.apply_steps(_s(("a", "completed"), ("b", "in_progress")))
    assert reopened["steps"][1]["status"] == "in_progress"
    assert reopened["steps"][1]["ended_at"] is None
    assert PlanManager.from_snapshot(reopened) is not None

    with pytest.raises(PlanError, match="already terminal"):
        manager.apply_steps(_s(("a", "pending"), ("b", "in_progress")))


def test_reset_keeps_revision_increasing():
    manager = PlanManager()
    first = manager.apply_steps(_s(("a", "pending")), objective="goal")

    manager.reset()
    second = manager.apply_steps(_s(("b", "pending")), objective="next")

    assert second["revision"] > first["revision"]


def test_new_turn_clears_a_finished_plan():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "completed")), objective="goal")

    manager.begin_turn()

    assert not manager.has_plan
    assert not manager.carried


def test_new_turn_carries_an_unfinished_plan_until_it_is_updated():
    manager = PlanManager()
    before = manager.apply_steps(_s(("a", "completed"), ("b", "in_progress"), ("c", "pending")), objective="goal")

    manager.begin_turn()
    carried = manager.snapshot()
    assert manager.carried and carried["carried"] is True
    assert _rows(carried) == _rows(before)
    assert carried["revision"] > before["revision"]

    # Restoring a checkpoint taken mid-turn keeps the flag.
    assert PlanManager.from_snapshot(carried).carried

    resumed = manager.apply_steps(_s(("b", "in_progress")))
    assert not manager.carried and resumed["carried"] is False
    assert resumed["revision"] > carried["revision"]


def test_a_new_objective_with_only_new_steps_replaces_an_open_plan():
    manager = PlanManager()
    old = manager.apply_steps(_s(("a", "completed"), ("b", "in_progress")), objective="old goal")

    replaced = manager.apply_steps(_s(("x", "in_progress")), objective="new goal")

    assert replaced["objective"] == "new goal"
    assert _rows(replaced) == [("step_1", "x", "in_progress")]
    assert replaced["generation"] == old["generation"] + 1


def test_a_new_objective_with_matching_steps_only_renames_the_goal():
    manager = PlanManager()
    old = manager.apply_steps(_s(("a", "completed"), ("b", "in_progress")), objective="old goal")

    renamed = manager.apply_steps(_s(("b", "completed")), objective="sharper goal")

    assert renamed["objective"] == "sharper goal"
    assert _rows(renamed) == [("step_1", "a", "completed"), ("step_2", "b", "completed")]
    assert renamed["generation"] == old["generation"]


def test_prompt_block_marks_a_carried_plan():
    manager = PlanManager()
    manager.apply_steps(_s(("a", "in_progress")), objective="goal")
    assert "earlier turn" not in plan_prompt_block(manager)

    manager.begin_turn()

    assert "earlier turn" in plan_prompt_block(manager)
    assert "carried" not in plan_prompt_block(manager).split("<plan-state>")[1]
