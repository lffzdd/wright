"""可靠性路径的回归测试。

超时/失败路径平时跑不到——不写测试它就等于不存在,第一次线上工具挂死时才爆。
LLMClient 用假参数构造(不发起任何网络请求),整套测试离线可跑。

运行(项目根目录下):
    uv run python scripts/smoke_agent.py
"""

import json
import tempfile
import threading
import time
from wright.tests.responses import event, response

from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.TemporaryDirectory(prefix="wright-test-", ignore_cleanup_errors=True)
_WORKSPACE = Path(_TMP.name)

from wright.agent import Agent
from wright.events import ContentDelta, ContentDone, UsageEvent
from wright.llm import LLMClient
from wright.renderer import SilentRenderer
from wright.session import Session, UsageRecord
from wright.tools.base import Tool, ToolCall, ToolResult, ToolRuntime


class RecordingRenderer(SilentRenderer):
    def __init__(self):
        self.context_compacts = []

    def on_context_compact(
        self,
        folded_count: int,
        prompt_tokens: int | None,
        context_limit: int | None,
        context_watermark: float,
    ) -> None:
        self.context_compacts.append(
            {
                "folded_count": folded_count,
                "prompt_tokens": prompt_tokens,
                "context_limit": context_limit,
                "context_watermark": context_watermark,
            }
        )


def _make_session(user_goal: str = "") -> Session:
    return Session.create(
        initial_goal=user_goal,
        workspace_dir=_WORKSPACE,
    )


def _make_agent(
    tools: list[Tool], tool_timeout: float, keep_recent_tool_results: int = 3
) -> Agent:
    llm = LLMClient(
        base_url="http://x", api_key="sk-x", model="m", context_limit=128_000
    )
    return Agent(
        llm,
        tools,
        _make_session(),
        SilentRenderer(),
        tool_timeout=tool_timeout,
        keep_recent_tool_results=keep_recent_tool_results,
    )


def test_agent_does_not_duplicate_system_prompt_for_existing_session():
    llm = LLMClient(
        base_url="http://x", api_key="sk-x", model="m", context_limit=128_000
    )
    session = _make_session()

    first_agent = Agent(llm, [], session, SilentRenderer(), tool_timeout=5)
    second_agent = Agent(llm, [], session, SilentRenderer(), tool_timeout=5)

    system_messages = [msg for msg in session.messages if msg.get("role") == "system"]
    assert tuple(first_agent.messages) == tuple(second_agent.messages)
    assert not hasattr(first_agent.messages, "append")
    assert len(system_messages) == 1


def test_parallel_timeout_fills_fail():
    """协作式取消后必须以 timeout 占位，不能遗弃仍运行的线程。"""

    def fast(args, runtime):
        return ToolResult.success("fast done")

    def slow(args, runtime):
        while True:
            runtime.raise_if_cancelled()
            time.sleep(0.01)

    agent = _make_agent(
        [
            Tool("fast", "", {}, fast, is_concurrency_safe=lambda args: True),
            Tool("slow", "", {}, slow, is_concurrency_safe=lambda args: True),
        ],
        tool_timeout=0.5,
    )
    calls = [ToolCall("fast", {}, "c1"), ToolCall("slow", {}, "c2")]

    t0 = time.time()
    outcomes = agent.executor.execute(calls)
    elapsed = time.time() - t0

    assert len(outcomes) == len(calls), "模型靠 id 对账,结果少一条都不行"
    assert outcomes[0].result.ok and outcomes[0].result.data == "fast done"
    assert outcomes[0].status == "succeeded"
    assert not outcomes[1].result.ok and "timeout" in outcomes[1].result.err
    assert outcomes[1].status == "timeout"
    assert elapsed < 2, f"应在预算 0.5s 附近返回,实际等了 {elapsed:.1f}s"


