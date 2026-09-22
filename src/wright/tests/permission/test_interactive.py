from ...domain.session import Session
from ...engine.executor import ToolExecutor
from ...permission import (
    InteractiveApprovalHandler,
    PermissionResolver,
)
from ...renderer import SilentRenderer
from ...tools.base import ToolCall
from ...tools.file_tools import edit_file_tool, read_file_tool, write_file_tool


class _MockRenderer(SilentRenderer):
    def __init__(self, *choices: str):
        super().__init__()
        self.choices = list(choices)
        self.prompts = []
        self.phases = []

    def on_tool_phase(self, tool_call, phase: str) -> None:
        self.phases.append((tool_call.id, phase))

    def prompt_permission(self, prompt):
        self.prompts.append(prompt)
        return self.choices.pop(0) if self.choices else "deny"


def _executor(session: Session, renderer: _MockRenderer) -> ToolExecutor:
    resolver = PermissionResolver(
        approval_handler=InteractiveApprovalHandler(renderer)
    )
    return ToolExecutor(
        {
            write_file_tool.name: write_file_tool,
            read_file_tool.name: read_file_tool,
            edit_file_tool.name: edit_file_tool,
        },
        workspace_dir=session.workspace_dir,
        cwd_provider=session.get_cwd,
        session=session,
        permission_resolver=resolver,
    )


def _run(executor: ToolExecutor, call: ToolCall):
    return executor.execute([call])[0].result


def test_structured_prompt_uses_fixed_choice_ids(tmp_path):
    session = Session.create("permission", tmp_path)
    renderer = _MockRenderer("allow_once")
    result = _run(
        _executor(session, renderer),
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1"),
    )
    assert result.ok
    assert [choice.id for choice in renderer.prompts[0].choices] == [
        "allow_once",
        "allow_session_rule",
        "allow_persistent_rule",
        "deny",
    ]
    assert renderer.prompts[0].targets == (str((tmp_path / "a.txt").resolve()),)
    assert renderer.phases == [("c1", "awaiting_approval")]


def test_outside_directory_choices_update_session_only_after_allow(tmp_path):
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    session = Session.create("outside grant", workspace)
    renderer = _MockRenderer("allow_session_directory")
    written = _run(
        _executor(session, renderer),
        ToolCall(
            "write_file",
            {"file": str(extra / "a.txt"), "content": "old line\n"},
            "c1",
        ),
    )
    assert written.ok
    assert session.working_directories_snapshot() == (extra.resolve(),)
    assert [choice.id for choice in renderer.prompts[0].choices] == [
        "allow_once",
        "allow_session_directory",
        "allow_persistent_directory",
        "deny",
    ]
    assert _run(
        _executor(session, _MockRenderer()),
        ToolCall("read_file", {"file": str(extra / "a.txt")}, "c2"),
    ).ok


def test_invalid_choice_is_fail_closed(tmp_path):
    session = Session.create("permission", tmp_path)
    result = _run(
        _executor(session, _MockRenderer("y")),
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1"),
    )
    assert not result.ok


def test_approval_handler_cannot_persist_a_rule_or_change_tool_policy(tmp_path):
    session = Session.create("permission", tmp_path)
    renderer = _MockRenderer("allow_once")
    result = _run(
        _executor(session, renderer),
        ToolCall("write_file", {"file": "a.txt", "content": "hello"}, "c1"),
    )
    assert result.ok
    assert not hasattr(renderer, "on_remember")


def test_session_rule_choice_is_scoped_and_reused_by_the_same_resolver(tmp_path):
    session = Session.create("permission", tmp_path)
    renderer = _MockRenderer("allow_session_rule")
    executor = _executor(session, renderer)

    first = _run(
        executor,
        ToolCall(
            "write_file",
            {"file": "nested/a.txt", "content": "a"},
            "c1",
        ),
    )
    second = _run(
        executor,
        ToolCall(
            "write_file",
            {"file": "nested/b.txt", "content": "b"},
            "c2",
        ),
    )

    assert first.ok and second.ok
    assert len(renderer.prompts) == 1
    assert (tmp_path / "nested/b.txt").read_text(encoding="utf-8") == "b"


def test_persistent_rule_choice_is_returned_to_the_commit_adapter(tmp_path):
    session = Session.create("permission", tmp_path)
    renderer = _MockRenderer("allow_persistent_rule")
    changes = []
    resolver = PermissionResolver(
        approval_handler=InteractiveApprovalHandler(renderer)
    )
    executor = ToolExecutor(
        {
            write_file_tool.name: write_file_tool,
            read_file_tool.name: read_file_tool,
            edit_file_tool.name: edit_file_tool,
        },
        workspace_dir=session.workspace_dir,
        cwd_provider=session.get_cwd,
        session=session,
        permission_resolver=resolver,
        authorization_commit=changes.append,
    )

    result = _run(
        executor,
        ToolCall(
            "write_file",
            {"file": "nested/a.txt", "content": "a"},
            "c1",
        ),
    )

    assert result.ok
    assert changes[0].persistent_rules == ("write_file(nested/*)",)
    assert changes[0].session_directories == ()


def test_cancelled_permission_is_denied(tmp_path):
    session = Session.create("permission", tmp_path)
    result = _run(
        _executor(session, _MockRenderer("deny")),
        ToolCall("edit_file", {"file": "a.txt", "old_text": "a", "new_text": "b"}, "c1"),
    )
    assert not result.ok


def test_empty_or_legacy_permission_answers_fail_closed(tmp_path):
    for choice in ("", "y", "a", "unknown"):
        session = Session.create("permission", tmp_path)
        result = _run(
            _executor(session, _MockRenderer(choice)),
            ToolCall("write_file", {"file": "a.txt", "content": "x"}, choice or "c1"),
        )
        assert not result.ok
