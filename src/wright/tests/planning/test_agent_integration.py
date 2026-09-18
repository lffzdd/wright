
from wright.tests.responses import event, response

from ...agent import Agent
from ...renderer import SilentRenderer
from ...session import Session
from ...tools.plan_tools import create_plan_tool


class PlanningLLM:
    context_limit = 128_000

    def __init__(self):
        self.calls = 0
        self.seen_messages = []

    def __call__(self, messages, **kwargs):
        self.seen_messages.append(list(messages))
        self.calls += 1
        if self.calls == 1:
            content = response(content=None, calls=[
                        {
                            "name": "create_plan",
                            "arguments": {
                                "objective": "完成复杂任务",
                                "steps": ["检查", "实现", "验证"],
                            },
                        }
                    ])
        else:
            content = response(content="计划已建立", calls=[])
        yield event(content=content)


def test_agent_injects_latest_plan_as_ephemeral_reminder(tmp_path):
    llm = PlanningLLM()
    session = Session.create("task", tmp_path)
    agent = Agent(
        llm,
        [create_plan_tool],
        session,
        SilentRenderer(),
    )

    answer = agent.run("处理这个复杂任务")

    assert answer == "计划已建立"
    assert len(llm.seen_messages) == 2
    assert not any(
        "<system-reminder>" in str(message.get("content", ""))
        for message in llm.seen_messages[0]
    )
    assert "<system-reminder>" in llm.seen_messages[1][-1]["content"]
    assert '"objective": "完成复杂任务"' in llm.seen_messages[1][-1]["content"]
    assert '"title": "检查"' in llm.seen_messages[1][-1]["content"]

    # reminder 是每轮临时 wire 状态，不复制进持久 transcript。
    assert not any(
        "<system-reminder>" in str(record.message.get("content", ""))
        for record in session.message_records
    )
