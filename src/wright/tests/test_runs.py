from pathlib import Path

from wright.session import Session, UsageRecord
from wright.tools.base import ToolCall, ToolResult


def test_runs_keep_goals_steps_tools_and_usage_separate(tmp_path: Path):
    session = Session.create("initial", tmp_path)
    session.begin_user_turn("A")
    run_a = session.active_run()
    assert run_a is not None
    turn_a = session.record_assistant_turn(
        "", {"tool_calls": []}, "tool_calls", [ToolCall("read_file", {"path": "a"}, "call-a")]
    )
    session.record_tool_execution("call-a", ToolResult.success("ok"))
    session.record_usage_for_turn(turn_a, UsageRecord(2, 3, 5))
    session.mark_completed()

    session.begin_user_turn("B")
    run_b = session.active_run()
    assert run_b is not None and run_b.run_id != run_a.run_id
    assert run_a.goal == "A" and run_a.status == "completed"
    assert run_a.usage["total_tokens"] == 5
    assert session.tool_executions["call-a"].run_id == run_a.run_id
    assert turn_a.run_id == run_a.run_id and turn_a.step_id in run_a.step_ids
    assert run_b.goal == "B" and run_b.status == "running"
