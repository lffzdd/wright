import pytest

from ...coordination import (
    AgentControlConfig,
    AgentControlError,
    AgentControlPlane,
)


def _begin(
    plane,
    *,
    task="task",
    parent_id=None,
    depth=1,
    steps=10,
    turn="root:0",
):
    return plane.begin_task(
        root_turn_id=turn,
        parent_id=parent_id,
        tool_call_id="call",
        depth=depth,
        task=task,
        requested_steps=steps,
    )


def test_task_tree_tracks_parent_child_lifecycle_and_usage():
    plane = AgentControlPlane()
    parent = _begin(plane, task="parent")
    child = _begin(plane, task="child", parent_id=parent.id, depth=2)
    plane.bind_child_session(child.id, "child-session")
    plane.add_usage(child.id, 10, 5, 15)
    plane.finish_task(
        child.id, status="completed", steps_used=2, result="child result"
    )
    plane.finish_task(parent.id, status="completed", steps_used=3, result="done")

    tree = plane.tree("root:0")
    assert len(tree) == 1
    assert tree[0]["id"] == parent.id
    assert tree[0]["status"] == "completed"
    assert tree[0]["children"][0]["id"] == child.id
    assert tree[0]["children"][0]["child_session_id"] == "child-session"
    assert tree[0]["children"][0]["usage"]["total_tokens"] == 15


def test_step_budget_is_reserved_across_concurrent_siblings_and_unused_is_released():
    plane = AgentControlPlane(AgentControlConfig(max_steps_per_turn=3))
    first = _begin(plane, task="first", steps=2)
    second = _begin(plane, task="second", steps=2)
    assert first.step_budget == 2
    assert second.step_budget == 1
    with pytest.raises(AgentControlError, match="step"):
        _begin(plane, task="no-budget", steps=1)

    plane.finish_task(first.id, status="completed", steps_used=1)
    third = _begin(plane, task="released", steps=2)
    assert third.step_budget == 1


def test_token_budget_cancels_the_whole_turn_and_propagates_to_descendants():
    plane = AgentControlPlane(AgentControlConfig(max_tokens_per_turn=20))
    parent = _begin(plane, task="parent")
    child = _begin(plane, task="child", parent_id=parent.id, depth=2)

    plane.add_usage(child.id, 15, 6, 21)

    assert plane.is_cancelled(parent.id)
    assert plane.is_cancelled(child.id)
    assert "token" in plane.cancellation_reason(child.id)


def test_explicit_parent_cancel_marks_entire_subtree():
    plane = AgentControlPlane()
    parent = _begin(plane, task="parent")
    child = _begin(plane, task="child", parent_id=parent.id, depth=2)

    plane.request_cancel(parent.id, "user interrupted")

    assert plane.is_cancelled(parent.id)
    assert plane.is_cancelled(child.id)
    assert plane.cancellation_reason(child.id) == "user interrupted"


def test_concurrency_capacity_fails_fast_instead_of_deadlocking_nested_agents():
    plane = AgentControlPlane(AgentControlConfig(max_concurrent_tasks=1))
    first = _begin(plane, task="first")
    second = _begin(plane, task="second")

    assert first.status == "running"
    assert second.status == "failed"
    assert "并发" in second.error


def test_snapshot_restore_marks_live_tasks_interrupted_and_keeps_completed_tasks():
    plane = AgentControlPlane()
    live = _begin(plane, task="live")
    done = _begin(plane, task="done")
    plane.finish_task(done.id, status="completed", steps_used=1, result="ok")

    restored = AgentControlPlane.from_snapshot(
        plane.snapshot(), mark_interrupted=True
    )

    assert restored.get(live.id).status == "failed"
    assert "结果未知" in restored.get(live.id).error
    assert restored.get(done.id).status == "completed"
    assert restored.get(done.id).result == "ok"


def test_result_is_bounded_before_returning_to_parent_context():
    plane = AgentControlPlane(AgentControlConfig(max_result_chars=5))
    task = _begin(plane)
    finished = plane.finish_task(
        task.id, status="completed", steps_used=1, result="123456789"
    )
    assert finished.result == "12345"


def test_old_terminal_turn_trees_are_pruned_as_whole_groups():
    plane = AgentControlPlane(AgentControlConfig(max_stored_tasks=1))
    old = _begin(plane, turn="old", task="old")
    plane.finish_task(old.id, status="completed", steps_used=1, result="old")

    current = _begin(plane, turn="current", task="current")

    with pytest.raises(AgentControlError, match="未知"):
        plane.get(old.id)
    assert plane.get(current.id).status == "running"


def test_tree_summary_truncates_large_payloads():
    plane = AgentControlPlane()
    task = _begin(plane, task="x" * 1_000)
    plane.finish_task(
        task.id, status="completed", steps_used=1, result="r" * 2_000
    )
    summary = plane.tree_summary("root:0")[0]
    assert len(summary["task"]) == 300
    assert len(summary["result"]) == 500
