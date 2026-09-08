from wright.tests.responses import event, response

from ..agent import Agent
from ..lifecycle import HookRegistration, LifecycleManager
from ..renderer import SilentRenderer
from ..session import SessionState
from ..tools.base import ToolCall, ToolResult
from ..tools.plan_tools import update_plan_tool
from ..verifier import Verifier


def _final(answer):
    return response(content=answer, calls=[])


def _tool(name, arguments):
    return response(content=None, calls=[{"name": name, "arguments": arguments}])


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def __call__(self, messages, **kwargs):
        self.messages.append(list(messages))
        yield event(self.responses.pop(0))


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


def test_structural_verifier_does_not_add_llm_turns_on_chat(tmp_path):
    llm = ScriptLLM([_final("hello")])
    session = SessionState.create("?", tmp_path)
    agent = Agent(llm, [], session, SilentRenderer(), verifier=Verifier())

    assert agent.run("?") == "hello"
    assert len(llm.messages) == 1
    assert session.turns[0].verification.approved is True


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


def test_verifier_and_stop_hook_retries_are_independent(tmp_path):
    session = SessionState.create("goal", tmp_path)
    session.plan_manager.create_plan("deliver", ["implement"])
    main_llm = ScriptLLM([
        _final("too early"),
        _tool("update_plan", {"step_id": "step_1", "status": "completed"}),
        _final("second"),
        _final("third"),
    ])

    def stop_hook(event):
        payload = event.payload
        if payload.get("status") != "completed":
            return None
        if payload.get("final_answer") == "second":
            return {"decision": "deny", "reason": "hook wants more"}
        return None

    lifecycle = LifecycleManager("session")
    lifecycle.register(HookRegistration(
        event="agent_stop", name="completion-gate", callback=stop_hook
    ))
    agent = Agent(
        main_llm,
        [update_plan_tool],
        session,
        SilentRenderer(),
        verifier=Verifier(),
        lifecycle=lifecycle,
        max_verification_retries=2,
    )

    result = agent.run("verify the project", max_steps=5)

    assert result == "third"
    assert session.status == "completed"
    finals = [turn for turn in session.turns if turn.route == "final"]
    assert finals[0].verification.approved is False
    assert finals[0].verification.issues[0]["code"] == "plan_incomplete"
    assert finals[1].verification.approved is True
    assert finals[2].verification.approved is True

