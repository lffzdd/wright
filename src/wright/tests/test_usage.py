from io import StringIO

import pytest
from rich.console import Console

from wright.agent import Agent
from wright.checkpoint import SessionCheckpointStore
from wright.events import UsageEvent
from wright.memory.llm_util import metered_events
from wright.renderer import ConsoleRenderer, SilentRenderer
from wright.session import SessionState, UsageRecord
from wright.tests.responses import response
from wright.tools.base import Tool, ToolCall, ToolResult
from wright.verifier import Verifier


class Capture(SilentRenderer):
    def __init__(self):
        self.requests = []
        self.summaries = []

    def on_usage(self, *args):
        self.requests.append(args)

    def on_usage_summary(self, *args):
        self.summaries.append(args)


def test_usage_after_tool_results_and_verifier(tmp_path):
    class LLM:
        context_limit = 10000
        calls = 0

        def __call__(self, messages, **kwargs):
            self.calls += 1
            yield UsageEvent({'prompt_tokens': 5, 'completion_tokens': 1})
            yield UsageEvent({'prompt_tokens': 100, 'completion_tokens': 20})
            yield response(calls=[{'name': 'read', 'arguments': {}}]) if self.calls == 1 else response(content='done')

    session = SessionState.create('test', tmp_path)
    renderer = Capture()
    tool = Tool('read', 'read', {'type': 'object', 'properties': {}}, lambda args, runtime: ToolResult.success('x' * 400))
    agent = Agent(LLM(), [tool], session, renderer, verifier=Verifier())
    assert agent.run('test') == 'done'
    assert len(renderer.requests) == 2  # Intermediate snapshots do not print twice.
    assert renderer.summaries == [(200, 40, 240)]
    assert session.context_tokens == 120


def test_task_totals_include_descendants_once_and_survive_resume(tmp_path):
    session = SessionState.create('old', tmp_path)
    session.add_usage(UsageRecord(90, 10, 100))
    session.begin_user_turn('new')
    session.add_usage(UsageRecord(10, 5, 15))
    plane = session.control_plane
    parent = None
    for depth in (1, 2):
        task = plane.begin_task(root_turn_id=session.agent_root_turn_id, parent_id=parent,
            tool_call_id=str(depth), depth=depth, task='child', requested_steps=1)
        plane.add_usage(task.id, 20, 3, 23)
        parent = task.id
    assert session.task_usage() == UsageRecord(50, 11, 61)
    store = SessionCheckpointStore(tmp_path / 'checkpoints')
    store.save(session)
    restored = store.load(session.session_id)
    assert restored.task_usage() == UsageRecord(50, 11, 61)
    restored.append_message({'role': 'user', 'content': 'new'})
    restored.begin_user_turn('next')
    assert restored.task_usage() == UsageRecord()


def test_metered_failure_keeps_last_snapshot():
    def events():
        yield UsageEvent({'prompt_tokens': 2, 'completion_tokens': 1})
        yield UsageEvent({'prompt_tokens': 8, 'completion_tokens': 3})
        raise RuntimeError('stream failed')

    recorded = []
    with pytest.raises(RuntimeError):
        list(metered_events(events(), recorded.append))
    assert recorded == [UsageRecord(8, 3, 11)]


def test_console_labels_and_unknown_usage():
    renderer = ConsoleRenderer()
    output = StringIO()
    renderer._console = Console(file=output, width=200, color_system=None)
    renderer.on_usage(None, None, None, 1000)
    renderer.on_usage_summary(200, 30, 230)
    text = output.getvalue()
    assert '本次请求' in text and '输入 ?' in text
    assert '当前任务累计（已报告）' in text
    assert '预计占用' not in text


def test_console_tool_result_labels_the_tool():
    renderer = ConsoleRenderer()
    output = StringIO()
    renderer._console = Console(file=output, width=200, color_system=None)
    call = ToolCall("list_files", {"directory": "."}, "c1")
    renderer.on_tool_result(call, ToolResult.success({"files": ["a.py"]}))
    renderer.on_tool_result(call, ToolResult.fail("not found"))
    text = output.getvalue()
    assert "list_files" in text
    assert "工具结果" not in text
    assert "not found" in text


