import threading
from pathlib import Path

from tests.responses import event, response
from wright.application.agent import make_spawn_agent_tool
from wright.application.command.execution import CommandExecution
from wright.application.execution.identity import bind_identity
from wright.application.session.live_resources import RuntimeResources
from wright.application.tool_execution.capabilities import assemble_tool_capabilities
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.application.tool_execution.runtime import tool_runtime_for_session
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall, ToolResult
from wright.domain.policy import (
    AccessTarget,
    PermissionPolicy,
    PermissionResolver,
    PermissionResponse,
    PermissionSettings,
    ToolAccess,
)
from wright.infrastructure.config import (
    append_additional_directory,
    append_allow_rule,
    load_permission_settings,
)
from wright.infrastructure.persistence.session.repository import FileSessionRepository
from wright.infrastructure.runtime import LocalExecutionBackend
from wright.infrastructure.tools.base import Tool
from wright.infrastructure.tools.command import execute_command_tool
from wright.infrastructure.tools.file import grep_tool, write_file_tool


def _mode_tool(call, *, safe_mode: str = "read") -> Tool:
    return Tool(
        name="mode_tool",
        description="",
        parameters={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["read", "write"]},
                "token": {"type": "string"},
            },
            "required": ["mode", "token"],
            "additionalProperties": False,
        },
        call=call,
        access_descriptor=lambda args: ToolAccess(
            frozenset({"file_write"}),
            (
                AccessTarget(
                    "file", "approved.txt", "file_write", kind="file"
                ),
            ),
            subject="approved.txt",
        ),
        is_concurrency_safe=lambda args: args["mode"] == safe_mode,
    )


def test_approval_rewrites_are_prepared_before_any_execution(tmp_path):
    owner = threading.current_thread()
    approval_threads = []
    approval_ids = []
    execution_started = threading.Event()
    active = 0
    peak = 0
    lock = threading.Lock()

    def call(_args, _runtime):
        nonlocal active, peak
        execution_started.set()
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            return ToolResult.success()
        finally:
            with lock:
                active -= 1

    def approve(request):
        approval_threads.append(threading.current_thread())
        approval_ids.append(request.tool_call.id)
        if request.tool_call.id == "c2":
            assert not execution_started.is_set()
        return PermissionResponse("allow_once", {"mode": "write", "token": "ok"})

    tool = _mode_tool(call)
    resolver = PermissionResolver(approval_handler=approve)
    outcomes = ToolDispatchService(
        {tool.name: tool},
        assemble_tool_capabilities(None, None, None, workspace_dir=tmp_path),
        permission_resolver=resolver,
    ).execute([
        ToolCall(tool.name, {"mode": "read", "token": "one"}, "c1"),
        ToolCall(tool.name, {"mode": "read", "token": "two"}, "c2"),
    ])

    assert all(item.result.ok for item in outcomes)
    assert approval_ids == ["c1", "c2"]
    assert approval_threads == [owner, owner]
    assert peak == 1


def test_final_safe_calls_overlap_after_sequential_preparation(tmp_path):
    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def call(_args, _runtime):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait(timeout=2)
            return ToolResult.success()
        finally:
            with lock:
                active -= 1

    def approve(request):
        return PermissionResponse(
            "allow_once",
            {"mode": "read", "token": request.arguments["token"]},
        )

    tool = _mode_tool(call)
    outcomes = ToolDispatchService(
        {tool.name: tool},
        assemble_tool_capabilities(None, None, None, workspace_dir=tmp_path),
        permission_resolver=PermissionResolver(approval_handler=approve),
    ).execute([
        ToolCall(tool.name, {"mode": "read", "token": "one"}, "c1"),
        ToolCall(tool.name, {"mode": "read", "token": "two"}, "c2"),
    ])

    assert all(item.result.ok for item in outcomes)
    assert peak == 2


def test_denied_rewrite_does_not_commit_or_execute(tmp_path):
    called = []

    def call(_args, _runtime):
        called.append(True)
        return ToolResult.success()

    tool = _mode_tool(call)
    session = Session.create("deny", tmp_path)
    resolver = PermissionResolver(
        approval_handler=lambda _request: PermissionResponse(
            "deny", {"mode": "write", "token": "outside"}
        )
    )
    outcome = ToolDispatchService(
        {tool.name: tool},
        assemble_tool_capabilities(session, None, None, workspace_dir=tmp_path),
        session=session,
        permission_resolver=resolver,
        authorization_commit=lambda _change: called.append("commit"),
    ).execute([ToolCall(tool.name, {"mode": "read", "token": "one"}, "c1")])[0]

    assert not outcome.result.ok
    assert called == []
    assert session.working_directories_snapshot() == ()