def test_mixed_calls_preserve_original_batch_order():
    """后面的安全调用不能越过前面的排他调用。"""
    log: list[str] = []
    lock = threading.Lock()

    def make_tool(name: str, safe: bool):
        def call(args, runtime):
            with lock:
                log.append(f"start:{name}")
            time.sleep(0.02)
            with lock:
                log.append(f"end:{name}")
            return ToolResult.success(name)

        return Tool(
            name,
            "",
            {},
            call,
            is_concurrency_safe=lambda args: safe,
        )

    agent = _make_agent(
        [
            make_tool("write", False),
            make_tool("read1", True),
            make_tool("read2", True),
        ],
        tool_timeout=1,
    )
    agent.executor.execute([
        ToolCall("write", {}, "c1"),
        ToolCall("read1", {}, "c2"),
        ToolCall("read2", {}, "c3"),
    ])

    assert log[0:2] == ["start:write", "end:write"]
    assert set(log[2:4]) == {"start:read1", "start:read2"}


def test_serial_timeout_exits_before_next_tool_starts():
    """排他工具收到 deadline 后必须先退出，下一项才能跨过批次边界。"""
    log: list[str] = []

    def slow(args, runtime):
        log.append("slow-start")
        while not runtime.is_cancelled():
            time.sleep(0.01)
        log.append("slow-exit")
        runtime.raise_if_cancelled()

    def next_tool(args, runtime):
        log.append("next-start")
        return ToolResult.success()

    agent = _make_agent(
        [
            Tool("slow", "", {}, slow),
            Tool("next", "", {}, next_tool),
        ],
        tool_timeout=0.1,
    )
    outcomes = agent.executor.execute([
        ToolCall("slow", {}, "c1"),
        ToolCall("next", {}, "c2"),
    ])

    assert log == ["slow-start", "slow-exit", "next-start"]
    assert outcomes[0].status == "timeout"
    assert outcomes[1].status == "succeeded"


def test_inner_timeout_clamped_to_budget():
    """模型传的内层 timeout 必须被钳到外层预算内,否则外层先掐。"""
    captured = {}

    def spy(timeout: int = 20):
        captured["timeout"] = timeout
        return ToolResult.success(None)

    agent = _make_agent(
        [Tool("spy", "", {}, lambda args, runtime: spy(**args))],
        tool_timeout=30,
    )
    agent.executor.execute([ToolCall("spy", {"timeout": 300}, "c1")])

    assert captured["timeout"] == 30


def test_tool_runtime_is_separate_from_model_arguments():
    """运行期上下文是 call 的独立参数,不混进模型生成的 arguments。"""

    captured = {}

    def spy(args: dict, runtime: ToolRuntime):
        captured["args"] = args
        captured["runtime"] = runtime
        return ToolResult.success(None)

    tool = Tool(
        "spy", "", {}, spy,
        required_capabilities=frozenset({"execution"}),
    )
    tool_call = ToolCall("spy", {"runtime": "model supplied"}, "c1")
    agent = _make_agent([tool], tool_timeout=5)

    result = agent.executor.execute([tool_call])[0].result

    assert result.ok
    assert captured["args"] == {"runtime": "model supplied"}
    assert isinstance(captured["runtime"], ToolRuntime)
    assert captured["runtime"].tool_name == "spy"
    assert captured["runtime"].tool_call_id == "c1"
    assert (
        captured["runtime"].capabilities.execution.workspace_dir
        == agent.session_state.workspace_dir
    )
    assert tool_call.arguments == {"runtime": "model supplied"}
    assert "runtime" not in tool.to_dict()["parameters"].get("properties", {})


def test_tool_exception_becomes_fail():
    """工具抛异常是数据(fail),不是事故,整轮照常。"""

    def boom():
        raise RuntimeError("炸了")

    agent = _make_agent(
        [Tool("boom", "", {}, lambda args, runtime: boom())],
        tool_timeout=5,
    )
    outcomes = agent.executor.execute([ToolCall("boom", {}, "c1")])

    assert not outcomes[0].result.ok
    assert "RuntimeError" in outcomes[0].result.err


def test_run_turn_records_usage():
    """UsageEvent 要被 Agent 接住:上下文取最近值,输出 token 做累计。"""

    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.context_limit = 128_000

        def __call__(self, messages, **kwargs):
            self.calls += 1
            yield event(content=f"content {self.calls}")
            yield UsageEvent(
                SimpleNamespace(
                    prompt_tokens=100 * self.calls,
                    completion_tokens=10 * self.calls,
                    total_tokens=110 * self.calls,
                )
            )

    agent = Agent(FakeLLM(), [], _make_session(), SilentRenderer(), tool_timeout=5)

    content, usage = agent._run_turn()
    assert content.content == "content 1"
    assert usage == UsageRecord(prompt_tokens=100, completion_tokens=10, total_tokens=110)

    content, usage = agent._run_turn()
    assert content.content == "content 2"
    assert usage == UsageRecord(prompt_tokens=200, completion_tokens=20, total_tokens=220)


