import json
import queue
import threading
import time

import pytest

from ...agent import Agent
from ...checkpoint import SessionCheckpointStore
from ...events import ContentDone
from ...looping import LoopError, SessionLoopRegistry, parse_interval, parse_loop_command
from ...renderer import SilentRenderer
from ...services import RuntimeServices
from ...session import SessionState
from ...tools.base import ToolRuntime
from ...tools.loop_tools import loop_tool


def _final(answer):
    return json.dumps({"tool_calls": [], "final_answer": answer})


class ScriptLLM:
    context_limit = 128_000

    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, messages):
        yield ContentDone(_final(self.answers.pop(0)))


def _registry(min_interval=0.05, idle=None):
    events = queue.Queue()
    if idle is None:
        idle = threading.Event()
        idle.set()
    registry = SessionLoopRegistry(
        events, idle, min_interval=min_interval, max_loops=20
    )
    return events, idle, registry


def test_loop_tick_uses_runtime_event_without_resetting_goal_or_plan(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SessionState.create("keep this goal", workspace)
    events, idle, registry = _registry()
    agent = Agent(
        ScriptLLM(["initial answer", "loop result"]),
        [],
        session,
        SilentRenderer(),
    )
    assert agent.run("keep this goal") == "initial answer"
    session.plan_manager.create_plan("ship", ["watch ci"])
    original_plan = session.plan_manager.snapshot()

    registry.start()
    try:
        created = registry.create(prompt="check deploy", interval_seconds=0.05)
        event_type, loop_id = events.get(timeout=1)
        assert event_type == "LOOP_DUE"
        assert loop_id == created.id
        record = registry.begin_tick(str(loop_id))
        assert record is not None and record.tick_count == 1
        result = agent.run_runtime_event(registry.runtime_event(record))
        registry.finish_tick(str(loop_id))
    finally:
        registry.close()

    assert result == "loop result"
    assert session.user_goal == "keep this goal"
    assert session.plan_manager.snapshot() == original_plan
    payload = json.loads(session.wire_messages()[-2]["content"])
    event = payload["runtime_event"]
    assert event["type"] == "loop_due"
    assert event["loop"]["id"] == created.id
    assert event["loop"]["prompt"] == "check deploy"
    assert event["loop"]["tick"] == 1


def test_busy_agent_coalesces_missed_loop_ticks():
    idle = threading.Event()
    events, _, registry = _registry(idle=idle)
    registry.start()
    try:
        registry.create(prompt="poll ci", interval_seconds=0.05)
        time.sleep(0.25)
        assert events.empty()
        idle.set()
        event_type, loop_id = events.get(timeout=1)
        assert event_type == "LOOP_DUE"
        time.sleep(0.2)
        assert events.empty()
        record = registry.begin_tick(str(loop_id))
        assert record is not None and record.tick_count == 1
    finally:
        registry.close()


def test_close_stops_loop_thread():
    events, idle, registry = _registry()
    registry.start()
    registry.create(prompt="ping", interval_seconds=0.05)
    thread = registry._thread
    assert thread is not None and thread.is_alive()
    registry.close()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert not any(
        item.is_alive() and item.name == "wright-session-loop"
        for item in threading.enumerate()
    )


def test_loop_rejects_short_interval_and_capacity():
    events, idle, registry = _registry(min_interval=5)
    with pytest.raises(LoopError, match=">= 5"):
        registry.create(prompt="too fast", interval_seconds=4)
    for index in range(20):
        registry.create(prompt=f"job {index}", interval_seconds=5)
    with pytest.raises(LoopError, match="at most 20"):
        registry.create(prompt="one more", interval_seconds=5)

    runtime = ToolRuntime(services=RuntimeServices(loop_registry=registry))
    failed = loop_tool.call(
        {"action": "create", "interval_seconds": 1, "prompt": "nope"},
        runtime,
    )
    assert not failed.ok
    assert ">= 5" in failed.err


def test_loop_state_is_absent_from_checkpoint(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SessionState.create("goal", workspace)
    events, idle, registry = _registry()
    created = registry.create(
        name="secret-loop-name",
        prompt="UNIQUE_LOOP_PROMPT_xyz",
        interval_seconds=0.05,
    )
    store = SessionCheckpointStore(tmp_path / "checkpoints")
    path = store.save(session)
    text = path.read_text(encoding="utf-8")
    assert created.id not in text
    assert "secret-loop-name" not in text
    assert "UNIQUE_LOOP_PROMPT_xyz" not in text
    restored = store.load(session.session_id)
    registry.close()


def test_parse_loop_command_and_interval_units():
    assert parse_interval("30s") == 30
    assert parse_interval("5m") == 300
    assert parse_interval("2h") == 7200
    assert parse_loop_command("/loop list") == ("list", None)
    action, payload = parse_loop_command("/loop 10s check deploy")
    assert action == "create"
    assert payload == (10.0, "check deploy")
    action, loop_id = parse_loop_command("/loop stop loop_ab12")
    assert action == "stop" and loop_id == "loop_ab12"
    with pytest.raises(ValueError, match="用法"):
        parse_loop_command("/loop")
