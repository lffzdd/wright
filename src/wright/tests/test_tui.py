import sys
from types import SimpleNamespace

import pytest

from wright.interaction import InteractionHub
from wright.renderer import SilentRenderer, collect_history_pairs
from wright.runtime import _trusted_mcp_config_paths, parse_cli_args
from wright.session_host import (
    dispatch_slash,
    process_session_event,
    slash_command_matches,
)
from wright.tools.base import ToolCall, ToolResult
from wright.tui.app import WrightTUI, _context_ring, require_interactive_tty
from wright.tui.renderer import TUIRenderer


def test_cli_ui_flag_defaults_to_tui(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright"])
    args = parse_cli_args()
    assert args.ui == "tui"


def test_tui_exposes_a_visible_stop_binding():
    assert any(binding.key == "ctrl+x" and binding.action == "stop_turn" for binding in WrightTUI.BINDINGS)


def test_cli_ui_flag_accepts_tui(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright", "--ui", "tui"])
    args = parse_cli_args()
    assert args.ui == "tui"


def test_cli_model_override(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright", "--model", "test-model"])
    assert parse_cli_args().model == "test-model"


def test_cli_ui_flag_accepts_web(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright", "--ui", "web"])
    args = parse_cli_args()
    assert args.ui == "web"
    assert args.web_port == 0
    assert args.web_capacity == 4


def test_project_mcp_is_not_trusted_by_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright"])
    assert parse_cli_args().trust_project_mcp is False

    monkeypatch.setattr(sys, "argv", ["wright", "--trust-project-mcp"])
    assert parse_cli_args().trust_project_mcp is True


def test_trusted_mcp_paths_exclude_project_config_until_opted_in(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("WRIGHT_TRUST_PROJECT_MCP", raising=False)
    workspace = tmp_path / "workspace"
    project_config = workspace / ".wright" / "mcp.json"
    project_config.parent.mkdir(parents=True)
    project_config.write_text("{}", encoding="utf-8")

    paths, ignored = _trusted_mcp_config_paths(
        workspace, SimpleNamespace(trust_project_mcp=False)
    )
    trusted_paths, trusted_ignored = _trusted_mcp_config_paths(
        workspace, SimpleNamespace(trust_project_mcp=True)
    )

    assert paths == [tmp_path / "home" / "mcp.json"]
    assert ignored == project_config
    assert trusted_paths[-1] == project_config
    assert trusted_ignored is None


def test_tui_requires_tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit, match="TUI"):
        require_interactive_tty()


def test_terminal_selects_iterm_compatibility_driver():
    from wright.terminal import configure_terminal

    environment = {"TERM_PROGRAM": "iTerm.app"}
    configure_terminal(environment, "darwin")
    assert environment["TEXTUAL_DISABLE_KITTY_KEY"] == "1"
    assert environment["TEXTUAL_DRIVER"] == "wright.tui.driver:ItermDriver"


def test_terminal_uses_default_driver_outside_iterm_and_keeps_user_override():
    from wright.terminal import configure_terminal

    default_environment: dict[str, str] = {}
    configure_terminal(default_environment, "darwin")
    assert "TEXTUAL_DISABLE_KITTY_KEY" not in default_environment
    assert "TEXTUAL_DRIVER" not in default_environment

    overridden_environment = {
        "LC_TERMINAL": "iTerm2",
        "TEXTUAL_DISABLE_KITTY_KEY": "0",
    }
    configure_terminal(overridden_environment, "darwin")
    assert overridden_environment["TEXTUAL_DISABLE_KITTY_KEY"] == "0"
    assert "TEXTUAL_DRIVER" not in overridden_environment


def test_iterm_driver_uses_xterm_shift_enter_protocol(monkeypatch):
    import textual.constants
    from textual._xterm_parser import XTermParser

    from wright.tui import driver

    assert driver._ENABLE_MODIFY_OTHER_KEYS == "\x1b[>4;1m"
    monkeypatch.setattr(textual.constants, "DISABLE_KITTY_KEY", True)
    parser = XTermParser()
    events = list(parser._sequence_to_key_events("\x1b[27;2;13~"))
    assert [event.key for event in events] == ["ctrl+j"]
    control_c = list(parser._sequence_to_key_events("\x03"))
    assert [event.key for event in control_c] == ["ctrl+c"]


def test_tui_renderer_streams_without_app():
    renderer = TUIRenderer()
    renderer.on_content_delta("hello ")
    renderer.on_content_delta("world")
    assert renderer.content == "hello world"
    renderer.on_final("hello world")
    assert renderer.final_answer == "hello world"


def test_tui_renderer_tracks_tools_without_app():
    renderer = TUIRenderer()
    call = ToolCall("list_directory", {"directory": "."}, "c1")
    renderer.on_tool_call(call)
    assert renderer.tools[0].name == "list_directory"
    assert renderer.tools[0].status == "planned"
    renderer.on_tool_phase(call, "awaiting_approval")
    assert renderer.tools[0].status == "awaiting_approval"
    renderer.on_tool_phase(call, "running")
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


def test_tui_usage_separates_request_task_and_context():
    renderer = TUIRenderer()
    renderer.on_usage(12_000, 800, 12_800, 128_000)
    renderer.on_usage_summary(20_000, 2_000, 22_000)
    assert renderer.request_usage == "12,000 in  ·  800 out"
    assert renderer.task_usage == "task total  ·  20,000 in  ·  2,000 out  ·  22,000 total"
    assert (renderer.context_tokens, renderer.context_limit) == (12_000, 128_000)


def test_context_ring_uses_current_session_context():
    assert _context_ring(None, 128_000) == (
        "○", "等待上下文", "Context window:\nWaiting for a context limit",
    )
    assert _context_ring(64_000, 128_000) == (
        "◑  50%",
        (
            "Context window:\n50% full\n64,000 / 128,000 tokens used\n\n"
            "Current session context"
        ),
        "normal",
    )


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


def test_slash_command_matches_and_completion_selection():
    from wright.tui.slash import SlashCompletion, tui_help_text

    assert [command.name for command in slash_command_matches("/hi")] == ["/history"]
    completion = SlashCompletion()
    completion.update("/")
    assert completion.selected is not None
    assert "/clear" in [command.name for command in completion.matches]
    completion.move(1)
    assert completion.selected is not None
    completion.update("/history ")
    assert completion.matches == ()
    assert "/model" in tui_help_text()


def test_session_control_preserves_cli_settings_and_model_choices():
    from argparse import Namespace

    from wright.tui.session_control import (
        SessionControlRequest,
        available_models,
        runtime_args_for_transition,
    )

    args = Namespace(resume="old", continue_latest=True, model="current", ui="tui")
    new_args = runtime_args_for_transition(args, SessionControlRequest.new())
    assert (new_args.resume, new_args.continue_latest, new_args.model) == (None, False, "current")
    resumed_args = runtime_args_for_transition(args, SessionControlRequest.resume("saved"))
    assert (resumed_args.resume, resumed_args.continue_latest) == ("saved", False)
    assert available_models("current", "fast,current,fast, careful ") == (
        "fast", "current", "careful",
    )


def test_process_model_name_uses_cli_then_openai_model(monkeypatch):
    from wright.tui.session_control import process_model_name

    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert process_model_name(None) == ""
    assert process_model_name("  ") == ""
    monkeypatch.setenv("OPENAI_MODEL", "  env-model  ")
    assert process_model_name(None) == "env-model"
    assert process_model_name(" cli-model ") == "cli-model"


def test_tui_model_switch_updates_the_active_llm_client():
    import threading

    from wright.tui.app import WrightTUI

    class ModelTestApp(WrightTUI):
        def _refresh_status(self) -> None:
            pass

    renderer = TUIRenderer()
    llm = SimpleNamespace(model="first")
    idle = threading.Event()
    idle.set()
    rt = SimpleNamespace(
        renderer=renderer,
        llm=llm,
        agent=SimpleNamespace(llm=llm, checkpoint_store=None),
        agent_idle=idle,
        session_state=SimpleNamespace(model_name="first"),
    )
    app = ModelTestApp(rt)
    app._set_model("second")
    assert llm.model == "second"
    assert rt.session_state.model_name == "second"
    assert "first  →  second" in renderer.notices[-1]


@pytest.mark.anyio
async def test_tui_slash_menu_navigates_and_completes():
    from wright.tui.app import MultilineComposer, WrightTUI

    class SlashTestApp(WrightTUI):
        def _seed_history(self) -> None:
            pass

        def _refresh_status(self) -> None:
            pass

        def _session_loop(self) -> None:
            pass

    rt = SimpleNamespace(
        renderer=TUIRenderer(),
        event_queue=SimpleNamespace(put_nowait=lambda _event: None),
    )
    app = SlashTestApp(rt)
    async with app.run_test() as pilot:
        composer = app.query_one(MultilineComposer)
        composer.load_text("/")
        await pilot.pause()
        suggestions = app.query_one("#slash-suggestions")
        assert suggestions.display is True
        await pilot.press("down", "tab")
        assert composer.text == "/status "
        assert suggestions.display is False


def test_help_and_status_slash_commands_render_notices():
    class Capture(SilentRenderer):
        def __init__(self) -> None:
            self.notices: list[str] = []

        def on_system_notice(self, text: str) -> None:
            self.notices.append(text)

    usage = SimpleNamespace(prompt_tokens=12, completion_tokens=3, total_tokens=15)
    session = SimpleNamespace(
        session_id="abc123",
        status="idle",
        workspace_dir="/tmp/project",
        task_usage=lambda: usage,
    )
    renderer = Capture()
    rt = SimpleNamespace(renderer=renderer, session_state=session)
    assert dispatch_slash("/help", rt) is True
    assert "/history" in renderer.notices[-1]
    assert dispatch_slash("/status", rt) is True
    assert "abc123" in renderer.notices[-1]


def test_process_session_event_exit_stops():
    assert process_session_event(SimpleNamespace(), "EXIT", None) is True


def test_collect_history_pairs_empty():
    session = SimpleNamespace(turns=[], message_records=[])
    assert collect_history_pairs(session) == []


def test_tool_title_formats_icons_and_arguments():
    from wright.tui.app import _tool_title
    from wright.tui.renderer import ToolView

    running = ToolView(key="1", name="execute_command", arguments={"command": "pytest -v"}, status="running")
    assert _tool_title(running) == "⏳ execute_command · $ pytest -v"

    done = ToolView(key="2", name="edit_file", arguments={"file": "src/app.py", "old_text": "a", "new_text": "b"}, status="done")
    assert _tool_title(done) == "✓ edit_file · src/app.py"

    err = ToolView(key="3", name="web_search", arguments={"query": "python"}, status="error")
    assert _tool_title(err) == '✗ web_search · "python"'


def test_tool_body_renders_edit_file_replacement():
    from rich.console import Group

    from wright.tui.app import _tool_body
    from wright.tui.renderer import ToolView

    tool = ToolView(
        key="1",
        name="edit_file",
        arguments={"file": "app.py", "old_text": "line1", "new_text": "line2"},
        status="done",
        result={"message": "File updated"},
    )
    body = _tool_body(tool)
    assert isinstance(body, Group)
    rendered_text = "".join(str(r) for r in body.renderables)
    assert "line1" in rendered_text
    assert "line2" in rendered_text


def test_assistant_block_markdown_and_draft():
    from rich.markdown import Markdown as RichMarkdown

    from wright.tui.app import _format_assistant_text

    assert _format_assistant_text("streaming text", draft=True) == "streaming text"
    rendered = _format_assistant_text("# Title\n```python\nprint(1)\n```", draft=False)
    assert isinstance(rendered, RichMarkdown)


@pytest.mark.anyio
async def test_assistant_block_mount_and_render():
    from textual.app import App, ComposeResult

    from wright.tui.app import AssistantBlock

    class DummyApp(App):
        def compose(self) -> ComposeResult:
            yield AssistantBlock("draft...", draft=True)
            yield AssistantBlock("# Final answer\n```python\nprint('ok')\n```", draft=False)

    app = DummyApp()
    async with app.run_test() as pilot:
        await pilot.pause()


@pytest.mark.anyio
async def test_multiline_composer_submits_on_enter_and_accepts_newline_shortcuts():
    from textual.app import App, ComposeResult

    from wright.tui.app import MultilineComposer

    class DummyApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.submitted: list[str] = []

        def compose(self) -> ComposeResult:
            yield MultilineComposer(id="composer")

        def on_multiline_composer_submitted(
            self, event: MultilineComposer.Submitted,
        ) -> None:
            self.submitted.append(event.value)

    app = DummyApp()
    async with app.run_test() as pilot:
        composer = app.query_one(MultilineComposer)
        composer.focus()
        await pilot.press(
            "h", "i", "shift+enter", "t", "h", "e", "r", "e",
            "ctrl+j", "l", "i", "n", "e", "3",
            "shift+enter", "l", "i", "n", "e", "4",
        )
        assert composer.text == "hi\nthere\nline3\nline4"
        assert composer.styles.height.value == 6
        await pilot.press("enter")
        assert app.submitted == ["hi\nthere\nline3\nline4"]
        assert composer.text == ""
        assert composer.styles.height.value == 5