def test_run_turn_records_dict_usage():
    """兼容 dict 形态的 usage,不少 OpenAI-compatible 接口会这样返回。"""

    class FakeLLM:
        context_limit = 128_000

        def __call__(self, messages, **kwargs):
            yield event(content="ok")
            yield UsageEvent(
                {
                    "completion_tokens": 1541,
                    "prompt_tokens": 11,
                    "total_tokens": 1552,
                    "completion_tokens_details": {
                        "reasoning_tokens": 1426,
                    },
                    "prompt_tokens_details": {
                        "cached_tokens": 0,
                    },
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 11,
                }
            )

    agent = Agent(FakeLLM(), [], _make_session(), SilentRenderer(), tool_timeout=5)

    content, usage = agent._run_turn()
    assert content.content == "ok"
    assert usage == UsageRecord(
        prompt_tokens=11, completion_tokens=1541, total_tokens=1552
    )


def test_stream_usage_event_can_live_on_choice_chunk():
    """兼容接口可能把 usage 挂在带 choices 的流式 chunk 上,不能漏读。"""

    usage = SimpleNamespace(prompt_tokens=12, completion_tokens=3)
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", delta=SimpleNamespace(content="hi", tool_calls=None))],
        usage=usage,
    )

    class FakeCompletions:
        def create(self, **kwargs):
            return iter([chunk])

    llm = LLMClient.__new__(LLMClient)
    llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    llm.model = "fake-model"
    llm.max_attempts = 1
    llm.base_wait = 0
    llm.max_wait = 0
    llm.response_format = {"type": "json_object"}

    events = list(llm._call_stream([{"role": "user", "content": "hello"}], {}, "fake-model"))

    assert isinstance(events[0], ContentDelta) and events[0].piece == "hi"
    assert isinstance(events[1], UsageEvent)
    assert isinstance(events[-1], ContentDone)


def test_session_records_tool_turn_and_execution():
    session = _make_session("inspect file")
    call = ToolCall("read_file", {"file": "a.py"}, "call_1")

    turn = session.record_assistant_turn(
        assistant_raw='Inspecting file',
        parsed={"tool_calls": [{"name": "read_file"}], "final_answer": None},
        route="tool_calls",
        tool_calls=[call],
    )

    assert session.step_count == 1
    assert turn.step == 1
    assert turn.message_id == "msg_1"
    assert turn.tool_execution_ids == ["call_1"]
    assert session.tool_executions["call_1"].call is call
    assert session.tool_executions["call_1"].result is None
    assert session.tool_executions["call_1"].status == "pending"

    result = ToolResult.success({"content": "hello"})
    execution = session.record_tool_execution("call_1", result)

    assert execution.result is result
    assert execution.status == "succeeded"


def test_session_rejects_duplicate_tool_ids_without_partial_state():
    session = _make_session("duplicate calls")
    calls = [
        ToolCall("read_file", {"file": "a.py"}, "call_dup"),
        ToolCall("read_file", {"file": "b.py"}, "call_dup"),
    ]

    try:
        session.record_assistant_turn(
            assistant_raw='Invalid duplicate call IDs',
            parsed={"tool_calls": [{}, {}], "final_answer": None},
            route="tool_calls",
            tool_calls=calls,
        )
    except ValueError as exc:
        assert "重复的 tool_call id" in str(exc)
    else:
        raise AssertionError("duplicate tool_call ids should be rejected")

    assert session.step_count == 0
    assert session.turns == []
    assert session.tool_executions == {}


