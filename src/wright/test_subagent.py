"""子 Agent 编排(spawn_agent)的离线回归测试。

用 ScriptedLLM 按调用顺序喂回包,父子 Agent 共用同一个 llm:执行是同步串行的
(父发起 spawn → 子 Agent 跑到收口 → 结果回填 → 父继续),所以一条脚本按调用顺序
就能驱动整条委派链,全程不发任何网络请求。

运行(项目根目录下):
    python -m wright.test_subagent
"""

import json
import tempfile
import threading
import time
from pathlib import Path

from .agent import Agent
from .events import ContentDone
from .renderer import SilentRenderer
from .session import SessionState
from .subagent import build_agent_tools, make_spawn_agent_tool
from .tools.base import Tool, ToolResult


_TMP = tempfile.TemporaryDirectory(prefix="wright-subagent-", ignore_cleanup_errors=True)
WORKSPACE = Path(_TMP.name)


class ScriptedLLM:
    """按调用顺序逐条吐回包;记录每次收到的 messages 以便断言上下文隔离。"""

    context_limit = 128_000

    def __init__(self, script: list[str]):
        self.script = script
        self.calls = 0
        self.seen_messages: list[list] = []

    def __call__(self, messages):
        self.seen_messages.append(list(messages))
        content = self.script[self.calls]
        self.calls += 1
        yield ContentDone(content=content)


def _tool_calls(name: str, **arguments) -> str:
    return json.dumps(
        {"tool_calls": [{"name": name, "arguments": arguments}], "final_answer": None}
    )


def _many_tool_calls(calls: list[tuple[str, dict]]) -> str:
    return json.dumps(
        {
            "tool_calls": [
                {"name": name, "arguments": arguments}
                for name, arguments in calls
            ],
            "final_answer": None,
        }
    )


def _final(answer: str) -> str:
    return json.dumps({"tool_calls": [], "final_answer": answer})


def _make_session(goal: str = "主任务") -> SessionState:
    return SessionState.create(user_goal=goal, workspace_dir=WORKSPACE)


def test_spawn_agent_listed_in_parent_tools_but_not_at_max_depth():
    """build_agent_tools:未到上限带 spawn,到 max_depth 那层不带(递归到底)。"""
    llm = ScriptedLLM([])

    top = build_agent_tools(llm, [], depth=0, max_depth=2)
    assert "spawn_agent" in {t.name for t in top}

    leaf = build_agent_tools(llm, [], depth=2, max_depth=2)
    assert "spawn_agent" not in {t.name for t in leaf}


def test_parent_delegates_and_aggregates_child_result():
    """父 Agent 委派 → 子 Agent 独立收口 → 结论作为 tool_result 回到父 Agent。"""
    llm = ScriptedLLM(
        [
            _tool_calls("spawn_agent", task="子任务:算 1+..+100"),  # 父 turn1:委派
            _final("子 Agent 报告:1 到 100 之和为 5050"),  # 子 turn1:收口
            _final("汇总:子 Agent 算得 5050"),  # 父 turn2:聚合
        ]
    )

    session = _make_session()
    tools = build_agent_tools(llm, [], depth=0, max_depth=2)
    agent = Agent(llm, tools, session, SilentRenderer())

    result = agent.run("把子任务委派出去")

    assert result == "汇总:子 Agent 算得 5050"
    assert session.status == "completed"
    # 父对话里只看得到"委派一次 + 拿回一条 tool_result",中间步骤被隔离在子上下文。
    tool_results_msgs = [
        m
        for m in session.wire_messages()
        if m.get("role") == "user" and "tool_results" in str(m.get("content", ""))
    ]
    assert len(tool_results_msgs) == 1
    payload = json.loads(tool_results_msgs[0]["content"])["tool_results"][0]
    assert payload["name"] == "spawn_agent"
    assert payload["result"]["ok"] is True
    assert payload["result"]["data"]["result"] == "子 Agent 报告:1 到 100 之和为 5050"
    assert payload["result"]["data"]["status"] == "completed"


