from ...tools import tools as base_tools
from ...tools.ask_user_tool import ask_user, ask_user_tool
from ...tools.base import ToolRuntime


def _runtime(handler=None):
    return ToolRuntime(
        tool_name="ask_user",
        tool_call_id="call_1",
    )


def test_ask_user_serializes_interaction_answer():
    runtime = _runtime()

    result = ask_user(
        "选择数据库？",
        context="数据规模未知",
        options=["SQLite", "PostgreSQL"],
        answer="蓝色",
        runtime=runtime,
    )

    assert result.ok
    assert result.data["question"] == "选择数据库？"
    assert result.data["answer"] == "蓝色"


def test_ask_user_fails_without_interaction_answer():
    runtime = _runtime()

    result = ask_user("question", runtime=runtime)

    assert not result.ok
    assert "answer" in result.err


def test_ask_user_validation_failure():
    runtime = _runtime()

    result = ask_user(
        "question", options=["same", "same"], answer="answer", runtime=runtime
    )

    assert not result.ok
    assert "重复" in result.err


def test_ask_user_is_not_a_child_base_tool():
    assert "ask_user" not in {tool.name for tool in base_tools}