def test_session_records_invalid_usage_and_status():
    session = _make_session("bad output")

    turn = session.record_invalid_turn("", "LLM 输出被截断")
    assert session.step_count == 1
    assert turn.route == "invalid"
    assert turn.error == "LLM 输出被截断"
    assert turn.tool_execution_ids == []

    session.record_usage_for_turn(
        turn,
        UsageRecord(prompt_tokens=10, completion_tokens=2, total_tokens=12),
    )
    assert turn.usage == UsageRecord(prompt_tokens=10, completion_tokens=2, total_tokens=12)
    assert session.last_usage == turn.usage
    assert session.total_usage == UsageRecord(
        prompt_tokens=10, completion_tokens=2, total_tokens=12
    )

    final_turn = session.record_assistant_turn(
        assistant_raw='done',
        parsed={"tool_calls": [], "final_answer": "done"},
        route="final",
    )
    assert final_turn.step == 2
    assert final_turn.tool_execution_ids == []

    session.begin_user_turn("record test")
    session.mark_completed()
    assert session.current_run_status() == "completed"


def test_assistant_raw_uses_stable_message_id_after_non_assistant_reorder():
    session = _make_session("stable ids")
    session.append_message({"role": "user", "content": "before"})

    first_turn = session.record_assistant_turn(
        assistant_raw='one',
        parsed={"tool_calls": [], "final_answer": "one"},
        route="final",
    )
    session.append_message({"role": "user", "content": "between"})
    second_turn = session.record_assistant_turn(
        assistant_raw='two',
        parsed={"tool_calls": [], "final_answer": "two"},
        route="final",
    )
    session.append_message({"role": "user", "content": "after"})

    assistant_records = [
        record
        for record in session.message_records
        if record.message.get("role") == "assistant"
    ]
    user_records = [
        record
        for record in session.message_records
        if record.message.get("role") == "user"
    ]
    session.message_records[:] = [
        user_records[-1],
        assistant_records[1],
        assistant_records[0],
    ]

    assert session.assistant_raw(first_turn) == 'one'
    assert session.assistant_raw(second_turn) == 'two'


def test_run_defaults_to_session_max_steps():
    class InvalidLLM:
        context_limit = 128_000

        def __call__(self, messages, **kwargs):
            yield ContentDone("", finish_reason="length")

    session = _make_session()
    session.max_steps = 2
    agent = Agent(InvalidLLM(), [], session, SilentRenderer(), tool_timeout=5)

    assert agent.run("keep failing") is None
    assert session.current_run_status() == "failed"
    assert session.step_count == 2
    assert session.max_steps == 2


def test_run_aborts_after_consecutive_invalid():
    """连续 N 轮响应不完整 就止损 failed,不该把 max_steps 烧光。"""

    class InvalidLLM:
        context_limit = 128_000

        def __call__(self, messages, **kwargs):
            yield ContentDone("", finish_reason="length")

    session = _make_session()
    session.max_steps = 25
    agent = Agent(
        InvalidLLM(),
        [],
        session,
        SilentRenderer(),
        tool_timeout=5,
        max_consecutive_invalid=3,
    )

    assert agent.run("keep failing") is None
    assert session.current_run_status() == "failed"
    assert session.step_count == 3, "第 3 次连续失败就该止损,不烧到 max_steps"


def test_consecutive_invalid_resets_on_success():
    """计数器是'连续'语义:中间成功一次必须清零,不是累计总失败数。"""

    script = [
        ContentDone("", finish_reason="length"),  # 连续 1
        response(content=None, calls=[{"name": "noop"}]),  # 成功→清零
        ContentDone("", finish_reason="length"),  # 连续 1(若没清零会变成 2 而误杀)
        response(content="done", calls=[]),  # 成功收尾
    ]

    class ScriptedLLM:
        context_limit = 128_000

        def __init__(self):
            self.calls = 0

        def __call__(self, messages, **kwargs):
            content = script[self.calls]
            self.calls += 1
            yield event(content=content)

    def noop():
        return ToolResult.success("ok")

    session = _make_session()
    agent = Agent(
        ScriptedLLM(),
        [Tool("noop", "", {}, lambda args, runtime: noop())],
        session,
        SilentRenderer(),
        tool_timeout=5,
        max_consecutive_invalid=2,
    )

    # 阈值 2,但失败从不连续出现两次,所以应正常完成而非 failed
    assert agent.run("mix") == "done"
    assert session.current_run_status() == "completed"