def test_child_uses_fixed_local_cwd_and_own_scope_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    later = tmp_path / "later"
    workspace.mkdir()
    nested.mkdir()
    later.mkdir()
    (nested / "same.txt").write_text("nested", encoding="utf-8")

    parent = Session.create("parent", workspace)
    parent.begin_user_turn("parent")
    parent.set_cwd(nested)
    observed = {}

    def inspect(_args, runtime):
        assert runtime.execution is not None
        observed["path"] = runtime.execution.resolve_path("same.txt").value
        observed["scope_before"] = runtime.access_scope.additional
        parent.add_working_directory(later)
        observed["scope_after"] = runtime.access_scope.additional
        return ToolResult.success()

    inspect_tool = Tool(
        "inspect_child", "", {
            "type": "object", "properties": {}, "additionalProperties": False,
        },
        inspect,
        access_descriptor=lambda _args: ToolAccess(
            frozenset({"file_read"}),
            (AccessTarget("file", "same.txt", "file_read", kind="file"),),
            subject="same.txt",
        ),
        required_capabilities=frozenset({"execution"}),
    )

    class ChildLLM:
        context_limit = 128_000

        def __init__(self):
            self.script = [
                response(content=None, calls=[
                    {"name": "inspect_child", "arguments": {}},
                ]),
                response(content="child done", calls=[]),
            ]

        def __call__(self, _messages, **_kwargs):
            yield event(self.script.pop(0))

    spawn = make_spawn_agent_tool(
        ChildLLM(), [inspect_tool], max_depth=1, render_subagents=False,
        permission_resolver=PermissionResolver(
            PermissionPolicy(PermissionSettings(mode="bypass"))
        ),
    )
    result = spawn.call(
        {"task": "inspect nested cwd"},
        tool_runtime_for_session(
            parent,
            tool_name="spawn_agent",
            tool_call_id="spawn",
            workspace_dir=workspace,
            cwd_provider=parent.get_cwd,
        ),
    )

    assert result.ok
    assert observed["path"] == str((nested / "same.txt").resolve())
    assert observed["scope_before"] == ()
    assert observed["scope_after"] == ()
    assert parent.get_cwd() == nested.resolve()
    assert parent.working_directories_snapshot() == (later.resolve(),)


def test_one_executor_refreshes_scope_for_shell_and_child(tmp_path):
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    (extra / "marker.txt").write_text("marker", encoding="utf-8")
    parent = Session.create("continuous", workspace)
    parent.begin_user_turn("continuous")
    observed = {}

    def inspect(_args, runtime):
        assert runtime.execution is not None
        observed["cwd"] = runtime.execution.cwd().value
        observed["additional"] = runtime.access_scope.additional
        observed["marker"] = runtime.execution.read_text(
            runtime.execution.resolve_path("marker.txt"), encoding="utf-8"
        )
        return ToolResult.success()

    inspect_tool = Tool(
        "inspect_child_scope", "", {
            "type": "object", "properties": {}, "additionalProperties": False,
        },
        inspect,
        access_descriptor=lambda _args: ToolAccess(
            frozenset({"file_read"}),
            (AccessTarget("file", "marker.txt", "file_read", kind="file"),),
            subject="marker.txt",
        ),
        required_capabilities=frozenset({"execution"}),
    )

    class ChildLLM:
        context_limit = 128_000

        def __init__(self):
            self.script = [
                response(content=None, calls=[
                    {"name": "inspect_child_scope", "arguments": {}},
                ]),
                response(content="done", calls=[]),
            ]

        def __call__(self, _messages, **_kwargs):
            yield event(self.script.pop(0))

    def authorization_commit_factory(target_session):
        def commit(change):
            for directory in change.session_directories:
                target_session.add_working_directory(Path(directory.value))

        return commit

    def approve(request):
        if request.tool_call.name == "write_file":
            return PermissionResponse("allow_session_directory")
        return PermissionResponse("allow_once")

    resolver = PermissionResolver(PermissionPolicy(PermissionSettings()), approve)
    spawn = make_spawn_agent_tool(
        ChildLLM(), [inspect_tool], max_depth=1, render_subagents=False,
        permission_resolver=resolver,
        authorization_commit_factory=authorization_commit_factory,
    )
    identity = bind_identity(parent)
    executor = ToolDispatchService(
        {
            write_file_tool.name: write_file_tool,
            execute_command_tool.name: execute_command_tool,
            spawn.name: spawn,
        },
        assemble_tool_capabilities(
            parent,
            None,
            RuntimeResources(
                parent.session_id,
                commands=CommandExecution(parent, identity),
                identity=identity,
            ),
            workspace_dir=workspace,
            cwd_provider=parent.get_cwd,
            authorization_commit_factory=authorization_commit_factory,
        ),
        session=parent,
        permission_resolver=resolver,
        authorization_commit=authorization_commit_factory(parent),
    )
    outcomes = executor.execute([
        ToolCall(
            write_file_tool.name,
            {"file": str(extra / "created.txt"), "content": "created"},
            "write",
        ),
        ToolCall(
            execute_command_tool.name,
            {"command": f"cd {extra}"},
            "cd",
        ),
        ToolCall(spawn.name, {"task": "inspect extra"}, "spawn"),
    ])

    assert all(outcome.result.ok for outcome in outcomes)
    assert parent.working_directories_snapshot() == (extra.resolve(),)
    assert parent.get_cwd() == extra.resolve()
    assert observed["cwd"] == str(extra.resolve())
    assert observed["additional"] == (extra.resolve(),)
    assert observed["marker"] == "marker"