def test_console_streams_then_panels_the_final_answer():
    renderer = ConsoleRenderer()
    output = StringIO()
    renderer._console = Console(file=output, width=200, color_system=None)
    renderer.on_content_delta("hello world")
    assert output.getvalue() == ""
    renderer.on_final("hello world")
    text = output.getvalue()
    assert text.count("hello world") == 1
    assert "╭" in text


def test_console_tool_card_settles_once():
    renderer = ConsoleRenderer()
    output = StringIO()
    renderer._console = Console(file=output, width=200, color_system=None)
    call = ToolCall("list_files", {"directory": "."}, "c1")
    renderer.on_tool_call(call)
    assert output.getvalue() == ""
    renderer.on_tool_result(call, ToolResult.success({"files": ["a.py"]}))
    text = output.getvalue()
    assert "list_files" in text
    assert "🔧" not in text
    assert "╭" in text


def test_console_panels_final_answer_when_nothing_was_streamed():
    renderer = ConsoleRenderer()
    output = StringIO()
    renderer._console = Console(file=output, width=200, color_system=None)
    renderer.on_final("hook denied")
    text = output.getvalue()
    assert "hook denied" in text
    assert "╭" in text


def test_console_reports_completion_rejection():
    renderer = ConsoleRenderer()
    output = StringIO()
    renderer._console = Console(file=output, width=200, color_system=None)
    renderer.on_completion_rejected([
        type("Issue", (), {"message": "计划未完成"})()
    ])
    text = output.getvalue()
    assert "完成检查未通过" in text
    assert "计划未完成" in text


def test_console_live_takes_a_snapshot_not_a_callback(monkeypatch):
    captured: dict = {}

    class FakeLive:
        def __init__(self, renderable=None, **kwargs):
            captured["renderable"] = renderable
            captured["get_renderable"] = kwargs.get("get_renderable")
            captured["updates"] = []

        def start(self):
            captured["started"] = True

        def update(self, renderable, *, refresh=False):
            captured["updates"].append(renderable)

        def stop(self):
            captured["stopped"] = True

    monkeypatch.setattr("wright.renderer.Live", FakeLive)
    renderer = ConsoleRenderer()
    renderer._can_live = lambda: True
    renderer.on_content_delta("hello")
    assert captured.get("started")
    assert captured["get_renderable"] is None
    assert captured["renderable"] is not None
    renderer.on_content_delta(" world")
    assert captured["updates"]


def test_legacy_checkpoint_derives_task_boundary(tmp_path):
    from wright.checkpoint import _deserialize_session, _serialize_session

    session = SessionState.create('old', tmp_path)
    for goal, usage in [('old', UsageRecord(90, 10, 100)), ('new', UsageRecord(10, 5, 15))]:
        session.begin_user_turn(goal)
        turn = session.record_assistant_turn(assistant_raw='done', parsed={}, route='final')
        session.record_usage_for_turn(turn, usage)
    payload = _serialize_session(session)
    del payload['session']['task_usage_start']
    restored = _deserialize_session(payload)
    assert restored.task_usage() == UsageRecord(10, 5, 15)


def test_runtime_event_summary_waits_for_memory_finalization(tmp_path):
    class LLM:
        context_limit = 10000

        def __call__(self, messages, **kwargs):
            yield UsageEvent({'prompt_tokens': 100, 'completion_tokens': 20})
            yield response(content='done')

    class Memory:
        def instructions(self):
            return ''

        def finalize_turn(self, session, answer, *, extract_semantic):
            self.usage_observer(UsageRecord(30, 10, 40))
            return {'episode_id': 'episode'}

    session = SessionState.create('task', tmp_path)
    session.begin_user_turn('task')
    renderer = Capture()
    agent = Agent(LLM(), [], session, renderer, memory=Memory())
    assert agent.run_runtime_event({
        'type': 'agent_completed',
        'task': {'root_turn_id': session.agent_root_turn_id},
    }) == 'done'
    assert renderer.summaries == [(130, 30, 160)]