def test_is_tool_result_message_only_accepts_valid_tool_results():
    compactor = _make_agent([], tool_timeout=5).compactor
    assert compactor._is_tool_result_message({
        "role": "tool", "tool_call_id": "c1", "content": "result",
    })
    for message in [
        {"role": "user", "content": '{"tool_results": []}'},
        {"role": "assistant", "content": "result"},
        {"role": "tool", "content": "result"},
        {"role": "tool", "tool_call_id": "c1", "content": None},
    ]:
        assert not compactor._is_tool_result_message(message)


def test_context_projection_folds_old_results_without_mutating_history():
    agent = _make_agent([], tool_timeout=5, keep_recent_tool_results=2)

    original_messages = list(agent.messages)
    for i in range(5):
        agent.session_state.append_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": json.dumps({
                                    "ok": True,
                                    "err": "",
                                    "data": f"large result {i} " * 20,
                                }, ensure_ascii=False), "_test_extra_field": f"keep {i}"}
        )
        agent.session_state.append_message(
            {"role": "assistant", "content": f"assistant {i}"}
        )

    before_ids = [record.id for record in agent.session_state.message_records]
    view = agent.context_builder.build(
        agent.session_state.message_records, tools=[], context_limit=100, output_reserve_tokens=0,
    )
    assert len(view.folded_record_ids) == 3
    assert [record.id for record in agent.session_state.message_records] == before_ids
    assert list(agent.messages[: len(original_messages)]) == original_messages

    tool_result_messages = [
        json.loads(msg["content"])
        for msg in view.messages
        if agent.compactor._is_tool_result_message(msg)
    ]

    assert len(tool_result_messages) == 5

    for folded in tool_result_messages[:3]:
        assert folded["folded"] is True
        result = folded
        assert result["ok"] is True
        assert result["err"] == ""
        assert result["data"] == "[older tool result folded for this request]"

    folded_messages = [msg for msg in view.messages if (
        agent.compactor._is_tool_result_message(msg)
        and json.loads(msg["content"]).get("folded")
    )]
    assert [msg["_test_extra_field"] for msg in folded_messages] == [
        "keep 0",
        "keep 1",
        "keep 2",
    ]

    for recent_idx, recent in enumerate(tool_result_messages[3:], start=3):
        assert "folded" not in recent
        assert (
            recent["data"]
            == f"large result {recent_idx} " * 20
        )


def test_context_projection_is_idempotent():
    agent = _make_agent([], tool_timeout=5, keep_recent_tool_results=1)
    for i in range(3):
        agent.session_state.append_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": json.dumps({"ok": False, "err": f"err {i}", "data": "x" * 200}, ensure_ascii=False)}
        )

    first = agent.context_builder.build(
        agent.session_state.message_records, tools=[], context_limit=100, output_reserve_tokens=0,
    )
    second = agent.context_builder.build(
        agent.session_state.message_records, tools=[], context_limit=100, output_reserve_tokens=0,
    )
    assert len(first.folded_record_ids) == 2
    assert second.folded_record_ids == first.folded_record_ids
    assert first.messages == second.messages
    assert not any(json.loads(message["content"]).get("folded") for message in agent.messages if message.get("role") == "tool")


def _append_tool_result_message(agent: Agent, idx: int) -> None:
    agent.session_state.append_message(
        {"role": "tool", "tool_call_id": f"call_{idx}", "content": json.dumps({
                                "ok": True,
                                "err": "",
                                "data": f"large result content that is long enough to make folding save tokens {idx}" * 5,
                            }, ensure_ascii=False)}
    )


def _make_compactor_agent(renderer, keep_recent_tool_results, watermark=0.75):
    llm = LLMClient(
        base_url="http://x", api_key="sk-x", model="m", context_limit=100
    )
    return Agent(
        llm,
        [],
        _make_session(),
        renderer,
        tool_timeout=5,
        context_watermark=watermark,
        keep_recent_tool_results=keep_recent_tool_results,
    )


