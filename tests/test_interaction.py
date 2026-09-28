import queue
import threading
import time

import pytest

from wright.application.session.interaction import RoutedPrompter
from wright.domain.policy import PermissionChoice, PermissionPrompt
from wright.interfaces.cli.input import CliInputController
from wright.interfaces.cli.prompter import ConsolePrompter
from wright.interfaces.interaction import InteractionHub, InteractionRequest


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
    request.reply.put("allow_once")
    thread.join(timeout=2)
    assert result == ["allow_once"]


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
    first.reply.put("allow_once")
    second.reply.put("deny")
    for thread in threads:
        thread.join(timeout=2)
    assert sorted(results) == ["a:allow_once", "b:deny"] or sorted(results) == ["a:deny", "b:allow_once"]


def test_console_permission_uses_hub_off_collector_thread():
    hub = InteractionHub()
    prompter = RoutedPrompter(hub)
    answers: list[str] = []
    permission_prompt = PermissionPrompt(
        "request", "write_file", "file=a.txt", "ask", (), targets=(),
        choices=(PermissionChoice("allow_once", "Allow once", "call", "none"),
                 PermissionChoice("deny", "Deny", "none", "none")),
    )

    def agent() -> None:
        answers.append(prompter.prompt_permission(permission_prompt))

    thread = threading.Thread(target=agent)
    thread.start()
    deadline = time.time() + 2
    while not hub.has_pending() and time.time() < deadline:
        time.sleep(0.01)
    request = hub.poll()
    assert request is not None
    assert request.kind == "permission"
    assert request.payload["tool_name"] == "write_file"
    request.reply.put("allow_once")
    thread.join(timeout=2)
    assert answers == ["allow_once"]


def test_hub_close_and_cancel_release_waiting_agents():
    hub = InteractionHub()
    denied: list[object] = []
    cancelled: list[object] = []

    def ask_permission() -> None:
        denied.append(hub.request("permission", {"tool_name": "write"}))

    def ask_user() -> None:
        cancelled.append(hub.request("ask_user", {"question": "?"}))

    permission_thread = threading.Thread(target=ask_permission)
    user_thread = threading.Thread(target=ask_user)
    permission_thread.start()
    user_thread.start()
    deadline = time.time() + 2
    while hub.has_pending() is False or time.time() >= deadline:
        if time.time() >= deadline:
            break
        time.sleep(0.01)
    # Wait until both requests are visible.
    deadline = time.time() + 2
    while True:
        with hub._lock:
            pending = len(hub._requests)
        if pending == 2 or time.time() >= deadline:
            break
        time.sleep(0.01)
    hub.cancel_pending()
    permission_thread.join(timeout=2)
    user_thread.join(timeout=2)
    assert denied == ["deny"]
    assert cancelled == [None]

    blocked: list[object] = []
    thread = threading.Thread(target=lambda: blocked.append(hub.request("ask_user", {"question": "later"})))
    thread.start()
    deadline = time.time() + 2
    while not hub.has_pending() and time.time() < deadline:
        time.sleep(0.01)
    hub.close()
    thread.join(timeout=2)
    assert blocked == [None]


def test_fulfill_exception_unblocks_the_waiting_agent():
    class Boom(ConsolePrompter):
        def collect_permission(self, payload):
            raise RuntimeError("terminal failed")

    answered: list[object] = []
    request = InteractionRequest(kind="permission", payload={"tool_name": "write"})

    def agent() -> None:
        answered.append(request.reply.get())

    thread = threading.Thread(target=agent)
    thread.start()
    CliInputController(
        service=object(),
        agent_idle=threading.Event(),
        prompter=Boom(renderer=object()),  # type: ignore[arg-type]
    ).fulfill(request)
    thread.join(timeout=2)
    assert answered == ["deny"]


def test_input_reader_holds_main_prompt_until_idle():
    idle = threading.Event()
    events: queue.Queue[tuple[str, object]] = queue.Queue()

    class ScriptInput:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, prompt_session=None, queueing=False):
            self.calls += 1
            if self.calls == 1:
                return "hello"
            return None

    reader = ScriptInput()
    CliInputController(service=events, agent_idle=idle, read_main=reader).start()
    with pytest.raises(queue.Empty):
        events.get(timeout=0.3)
    assert reader.calls == 0
    idle.set()
    kind, payload = events.get(timeout=2)
    assert kind == "USER_INPUT"
    assert payload == "hello"
    with pytest.raises(queue.Empty):
        events.get(timeout=0.2)
    idle.set()
    kind, payload = events.get(timeout=2)
    assert kind == "EXIT"
