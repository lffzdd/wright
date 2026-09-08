import queue
import threading
import time

import pytest

from wright.interaction import InteractionHub
from wright.renderer import ConsoleRenderer, SilentRenderer
from wright.repl import _start_input_reader


def test_hub_delivers_reply_from_collector():
    hub = InteractionHub()
    result = []

    def agent() -> None:
        result.append(hub.request("permission", {"tool_name": "write_file"}))

    thread = threading.Thread(target=agent)
    thread.start()
    deadline = time.time() + 2
    while not hub.has_pending() and time.time() < deadline:
        time.sleep(0.01)
    request = hub.poll()
    assert request is not None
    assert request.payload["tool_name"] == "write_file"
    request.reply.put("y")
    thread.join(timeout=2)
    assert result == ["y"]


def test_hub_serializes_two_agent_threads():
    hub = InteractionHub()
    results: list[str] = []

    def ask(tag: str) -> None:
        results.append(f"{tag}:{hub.request('permission', {'tag': tag})}")

    threads = [
        threading.Thread(target=ask, args=("a",)),
        threading.Thread(target=ask, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    deadline = time.time() + 2
    while True:
        with hub._lock:
            pending = len(hub._pending)
        if pending == 2 or time.time() >= deadline:
            break
        time.sleep(0.01)
    first = hub.poll()
    second = hub.poll()
    assert first is not None and second is not None
    first.reply.put("y")
    second.reply.put("n")
    for thread in threads:
        thread.join(timeout=2)
    assert sorted(results) == ["a:y", "b:n"] or sorted(results) == ["a:n", "b:y"]


def test_console_permission_uses_hub_off_collector_thread():
    hub = InteractionHub()
    renderer = ConsoleRenderer()
    renderer.bind_interaction(hub)
    answers: list[str] = []

    def agent() -> None:
        answers.append(
            renderer.prompt_permission("write_file", "file=a.txt", "无", "ask", True)
        )

    thread = threading.Thread(target=agent)
    thread.start()
    deadline = time.time() + 2
    while not hub.has_pending() and time.time() < deadline:
        time.sleep(0.01)
    request = hub.poll()
    assert request is not None
    assert request.kind == "permission"
    assert request.payload["tool_name"] == "write_file"
    request.reply.put("y")
    thread.join(timeout=2)
    assert answers == ["y"]


def test_input_reader_holds_main_prompt_until_idle():
    idle = threading.Event()
    events: queue.Queue[tuple[str, object]] = queue.Queue()

    class ScriptRenderer(SilentRenderer):
        def __init__(self) -> None:
            self.calls = 0

        def prompt_main_input(self, prompt_session=None, *, queueing=False):
            self.calls += 1
            if self.calls == 1:
                return "hello"
            return None

    renderer = ScriptRenderer()
    _start_input_reader(renderer, events, idle)
    with pytest.raises(queue.Empty):
        events.get(timeout=0.3)
    assert renderer.calls == 0
    idle.set()
    kind, payload = events.get(timeout=2)
    assert kind == "USER_INPUT"
    assert payload == "hello"
    with pytest.raises(queue.Empty):
        events.get(timeout=0.2)
    idle.set()
    kind, payload = events.get(timeout=2)
    assert kind == "EXIT"