def test_context_projection_reports_folding_without_changing_history():
    """Projection crossing the watermark reports folding without rewriting history."""
    renderer = RecordingRenderer()
    agent = _make_compactor_agent(renderer, keep_recent_tool_results=2)

    for i in range(5):
        _append_tool_result_message(agent, i)

    original = [record.message.copy() for record in agent.session_state.message_records]
    view = agent.context_builder.build(
        agent.session_state.message_records, tools=[], context_limit=100, output_reserve_tokens=0,
    )
    assert len(view.folded_record_ids) == 3
    assert [record.message for record in agent.session_state.message_records] == original

    assert len(renderer.context_compacts) == 1
    report = renderer.context_compacts[0]
    assert report["folded_count"] == 3
    assert report["context_limit"] == 100
    assert report["context_watermark"] == 0.75
    assert report["prompt_tokens"] > 75


def test_context_projection_without_tool_results_does_not_fold():
    renderer = RecordingRenderer()
    agent = _make_compactor_agent(renderer, keep_recent_tool_results=3)

    agent.session_state.append_message({"role": "user", "content": "x" * 1_000})
    view = agent.context_builder.build(
        agent.session_state.message_records, tools=[], context_limit=100, output_reserve_tokens=0,
    )
    assert view.folded_record_ids == ()
    assert renderer.context_compacts[-1]["folded_count"] == 0


def test_context_projection_skips_below_watermark():
    renderer = RecordingRenderer()
    agent = _make_compactor_agent(renderer, keep_recent_tool_results=1)

    view = agent.context_builder.build(
        agent.session_state.message_records, tools=[], context_limit=100, output_reserve_tokens=0,
    )
    assert view.folded_record_ids == ()
    assert renderer.context_compacts == []


def test_history_estimate_stays_distinct_from_reported_usage():
    """Persistent-history estimate is never overwritten by provider usage."""
    session = _make_session("calibration test")

    # 模拟追加了一些消息,running total 是估算值(可能不准)
    session.append_message({"role": "user", "content": "hello world"})
    estimated_before = session.context_tokens
    assert estimated_before > 0

    # 模拟 record_assistant_turn + record_usage
    turn = session.record_assistant_turn(
        assistant_raw='done',
        parsed={"tool_calls": [], "final_answer": "done"},
        route="final",
    )
    session.record_usage_for_turn(
        turn, UsageRecord(prompt_tokens=500, completion_tokens=50, total_tokens=550)
    )
    assert session.last_usage == UsageRecord(500, 50, 550)
    assert session.context_tokens > 0
    assert session.context_tokens != 550


def test_running_total_append_message_increments():
    """append_message 是唯一入口,每次追加都增量累加 running total。"""
    session = _make_session("increment test")
    before = session.context_tokens
    message_id = session.append_message(
        {"role": "user", "content": "a]" * 100}
    )  # 200 chars ~= 50 tokens
    after = session.context_tokens
    assert message_id == "msg_1"
    assert after > before
    assert after - before == 50  # 200 chars // 4

    turn = session.record_assistant_turn(
        assistant_raw='done',
        parsed={"tool_calls": [], "final_answer": "done"},
        route="final",
    )
    assert turn.message_id == "msg_2"


if __name__ == "__main__":
    test_agent_does_not_duplicate_system_prompt_for_existing_session()
    test_parallel_timeout_fills_fail()
    test_mixed_calls_preserve_original_batch_order()
    test_serial_timeout_exits_before_next_tool_starts()
    test_inner_timeout_clamped_to_budget()
    test_tool_runtime_is_separate_from_model_arguments()
    test_tool_exception_becomes_fail()
    test_run_turn_records_usage()
    test_run_turn_records_dict_usage()
    test_stream_usage_event_can_live_on_choice_chunk()
    test_session_records_tool_turn_and_execution()
    test_session_rejects_duplicate_tool_ids_without_partial_state()
    test_session_records_invalid_usage_and_status()
    test_assistant_raw_uses_stable_message_id_after_non_assistant_reorder()
    test_run_defaults_to_session_max_steps()
    test_run_aborts_after_consecutive_invalid()
    test_consecutive_invalid_resets_on_success()
    test_is_tool_result_message_only_accepts_valid_tool_results()
    test_context_projection_folds_old_results_without_mutating_history()
    test_context_projection_is_idempotent()
    test_context_projection_reports_folding_without_changing_history()
    test_context_projection_without_tool_results_does_not_fold()
    test_context_projection_skips_below_watermark()
    test_history_estimate_stays_distinct_from_reported_usage()
    test_running_total_append_message_increments()
    print("all tests passed")
