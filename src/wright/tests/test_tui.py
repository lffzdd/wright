import sys
from types import SimpleNamespace

import pytest

from wright.interaction import InteractionHub
from wright.renderer import SilentRenderer, collect_history_pairs
from wright.runtime import parse_cli_args
from wright.session_host import dispatch_slash, process_session_event
from wright.tools.base import ToolCall, ToolResult
from wright.tui.app import require_interactive_tty
from wright.tui.renderer import TUIRenderer


def test_cli_ui_flag_defaults_to_cli(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright"])
    args = parse_cli_args()
    assert args.ui == "cli"


def test_cli_ui_flag_accepts_tui(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright", "--ui", "tui"])
    args = parse_cli_args()
    assert args.ui == "tui"


def test_cli_ui_flag_rejects_unknown(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright", "--ui", "web"])
    with pytest.raises(SystemExit):
        parse_cli_args()


def test_tui_requires_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit, match="TUI"):
        require_interactive_tty()


def test_tui_renderer_streams_without_app():
    renderer = TUIRenderer()
    renderer.on_content_delta("hello ")
    renderer.on_content_delta("world")
    assert renderer.content == "hello world"
    renderer.on_final("hello world")
    assert renderer.final_answer == "hello world"


def test_tui_renderer_tracks_tools_without_app():
    renderer = TUIRenderer()
    call = ToolCall("list_files", {"directory": "."}, "c1")
    renderer.on_tool_call(call)
    assert renderer.tools[0].name == "list_files"
    assert renderer.tools[0].status == "running"
    renderer.on_tool_result(call, ToolResult.success({"files": ["a.py"]}))
    assert renderer.tools[0].status == "done"
    assert renderer.tools[0].result == {"files": ["a.py"]}


def test_tui_renderer_permission_fail_closed_without_app():
    renderer = TUIRenderer()
    assert renderer.prompt_permission("write_file", "a.txt", "无", "ask", True) == "n"
    assert renderer.prompt_user("q") is None


def test_tui_renderer_permission_uses_hub_off_collector_thread():
    hub = InteractionHub()
    renderer = TUIRenderer()
    renderer.bind_interaction(hub)
    answers: list[str] = []

    def agent() -> None:
        answers.append(
            renderer.prompt_permission("write_file", "file=a.txt", "无", "ask", True)
        )

    import threading
    import time

    thread = threading.Thread(target=agent)
    thread.start()
    deadline = time.time() + 2
    while not hub.has_pending() and time.time() < deadline:
        time.sleep(0.01)
    request = hub.poll()
    assert request is not None
    assert request.kind == "permission"
    request.reply.put("y")
    thread.join(timeout=2)
    assert answers == ["y"]


def test_tui_renderer_completion_rejected_is_a_notice():
    renderer = TUIRenderer()
    renderer.on_content_delta("draft")
    renderer.on_completion_rejected([type("Issue", (), {"message": "计划未完成"})()])
    assert renderer.content == ""
    assert any("完成检查未通过" in item for item in renderer.notices)
    assert any("计划未完成" in item for item in renderer.notices)


def test_tui_attach_flag_tracks_lifecycle():
    renderer = TUIRenderer()
    assert not TUIRenderer.is_active()
    renderer.attach(object())
    try:
        assert TUIRenderer.is_active()
    finally:
        renderer.detach()
    assert not TUIRenderer.is_active()


def test_history_slash_on_non_console_renderer():
    class Capture(SilentRenderer):
        def __init__(self) -> None:
            self.notices: list[str] = []

        def on_system_notice(self, text: str) -> None:
            self.notices.append(text)

    rt = SimpleNamespace(renderer=Capture(), session_state=None)
    assert dispatch_slash("/history", rt) is True
    assert rt.renderer.notices
    assert "滚动" in rt.renderer.notices[0]


def test_process_session_event_exit_stops():
    assert process_session_event(SimpleNamespace(), "EXIT", None) is True


def test_collect_history_pairs_empty():
    session = SimpleNamespace(turns=[], message_records=[])
    assert collect_history_pairs(session) == []
