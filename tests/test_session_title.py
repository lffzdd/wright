import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_session_service import _runtime
from wright.application.session.service import SessionService
from wright.application.session.title import (
    TITLE_MAX_CHARS,
    clean_title,
    generate_title,
)
from wright.domain.model.llm.events import ContentDone
from wright.domain.model.session.session import INTERACTIVE_SESSION_LABEL


class FakeTitleModel:
    def __init__(self, reply: str = '{"title": "重构权限审批流程"}', *, gate: threading.Event | None = None):
        self.reply = reply
        self.gate = gate
        self.calls: list[list[dict]] = []

    def __call__(self, messages, tools=None, model=None, **kwargs):
        self.calls.append(list(messages))
        if self.gate is not None:
            self.gate.wait(2)
        yield ContentDone(content=self.reply)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"title": "Fix flaky login test"}', "Fix flaky login test"),
        ('{"title": "  \\"整理 Web 样式\\"。 "}', "整理 Web 样式"),
        ("Title: Add dark mode toggle.", "Add dark mode toggle"),
        ('{"title": ""}', ""),
        ("[1, 2]", ""),
    ],
)
def test_clean_title_keeps_one_short_line(raw, expected):
    assert clean_title(raw) == expected


def test_clean_title_truncates_long_titles():
    title = clean_title('{"title": "' + "word " * 40 + '"}')
    assert len(title) == TITLE_MAX_CHARS
    assert title.endswith("…")


def test_generate_title_returns_empty_when_the_model_fails():
    def broken(messages, **kwargs):
        raise RuntimeError("offline")
        yield  # pragma: no cover

    assert generate_title(broken, "do something", "done") == ""
    assert generate_title(FakeTitleModel(), "   ", "done") == ""


def _titled_runtime(model, *, label=""):
    runtime = _runtime()
    state = runtime.session_state
    state.session_label = label
    state.agent_task_id = None
    state.runs = {"r1": SimpleNamespace(source="user_input", goal="用 list_directory 看一下 web/src 目录有哪些文件")}
    state.wire_messages = lambda: [
        {"role": "user", "content": "用 list_directory 看一下 web/src 目录有哪些文件"},
        {"role": "assistant", "content": "web/src 有 24 个条目。"},
    ]
    runtime.title_llm = model
    return runtime


def _run_turns(runtime, *prompts):
    service = SessionService(runtime, event_processor=lambda _rt, kind, _payload: kind == "EXIT", shutdown=lambda _: None)
    service.start()
    for index, prompt in enumerate(prompts):
        service.submit(prompt, f"command-{index}")
    return service


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_first_finished_turn_names_an_unlabeled_session_once():
    model = FakeTitleModel()
    runtime = _titled_runtime(model, label=INTERACTIVE_SESSION_LABEL)
    service = _run_turns(runtime, "first", "second")
    assert _wait_for(lambda: runtime.session_state.session_label == "重构权限审批流程")
    time.sleep(0.05)
    assert len(model.calls) == 1
    prompt = model.calls[0][1]["content"]
    assert "web/src" in prompt and "24 个条目" in prompt
    assert service.close(wait_timeout=1)


def test_a_rename_during_generation_wins():
    gate = threading.Event()
    model = FakeTitleModel(gate=gate)
    runtime = _titled_runtime(model)
    service = _run_turns(runtime, "first")
    assert _wait_for(lambda: len(model.calls) == 1)
    runtime.session_state.session_label = "My own name"
    gate.set()
    time.sleep(0.1)
    assert runtime.session_state.session_label == "My own name"
    assert service.close(wait_timeout=1)


def test_labeled_or_disabled_sessions_are_left_alone():
    model = FakeTitleModel()
    labeled = _titled_runtime(model, label="Already named")
    service = _run_turns(labeled, "first")
    disabled = _titled_runtime(None)
    other = _run_turns(disabled, "first")
    time.sleep(0.1)
    assert model.calls == []
    assert labeled.session_state.session_label == "Already named"
    assert disabled.session_state.session_label == ""
    assert service.close(wait_timeout=1)
    assert other.close(wait_timeout=1)


def test_runtime_assembly_honours_the_switch(monkeypatch):
    from wright.application.composition.runtime import _auto_title_disabled

    monkeypatch.setenv("WRIGHT_AUTO_TITLE", "0")
    assert _auto_title_disabled()
    monkeypatch.delenv("WRIGHT_AUTO_TITLE")
    assert not _auto_title_disabled()
