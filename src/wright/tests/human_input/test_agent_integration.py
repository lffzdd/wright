

from wright.tests.responses import event, response

from ...agent import Agent
from ...permission import PermissionCheckResult, PermissionResolver
from ...renderer import SilentRenderer
from ...session import SessionState
from ...tools.ask_user_tool import ask_user_tool


def _tool_turn(name, **arguments):
    return response(content=None, calls=[{"name": name, "arguments": arguments}])


def _final(answer):
    return response(content=answer, calls=[])


def _interaction_handler(answer, calls=None):
    """模拟 Claude Code 的交互层：回填参数后才允许 tool.call() 执行。"""
    def handler(request):
        if calls is not None:
            calls.append(request)
        return PermissionCheckResult(
            "allow",
            "test user answered",
            updated_arguments={**request.arguments, "answer": answer},
            source="test_interaction",
        )
    return handler


class AskThenAnswerLLM:
    """第一轮调 ask_user，第二轮给 final_answer。"""
    context_limit = 128_000

    def __init__(self):
        self.calls = 0
        self.seen_messages = []

    def __call__(self, messages, **kwargs):
        self.seen_messages.append(list(messages))
        self.calls += 1
        if self.calls == 1:
            content = _tool_turn(
                "ask_user",
                question="你更喜欢哪种颜色？",
                context="这会决定主题色",
                options=["蓝色", "绿色"],
            )
        else:
            content = _final("已选择蓝色主题")
        yield event(content=content)


def test_ask_user_runs_through_without_pausing(tmp_path):
    """核心集成测试：ask_user 在工具执行期间同步获取用户输入，
    主循环不中断、不退出、不需要 resume。一次 run() 调用跑完全程。"""
    llm = AskThenAnswerLLM()
    session = SessionState.create("theme", tmp_path)

    interaction_calls = []

    agent = Agent(
        llm,
        [ask_user_tool],
        session,
        SilentRenderer(),
        permission_resolver=PermissionResolver(
            interaction_handler=_interaction_handler("蓝色", interaction_calls)
        ),
    )

    result = agent.run("帮我设置主题", max_steps=3)

    # 一次 run() 就拿到了最终答案，不再返回 None
    assert result == "已选择蓝色主题"
    assert session.status == "completed"

    # 交互层被正确调用，且它拿到的是完整工具请求。
    assert len(interaction_calls) == 1
    assert interaction_calls[0].arguments == {
        "question": "你更喜欢哪种颜色？",
        "context": "这会决定主题色",
        "options": ["蓝色", "绿色"],
    }

    # LLM 被调用了两轮（ask_user + final）
    assert llm.calls == 2

    # ask_user 的回答作为 tool_result 出现在第二轮的 messages 里
    second_call_messages = llm.seen_messages[1]
    # 找到包含 ask_user 结果的 tool message（tool results）
    tool_result_msgs = [
        m for m in second_call_messages
        if m.get("role") == "tool" and isinstance(m.get("content"), str)
        and "蓝色" in m["content"]
    ]
    assert len(tool_result_msgs) >= 1


def test_ask_user_can_be_called_multiple_times(tmp_path):
    """ask_user 可以在同一次 run() 中被调用多次。"""

    class TwiceLLM:
        context_limit = 128_000

        def __init__(self):
            self.calls = 0

        def __call__(self, messages, **kwargs):
            self.calls += 1
            script = [
                _tool_turn("ask_user", question="first?"),
                _tool_turn("ask_user", question="second?"),
                _final("done"),
            ]
            yield event(content=script[self.calls - 1])

    answers = iter(["one", "two"])
    def interaction_handler(request):
        return PermissionCheckResult(
            "allow",
            "test user answered",
            updated_arguments={**request.arguments, "answer": next(answers)},
            source="test_interaction",
        )

    session = SessionState.create("twice", tmp_path)
    agent = Agent(
        TwiceLLM(),
        [ask_user_tool],
        session,
        SilentRenderer(),
        permission_resolver=PermissionResolver(interaction_handler=interaction_handler),
    )

    result = agent.run("start", max_steps=5)

    assert result == "done"
    assert session.status == "completed"


def test_ask_user_without_interaction_handler_returns_error(tmp_path):
    """没有交互层时 ask_user 被 fail-closed，Agent 仍能正常处理。"""

    class AskThenFinalLLM:
        context_limit = 128_000

        def __init__(self):
            self.calls = 0

        def __call__(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield event(content=_tool_turn("ask_user", question="q?"))
            else:
                yield event(content=_final("recovered"))

    session = SessionState.create("no-handler", tmp_path)
    agent = Agent(
        AskThenFinalLLM(),
        [ask_user_tool],
        session,
        SilentRenderer(),
        # 不传 interaction_handler
    )

    result = agent.run("start", max_steps=3)
    assert result == "recovered"


def test_ask_user_cannot_be_auto_approved_by_normal_permission_handler(tmp_path):
    """requires_user_interaction 必须压过普通 allow 规则，避免答案由策略伪造。"""

    session = SessionState.create("no-auto-answer", tmp_path)
    agent = Agent(
        AskThenAnswerLLM(),
        [ask_user_tool],
        session,
        SilentRenderer(),
        permission_resolver=PermissionResolver(
            approval_handler=lambda request: PermissionCheckResult(
                "allow",
                "this must not be used for ask_user",
                updated_arguments={**request.arguments, "answer": "forged"},
                source="test_approval",
            )
        ),
    )

    result = agent.run("start", max_steps=3)

    # 第一轮的 ask_user 被 fail-closed，第二轮仍能由模拟 LLM 正常收口。
    assert result == "已选择蓝色主题"
    tool_results = [
        message["content"]
        for message in session.messages
        if message.get("role") == "tool" and isinstance(message.get("content"), str)
    ]
    assert any("Permission denied" in content for content in tool_results)
    assert not any("forged" in content for content in tool_results)


def test_agent_no_longer_has_resume_method(tmp_path):
    """确认 resume() 方法已被移除。"""
    session = SessionState.create("goal", tmp_path)
    agent = Agent(AskThenAnswerLLM(), [ask_user_tool], session, SilentRenderer())

    assert not hasattr(agent, "resume")