def test_agent_child_persistent_directory_uses_child_checkpoint(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    config = tmp_path / "permission-settings.json"
    monkeypatch.setenv("WRIGHT_PERMISSION_CONFIG", str(config))
    checkpoints = FileSessionRepository(tmp_path / "checkpoints")
    parent = Session.create("parent", workspace)
    parent.begin_user_turn("parent")
    sessions = {}

    def authorization_commit_factory(target_session):
        sessions[target_session.session_id] = target_session

        def commit(change):
            for directory in change.session_directories:
                target_session.add_working_directory(Path(directory.value))
            if change.session_directories:
                checkpoints.save(target_session)
            for rule in change.persistent_rules:
                append_allow_rule(rule)
            for directory in change.persistent_directories:
                append_additional_directory(directory.value)

        return commit

    class ChildLLM:
        context_limit = 128_000

        def __init__(self):
            self.script = [
                response(content=None, calls=[
                    {
                        "name": "write_file",
                        "arguments": {
                            "file": str(extra / "child.txt"), "content": "child",
                        },
                    },
                ]),
                response(content="child done", calls=[]),
            ]

        def __call__(self, _messages, **_kwargs):
            yield event(self.script.pop(0))

    def approve(request):
        if request.tool_call.name == "write_file":
            return PermissionResponse("allow_persistent_directory")
        return PermissionResponse("allow_once")

    resolver = PermissionResolver(
        PermissionPolicy(PermissionSettings()), approve
    )
    spawn = make_spawn_agent_tool(
        ChildLLM(), [write_file_tool], max_depth=1, render_subagents=False,
        permission_resolver=resolver,
        authorization_commit_factory=authorization_commit_factory,
    )
    outcome = ToolDispatchService(
        {spawn.name: spawn},
        assemble_tool_capabilities(
            parent, None, None,
            workspace_dir=workspace,
            cwd_provider=parent.get_cwd,
            authorization_commit_factory=authorization_commit_factory,
        ),
        session=parent,
        permission_resolver=resolver,
        authorization_commit=authorization_commit_factory(parent),
    ).execute([ToolCall(spawn.name, {"task": "write outside"}, "spawn")])[0]

    assert outcome.result.ok
    assert (extra / "child.txt").read_text(encoding="utf-8") == "child"
    assert parent.working_directories_snapshot() == ()
    child_sessions = [session for session in sessions.values() if session is not parent]
    assert len(child_sessions) == 1
    child = child_sessions[0]
    assert child.working_directories_snapshot() == (extra.resolve(),)
    assert checkpoints.load(child.session_id).working_directories_snapshot() == (
        extra.resolve(),
    )
    assert str(extra.resolve()) in load_permission_settings().additional_directories


def test_persistent_approval_without_commit_callback_fails_closed(tmp_path):
    session = Session.create("independent", tmp_path)
    outside = tmp_path.parent / "a.txt"
    resolver = PermissionResolver(
        approval_handler=lambda _request: PermissionResponse(
            "allow_persistent_directory"
        )
    )
    outcome = ToolDispatchService(
        {write_file_tool.name: write_file_tool},
        assemble_tool_capabilities(session, None, None, workspace_dir=tmp_path),
        session=session,
        permission_resolver=resolver,
    ).execute([
        ToolCall(
            write_file_tool.name,
            {"file": str(outside), "content": "x"},
            "c1",
        )
    ])[0]

    assert not outcome.result.ok
    assert "persistent authorization" in outcome.result.err
    assert not outside.exists()


def test_authorization_commit_failure_prevents_tool_execution(tmp_path):
    outside = tmp_path.parent / "save-failure.txt"
    called = []

    def failing_commit(_change):
        called.append(True)
        raise OSError("checkpoint unavailable")

    resolver = PermissionResolver(
        approval_handler=lambda _request: PermissionResponse(
            "allow_session_directory"
        )
    )
    outcome = ToolDispatchService(
        {write_file_tool.name: write_file_tool},
        assemble_tool_capabilities(None, None, None, workspace_dir=tmp_path),
        permission_resolver=resolver,
        authorization_commit=failing_commit,
    ).execute([
        ToolCall(
            write_file_tool.name,
            {"file": str(outside), "content": "must not run"},
            "c1",
        )
    ])[0]

    assert called == [True]
    assert not outcome.result.ok
    assert "Could not save the authorization" in outcome.result.err
    assert not outside.exists()


def test_rg_search_literals_globs_and_explicit_hidden_file(tmp_path):
    nested = tmp_path / "nested"
    hidden = tmp_path / ".hidden.txt"
    nested.mkdir()
    (tmp_path / "root.txt").write_text("[x] \\ needle\n", encoding="utf-8")
    (nested / "code.py").write_text(
        "prefix (Needle) needle\n", encoding="utf-8"
    )
    (nested / "notes.md").write_text("needle\n", encoding="utf-8")
    hidden.write_text("needle\n", encoding="utf-8")

    executor = ToolDispatchService(
        {grep_tool.name: grep_tool},
        assemble_tool_capabilities(
            None, None, None, workspace_dir=tmp_path, cwd_provider=lambda: tmp_path,
        ),
    )
    literal = executor.execute([
        ToolCall(grep_tool.name, {
            "pattern": "[",
            "fixed_string": True,
        }, "literal")
    ])[0].result
    assert literal.ok
    assert literal.data["matches"][0]["column"] == 1

    regex = executor.execute([
        ToolCall(grep_tool.name, {
            "pattern": "needle",
            "case_sensitive": False,
            "glob": "*.py",
        }, "regex")
    ])[0].result
    assert regex.ok
    assert [item["path"] for item in regex.data["matches"]] == [
        "nested/code.py", "nested/code.py",
    ]
    assert [item["column"] for item in regex.data["matches"]] == [9, 17]

    hidden_result = executor.execute([
        ToolCall(grep_tool.name, {
            "pattern": "needle", "path": str(hidden), "fixed_string": True,
        }, "hidden")
    ])[0].result
    assert hidden_result.ok
    assert hidden_result.data["matches"][0]["path"] == ".hidden.txt"


def test_rg_candidates_are_authorized_before_content_search(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path))
    protected = tmp_path / "permission_settings.json"
    protected.write_text("needle protected\n", encoding="utf-8")
    (tmp_path / "allowed.txt").write_text("needle allowed\n", encoding="utf-8")

    class RecordingBackend(LocalExecutionBackend):
        def __init__(self):
            super().__init__(tmp_path, lambda: tmp_path)
            self.seen = []

        def search_files(self, paths, pattern, **kwargs):
            collected = list(paths)
            self.seen.extend(collected)
            return super().search_files(collected, pattern, **kwargs)

    backend = RecordingBackend()
    result = ToolDispatchService(
        {grep_tool.name: grep_tool},
        assemble_tool_capabilities(
            None, None, None,
            workspace_dir=tmp_path,
            cwd_provider=lambda: tmp_path,
            execution_backend=backend,
        ),
    ).execute([ToolCall(grep_tool.name, {"pattern": "needle"}, "c1")])[0].result

    assert result.ok
    assert [item["path"] for item in result.data["matches"]] == ["allowed.txt"]
    assert all(path.value != str(protected.resolve()) for path in backend.seen)
