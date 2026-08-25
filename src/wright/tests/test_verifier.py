import json

from ..agent import Agent
from ..events import ContentDone
from ..renderer import SilentRenderer
from ..session import SessionState
from ..tools.base import ToolCall, ToolResult
from ..tools.plan_tools import update_plan_tool
from ..verifier import Verifier


def _final(answer):
    return json.dumps({"tool_calls": [], "final_answer": answer}, ensure_ascii=False)


def _tool(name, arguments):
    return json.dumps(
        {
            "tool_calls": [{"name": name, "arguments": arguments}],
            "final_answer": None,
        },
        ensure_ascii=False,
    )


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def __call__(self, messages):
        self.messages.append(list(messages))
        yield ContentDone(self.responses.pop(0))


def test_incomplete_plan_blocks_final_and_returns_to_agent_loop(tmp_path):
    session = SessionState.create("goal", tmp_path)
    session.plan_manager.create_plan("deliver", ["implement"])
    llm = ScriptLLM([
        _final("done too early"),
        _tool("update_plan", {"step_id": "step_1", "status": "completed"}),
        _final("really done"),
    ])
    agent = Agent(
        llm,
        [update_plan_tool],
        session,
        SilentRenderer(),
        verifier=Verifier(),
    )

    result = agent.run("finish it", max_steps=4)

    assert result == "really done"
    assert session.plan_manager.status == "completed"
    final_turns = [turn for turn in session.turns if turn.route == "final"]
    assert final_turns[0].verification.approved is False
    assert final_turns[0].verification.issues[0]["code"] == "plan_incomplete"
    assert final_turns[1].verification.approved is True
    assert any(
        "verification_feedback" in str(message.get("content", ""))
        for message in llm.messages[1]
    )


def test_semantic_reviewer_rejection_is_repairable(tmp_path):
    main_llm = ScriptLLM([_final("tests pass"), _final("tests pass with evidence")])
    reviewer = ScriptLLM([
        json.dumps({
            "approved": False,
            "issues": [{
                "code": "tests_unverified",
                "message": "没有成功测试命令证据",
            }],
        }, ensure_ascii=False),
        json.dumps({"approved": True, "issues": []}),
    ])
    session = SessionState.create("goal", tmp_path)
    agent = Agent(
        main_llm,
        [],
        session,
        SilentRenderer(),
        verifier=Verifier(reviewer),
    )

    result = agent.run("verify the project", max_steps=3)

    assert result == "tests pass with evidence"
    assert session.status == "completed"
    assert len(reviewer.messages) == 2
    assert session.turns[0].verification.approved is False
    assert session.turns[1].verification.approved is True


def test_reviewer_errors_fail_closed_with_bounded_retries(tmp_path):
    main_llm = ScriptLLM([_final("done"), _final("done again")])
    reviewer = ScriptLLM(["not-json", "still-not-json"])
    session = SessionState.create("goal", tmp_path)
    agent = Agent(
        main_llm,
        [],
        session,
        SilentRenderer(),
        verifier=Verifier(reviewer),
        max_verification_retries=2,
    )

    assert agent.run("task", max_steps=3) is None
    assert session.status == "failed"
    assert all(
        turn.verification.issues[0]["code"] == "verifier_error"
        for turn in session.turns
    )


def test_verifier_restats_successful_file_artifacts(tmp_path):
    session = SessionState.create("goal", tmp_path)
    session.begin_user_turn("write artifact")
    call = ToolCall(
        "write_file", {"file": "missing.txt", "content": "data"}, "c1"
    )
    session.record_assistant_turn(
        "tool call",
        {"tool_calls": []},
        "tool_calls",
        [call],
    )
    session.record_tool_execution("c1", ToolResult.success({"file": "missing.txt"}))

    result = Verifier().verify(session, "created missing.txt")

    assert result.approved is False
    assert result.issues[0].code == "artifact_missing"
