import threading
import time

from wright.application.session.interaction import RoutedPrompter
from wright.domain.policy import PermissionChoice, PermissionPrompt
from wright.interfaces.cli.input import CliInputController
from wright.interfaces.cli.prompter import ConsolePrompter
from wright.interfaces.interaction import InteractionHub


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
    assert hub.resolve(request.request_id, "allow_once") is True
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
        pending = len(hub.snapshot())
        if pending == 2 or time.time() >= deadline:
            break
        time.sleep(0.01)
    first = hub.poll()
    second = hub.poll()
    assert first is not None and second is not None
    assert hub.resolve(first.request_id, "allow_once") is True
    assert hub.resolve(second.request_id, "deny") is True
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
    assert hub.resolve(request.request_id, "allow_once") is True
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
        pending = len(hub.snapshot())
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

    hub = InteractionHub()
    answered: list[object] = []

    def agent() -> None:
        answered.append(hub.request("permission", {"tool_name": "write"}))

    thread = threading.Thread(target=agent)
    thread.start()
    deadline = time.time() + 2
    request = None
    while request is None and time.time() < deadline:
        request = hub.poll()
    assert request is not None
    class Service:
        def respond_interaction(self, _command_id, request_id, answer):
            return hub.resolve(request_id, answer)

    CliInputController(
        service=Service(),
        agent_idle=threading.Event(),
        hub=hub,
        prompter=Boom(renderer=object()),  # type: ignore[arg-type]
    ).fulfill(request)
    thread.join(timeout=2)
    assert answered == ["deny"]


def test_resolve_and_cancel_wake_the_waiter_once():
    hub = InteractionHub()
    ready = threading.Event()
    entered = threading.Event()
    release = threading.Event()

    def on_requested(*_args) -> None:
        ready.set()

    def on_resolved(*_args) -> None:
        entered.set()
        release.wait()

    hub.set_persistence(on_requested, on_resolved)
    answered: list[object] = []

    def agent() -> None:
        answered.append(hub.request("permission", {"tool_name": "write_file"}))

    thread = threading.Thread(target=agent)
    thread.start()
    assert ready.wait(timeout=2)
    request = hub.poll()
    assert request is not None
    resolver = threading.Thread(
        target=hub.resolve, args=(request.request_id, "allow_once")
    )
    resolver.start()
    assert entered.wait(timeout=2)
    # Persist is in progress. Cancel must not raise or deliver a second answer.
    hub.cancel_pending()
    release.set()
    resolver.join(timeout=2)
    thread.join(timeout=2)
    assert answered == ["allow_once"]
    assert hub.resolve(request.request_id, "deny") is False


def test_cancel_before_resolve_rejects_a_late_answer():
    hub = InteractionHub()
    ready = threading.Event()
    hub.set_persistence(lambda *_args: ready.set(), lambda *_args: None)
    answered: list[object] = []

    def agent() -> None:
        answered.append(hub.request("permission", {"tool_name": "write_file"}))

    thread = threading.Thread(target=agent)
    thread.start()
    assert ready.wait(timeout=2)
    request = hub.poll()
    assert request is not None
    hub.cancel_pending()
    thread.join(timeout=2)
    assert answered == ["deny"]
    assert hub.resolve(request.request_id, "allow_once") is False


def test_persist_failure_keeps_the_request_pending():
    hub = InteractionHub()
    ready = threading.Event()
    attempts = {"count": 0}

    def on_resolved(*_args) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("store down")

    hub.set_persistence(lambda *_args: ready.set(), on_resolved)
    answered: list[object] = []

    def agent() -> None:
        answered.append(hub.request("permission", {"tool_name": "write_file"}))

    thread = threading.Thread(target=agent)
    thread.start()
    assert ready.wait(timeout=2)
    request = hub.poll()
    assert request is not None
    assert hub.resolve(request.request_id, "allow_once") is False
    assert hub.has_pending()
    assert answered == []
    assert hub.resolve(request.request_id, "allow_once") is True
    thread.join(timeout=2)
    assert answered == ["allow_once"]


def test_input_coordinator_stops_while_busy_and_still_queues_text():
    idle = threading.Event()
    stopped: list[str] = []
    submitted: list[str] = []

    class Service:
        def stop_current(self, command_id=None):
            stopped.append(command_id or "")
            return {}

        def submit(self, prompt, command_id=None, **_kwargs):
            submitted.append(prompt)

        def close(self, wait_timeout=0):
            submitted.append("EXIT")

    class ScriptInput:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, prompt_session=None, queueing=False):
            self.calls += 1
            assert queueing is True or self.calls > 1
            if self.calls == 1:
                return "/stop"
            if self.calls == 2:
                return "hello"
            return None

    reader = ScriptInput()
    CliInputController(service=Service(), agent_idle=idle, read_main=reader).start()
    deadline = time.time() + 2
    while len(submitted) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert stopped
    assert submitted[0] == "hello"
    assert submitted[1] == "EXIT"
