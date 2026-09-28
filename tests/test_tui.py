import queue
import sys
import threading
import time
from argparse import Namespace
from types import SimpleNamespace

import pytest

from wright.application.composition.runtime import _trusted_mcp_config_paths
from wright.application.session.dispatch import (
    dispatch_slash,
    process_session_event,
    slash_command_matches,
)
from wright.application.session.interaction import RoutedPrompter
from wright.domain.model.tool import ToolCall, ToolResult
from wright.domain.policy import PermissionChoice, PermissionPrompt
from wright.interfaces.cli.args import parse_cli_args
from wright.interfaces.interaction import InteractionHub
from wright.interfaces.rendering.history import collect_history_pairs
from wright.interfaces.rendering.silent import SilentRenderer
from wright.interfaces.tui import app as tui_app_module
from wright.interfaces.tui.app import WrightTUI, require_interactive_tty
from wright.interfaces.tui.blocks import AssistantBlock
from wright.interfaces.tui.composer import MultilineComposer
from wright.interfaces.tui.format import (
    _context_ring,
    _format_assistant_text,
    _tool_body,
    _tool_title,
)
from wright.interfaces.tui.renderer import TUIRenderer
from wright.interfaces.tui.session_control import SessionControlRequest


def test_cli_ui_flag_defaults_to_tui(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wright"])
    args = parse_cli_args()
    assert args.ui == "tui"


def test_tui_exposes_a_visible_stop_binding():
    assert any(binding.key == "ctrl+x" and binding.action == "stop_turn" for binding in WrightTUI.BINDINGS)


def test_tui_keeps_application_host_alive_across_session_resume(tmp_path, monkeypatch):
    from wright.application.session.publisher import EventPublisher
    from wright.interfaces import interaction as interaction_module

    class Host:
        def __init__(self):
            self.workspace_dir = tmp_path
            self.closed = 0

        def close(self):
            self.closed += 1
            return True

        def has_active_work(self):
            return False

    class FakeApp:
        def __init__(self, _runtime, renderer=None, service=None):
            self.service = service or SimpleNamespace(closed=True, close=lambda **_kwargs: True)

        def run(self):
            if len(hubs) == 2:
                assert hubs[0].closed
                assert renderers[0] is not renderers[1]
                assert renderers[0].content == "session-one"
                assert renderers[1].content == ""
                result: dict[str, object] = {}

                def ask() -> None:
                    result["value"] = hubs[1].request("permission", {"tool_name": "shell"})

                thread = threading.Thread(target=ask)
                thread.start()
                pending = None
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    pending = hubs[1].poll()
                    if pending is not None:
                        break
                    time.sleep(0.01)
                assert pending is not None
                assert hubs[0].poll() is None
                assert hubs[1].resolve(pending.request_id, "allow_once")
                thread.join(2)
                assert result["value"] == "allow_once"
                assert hubs[0].request("ask_user", {"question": "old"}) is None
            elif len(renderers) == 1:
                renderers[0].content = "session-one"
            return transitions.pop(0)

    host = Host()
    transitions = [SessionControlRequest.resume("saved"), None]
    assembled_hosts = []
    hubs: list[interaction_module.InteractionHub] = []
    renderers: list[TUIRenderer] = []

    class RecordingHub(interaction_module.InteractionHub):
        def __init__(self) -> None:
            super().__init__()
            hubs.append(self)

    class RecordingRenderer(TUIRenderer):
        def __init__(self) -> None:
            super().__init__()
            renderers.append(self)

    class Saved:
        project_root = tmp_path.resolve()
        workspace_dir = tmp_path.resolve()
        environment = "local"
        base_commit = ""
        branch_name = ""

    def assemble(_config, *, session_id=None, application_host=None, interaction_broker=None, publisher=None, **_kwargs):
        assembled_hosts.append(application_host)
        chosen = application_host or host
        state = SimpleNamespace(
            session_id=session_id or "saved",
            lifecycle="open",
            model_name="test",
            environment="local",
            workspace_dir=tmp_path,
            project_root=tmp_path,
            base_commit="",
            branch_name="",
            user_goal="",
            message_records=[],
            turns=[],
            runs={},
            attachments={},
            current_run_status=lambda: "idle",
            active_run=lambda: None,
            attachment_records=lambda _ids: [],
            task_usage=lambda: SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            plan_manager=SimpleNamespace(has_plan=False, snapshot=lambda: {"steps": []}),
            request_context_tokens=0,
            context_tokens=0,
        )
        idle = threading.Event()
        idle.set()
        return SimpleNamespace(
            application_host=chosen,
            owns_application_host=True,
            session_state=state,
            agent=SimpleNamespace(checkpoint_store=None),
            publisher=publisher or EventPublisher(project_id="tui", session_id=state.session_id),
            interaction_broker=interaction_broker,
            event_queue=queue.Queue(),
            agent_idle=idle,
            cancellation_event=threading.Event(),
            autonomy_store=None,
            resumed=False,
            project_context=SimpleNamespace(execution_root=tmp_path.resolve()),
        )

    monkeypatch.setattr(tui_app_module, "require_interactive_tty", lambda: None)
    monkeypatch.setattr(tui_app_module, "assemble_runtime", assemble)
    monkeypatch.setattr(tui_app_module, "WrightTUI", FakeApp)
    monkeypatch.setattr(tui_app_module, "TUIRenderer", RecordingRenderer)
    monkeypatch.setattr(interaction_module, "InteractionHub", RecordingHub)
    monkeypatch.setattr(
        "wright.application.session.directory.shutdown_runtime",
        lambda _runtime: None,
    )
    monkeypatch.setattr(
        "wright.application.session.directory.FileSessionRepository.load",
        lambda _self, _session_id: Saved(),
    )
    monkeypatch.setattr(
        tui_app_module, "runtime_config_from_args",
        lambda args: SimpleNamespace(workspace=args.workspace, model=None),
    )
    monkeypatch.setattr(
        tui_app_module, "runtime_args_for_transition",
        lambda args, request: Namespace(workspace=args.workspace, resume=request.session_id),
    )

    tui_app_module.run_tui(Namespace(workspace=tmp_path, resume=None))

    assert assembled_hosts == [None, host]
    assert host.closed == 1
    assert len(hubs) == 2
    assert all(hub.closed for hub in hubs)
    assert len(renderers) == 2


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
    from wright.interfaces.tui.terminal import configure_terminal

    environment = {"TERM_PROGRAM": "iTerm.app"}
    configure_terminal(environment, "darwin")
    assert environment["TEXTUAL_DISABLE_KITTY_KEY"] == "1"
    assert environment["TEXTUAL_DRIVER"] == "wright.interfaces.tui.driver:ItermDriver"


def test_terminal_uses_default_driver_outside_iterm_and_keeps_user_override():
    from wright.interfaces.tui.terminal import configure_terminal

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

    from wright.interfaces.tui import driver

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


def test_tui_prompter_fail_closed_without_a_hub():
    prompter = RoutedPrompter(None)
    prompt = PermissionPrompt(
        "request", "write_file", "a.txt", "ask", (), targets=(),
        choices=(PermissionChoice("allow_once", "Allow once", "call", "none"),),
    )
    assert prompter.prompt_permission(prompt) == "deny"
    assert prompter.prompt_user("q") is None


def test_tui_prompter_uses_hub_off_collector_thread():
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
    assert hub.resolve(request.request_id, "allow_once") is True
    thread.join(timeout=2)
    assert answers == ["allow_once"]


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
    from wright.application.session.publisher import EventPublisher
    from wright.interfaces.rendering.subscriber import RendererEventSubscriber

    class Capture(SilentRenderer):
        def __init__(self) -> None:
            self.notices: list[str] = []

        def on_system_notice(self, text: str) -> None:
            self.notices.append(text)

    renderer = Capture()
    publisher = EventPublisher(project_id="project", session_id="session")
    publisher.add_listener(RendererEventSubscriber(renderer))
    rt = SimpleNamespace(event_renderer=renderer, publisher=publisher, session_state=None)
    assert dispatch_slash("/history", rt) is True
    assert renderer.notices
    assert "滚动" in renderer.notices[0]


def test_slash_command_matches_and_completion_selection():
    from wright.interfaces.tui.slash import SlashCompletion, tui_help_text

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

    from wright.interfaces.tui.session_control import (
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
    from wright.interfaces.tui.session_control import process_model_name

    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert process_model_name(None) == ""
    assert process_model_name("  ") == ""
    monkeypatch.setenv("OPENAI_MODEL", "  env-model  ")
    assert process_model_name(None) == "env-model"
    assert process_model_name(" cli-model ") == "cli-model"


def test_tui_model_switch_updates_the_active_llm_client():
    import threading

    from wright.interfaces.tui.app import WrightTUI

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
    from wright.interfaces.tui.app import WrightTUI

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
    rt = SimpleNamespace(renderer=renderer, event_renderer=renderer, session_state=session)
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
    from wright.interfaces.tui.view_models import ToolView

    running = ToolView(key="1", name="execute_command", arguments={"command": "pytest -v"}, status="running")
    assert _tool_title(running) == "⏳ execute_command · $ pytest -v"

    done = ToolView(key="2", name="edit_file", arguments={"file": "src/app.py", "old_text": "a", "new_text": "b"}, status="done")
    assert _tool_title(done) == "✓ edit_file · src/app.py"

    err = ToolView(key="3", name="web_search", arguments={"query": "python"}, status="error")
    assert _tool_title(err) == '✗ web_search · "python"'


def test_tool_body_renders_edit_file_replacement():
    from rich.console import Group

    from wright.interfaces.tui.view_models import ToolView

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

    assert _format_assistant_text("streaming text", draft=True) == "streaming text"
    rendered = _format_assistant_text("# Title\n```python\nprint(1)\n```", draft=False)
    assert isinstance(rendered, RichMarkdown)


@pytest.mark.anyio
async def test_assistant_block_mount_and_render():
    from textual.app import App, ComposeResult

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


@pytest.mark.anyio
async def test_permission_modal_shows_scope_preview_and_escape_denies():
    from textual.app import App
    from textual.widgets import Button, Static

    from wright.interfaces.tui.modals import PermissionModal

    class Host(App):
        def __init__(self) -> None:
            super().__init__()
            self.result: str | None = None

        def on_mount(self) -> None:
            def done(choice: str | None) -> None:
                self.result = choice

            self.push_screen(
                PermissionModal(
                    tool_name="write_file",
                    subject="nested/a.txt",
                    risk_flags=("writes_files",),
                    reason="needs approval <script>",
                    grant_summary="Adds file root " + ("nested/" * 40),
                    preview="new line <b>literal</b>",
                    choices=(
                        {
                            "id": "allow_once",
                            "label": "Allow once",
                            "scope": "This invocation only",
                            "persistence": "No save",
                        },
                        {
                            "id": "deny",
                            "label": "Deny",
                            "scope": "No execution",
                            "persistence": "No save",
                        },
                    ),
                ),
                done,
            )

    app = Host()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        rendered = " ".join(
            str(widget.render())
            for widget in (*app.screen.query(Static), *app.screen.query(Button))
        )
        assert "needs approval <script>" in rendered
        assert "Adds file root" in rendered
        assert "new line <b>literal</b>" in rendered
        assert "This invocation only" in rendered
        await pilot.press("escape")
        await pilot.pause()
        assert app.result == "deny"