def test_child_context_is_isolated_from_parent():
    """子 Agent 收到的 messages 不含父对话历史——上下文隔离的硬证据。"""
    parent_goal_marker = "PARENT_SECRET_GOAL_XYZ"
    llm = ScriptedLLM(
        [
            _tool_calls("spawn_agent", task="干净的子任务"),
            _final("子任务完成"),
            _final("主任务完成"),
        ]
    )

    session = _make_session(goal=parent_goal_marker)
    tools = build_agent_tools(llm, [], depth=0, max_depth=2)
    agent = Agent(llm, tools, session, SilentRenderer())
    agent.run(parent_goal_marker)

    # 第 2 次 LLM 调用是子 Agent 的 turn1;它看到的所有消息都不该提到父任务的暗号。
    child_first_messages = llm.seen_messages[1]
    blob = json.dumps(child_first_messages, ensure_ascii=False)
    assert parent_goal_marker not in blob
    assert "干净的子任务" in blob  # 子 Agent 只看到自己的 task


def test_child_failure_surfaces_as_failed_tool_result():
    """子 Agent 步数耗尽:父 Agent 拿到 ok=False 的 tool_result,而非静默成功。"""
    # 子 Agent 每轮都发同一个工具调用却永不收口 → 撞上 child_max_steps。
    child_loop = _tool_calls("noop")
    llm = ScriptedLLM(
        [
            _tool_calls("spawn_agent", task="注定跑不完的子任务"),  # 父 turn1
            child_loop,  # 子 turn1
            child_loop,  # 子 turn2(child_max_steps=2,到此耗尽)
            _final("子 Agent 没做完,主 Agent 如实收尾"),  # 父 turn2
        ]
    )

    noop = Tool("noop", "", {}, lambda args, runtime: ToolResult.success("ok"))
    # 直接造一把 child_max_steps=2 的 spawn 工具,逼子 Agent 快速耗尽步数。
    spawn = make_spawn_agent_tool(
        llm, [noop], depth=0, max_depth=2, child_max_steps=2, render_subagents=False
    )

    session = _make_session()
    agent = Agent(llm, [noop, spawn], session, SilentRenderer())
    result = agent.run("委派一个跑不完的任务")

    assert result == "子 Agent 没做完,主 Agent 如实收尾"
    tool_results_msgs = [
        m
        for m in session.wire_messages()
        if m.get("role") == "user" and "tool_results" in str(m.get("content", ""))
    ]
    payload = json.loads(tool_results_msgs[0]["content"])["tool_results"][0]
    assert payload["result"]["ok"] is False
    assert payload["result"]["data"]["status"] == "max_steps"


def test_multiple_spawn_agents_run_concurrently_and_preserve_result_order():
    """同一连续批次中的子 Agent 并发执行，父级结果仍按 tool_call 顺序聚合。"""

    class ConcurrentLLM:
        context_limit = 128_000

        def __init__(self):
            self.lock = threading.Lock()
            self.active_children = 0
            self.max_active_children = 0

        def __call__(self, messages):
            last = messages[-1]["content"]
            if last == "并行委派":
                yield ContentDone(
                    content=_many_tool_calls([
                        ("spawn_agent", {"task": "task-a"}),
                        ("spawn_agent", {"task": "task-b"}),
                    ])
                )
                return
            if last in {"task-a", "task-b"}:
                with self.lock:
                    self.active_children += 1
                    self.max_active_children = max(
                        self.max_active_children, self.active_children
                    )
                time.sleep(0.1)
                with self.lock:
                    self.active_children -= 1
                yield ContentDone(content=_final(f"done:{last}"))
                return
            if isinstance(last, str) and "tool_results" in last:
                payload = json.loads(last)
                ordered = [
                    item["result"]["data"]["result"]
                    for item in payload["tool_results"]
                ]
                yield ContentDone(content=_final("|".join(ordered)))
                return
            raise AssertionError(f"unexpected messages: {messages!r}")

    llm = ConcurrentLLM()
    session = _make_session()
    tools = build_agent_tools(llm, [], depth=0, max_depth=1)
    agent = Agent(llm, tools, session, SilentRenderer(), tool_timeout=2)

    result = agent.run("并行委派")

    assert llm.max_active_children == 2
    assert result == "done:task-a|done:task-b"


def _run_all():
    tests = [
        test_spawn_agent_listed_in_parent_tools_but_not_at_max_depth,
        test_parent_delegates_and_aggregates_child_result,
        test_child_context_is_isolated_from_parent,
        test_child_failure_surfaces_as_failed_tool_result,
        test_multiple_spawn_agents_run_concurrently_and_preserve_result_order,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n全部 {len(tests)} 个子 Agent 编排测试通过。")


if __name__ == "__main__":
    _run_all()
