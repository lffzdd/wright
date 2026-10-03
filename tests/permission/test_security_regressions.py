"""Regressions for permission identity, shell decisions, and authorization commit.

These go through ToolDispatchService and the real file/shell tools. HTTP uses
the production access descriptor and a local call so no network request is sent.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tests.permission_helpers import permission_directories
from wright.application.command.execution import CommandExecution
from wright.application.execution.identity import bind_identity
from wright.application.session.live_resources import RuntimeResources
from wright.application.tool_execution.capabilities import assemble_tool_capabilities
from wright.application.tool_execution.commit import commit_authorization
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall, ToolResult
from wright.domain.policy import (
    PermissionPolicy,
    PermissionResolver,
    PermissionResponse,
    PermissionSettings,
)
from wright.infrastructure.config.permission_store import append_allow_rule
from wright.infrastructure.tools.command import execute_command_tool
from wright.infrastructure.tools.file import read_file_tool, write_file_tool
from wright.infrastructure.tools.web_tools import http_request_tool
from wright.interfaces.interaction import InteractionHub


class _Choices:
    def __init__(self, *choices: str) -> None:
        self.choices = list(choices)
        self.prompts = []

    def __call__(self, request):
        self.prompts.append(request.prompt)
        choice = self.choices.pop(0) if self.choices else "deny"
        return PermissionResponse(choice)


def _files(session: Session, handler=None, settings=None) -> ToolDispatchService:
    policy = PermissionPolicy(settings) if settings is not None else PermissionPolicy()
    return ToolDispatchService(
        {
            write_file_tool.name: write_file_tool,
            read_file_tool.name: read_file_tool,
        },
        assemble_tool_capabilities(
            session, None, None,
            workspace_dir=session.workspace_dir,
            cwd_provider=session.get_cwd,
        ),
        session=session,
        permission_resolver=PermissionResolver(policy, handler),
    )


def _run(executor: ToolDispatchService, name: str, arguments: dict, call_id: str):
    return executor.execute([ToolCall(name, arguments, call_id)])[0].result


def test_remembered_file_rule_does_not_follow_parent_segments(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session.create("files", workspace)
    handler = _Choices("allow_session_rule")
    executor = _files(session, handler)
    first = _run(executor, "write_file", {"file": "nested/a.txt", "content": "a"}, "c1")
    second = _run(
        executor,
        "write_file",
        {"file": "nested/../../outside.txt", "content": "escaped"},
        "c2",
    )
    assert first.ok
    assert not second.ok
    assert (workspace / "nested" / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (tmp_path / "outside.txt").exists()
    assert len(handler.prompts) == 2


def test_file_deny_matches_dot_and_absolute_aliases(tmp_path):
    workspace = tmp_path / "workspace"
    secret = workspace / "secrets" / "key"
    secret.parent.mkdir(parents=True)
    secret.write_text("hidden", encoding="utf-8")
    session = Session.create("files", workspace)
    settings = PermissionSettings.from_dict({"version": 2,
        "mode": "bypass",
        "permissions": {"deny": ["read_file(secrets/*)"]},
    })
    executor = _files(session, settings=settings)
    relative = _run(executor, "read_file", {"file": "./secrets/key"}, "c1")
    absolute = _run(executor, "read_file", {"file": str(secret.resolve())}, "c2")
    assert not relative.ok
    assert not absolute.ok


def test_file_rule_stays_on_its_root_when_cwd_or_project_changes(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    session = Session.create("files", workspace)
    handler = _Choices("allow_session_rule")
    executor = _files(session, handler)
    assert _run(
        executor, "write_file", {"file": "nested/a.txt", "content": "a"}, "c1"
    ).ok
    session.set_cwd(other)
    moved = _run(
        executor, "write_file", {"file": "nested/a.txt", "content": "nope"}, "c2"
    )
    assert not moved.ok
    assert not (other / "nested" / "a.txt").exists()

    config = tmp_path / "permissions.json"
    monkeypatch.setenv("WRIGHT_PERMISSION_CONFIG", str(config))
    rule = session.permission_rules[0]
    from dataclasses import replace as replace_rule

    from wright.domain.policy.permission.settings import PermissionRule
    record = replace_rule(PermissionRule.from_mapping(rule), root=str(workspace), root_kind="absolute", lifetime="user").to_persistent()
    append_allow_rule(record, config)
    from wright.infrastructure.config import load_permission_settings

    elsewhere = Session.create("elsewhere", other)
    loaded = _files(elsewhere, settings=load_permission_settings(config))
    cross = _run(loaded, "write_file", {"file": "nested/a.txt", "content": "cross"}, "c3")
    assert not cross.ok
    assert not (other / "nested" / "a.txt").exists()


def test_symlink_write_is_judged_by_the_canonical_target(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    try:
        (nested / "inside.txt").symlink_to(outside)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create symlinks; native isolation suite also tests junctions")
        raise
    session = Session.create("files", workspace)
    handler = _Choices("allow_session_rule")
    executor = _files(session, handler)
    assert _run(
        executor, "write_file", {"file": "nested/a.txt", "content": "a"}, "c1"
    ).ok
    linked = _run(
        executor, "write_file", {"file": "nested/inside.txt", "content": "via link"}, "c2"
    )
    assert not linked.ok
    assert not outside.exists()


def test_http_grant_keeps_method_and_origin(tmp_path):
    session = Session.create("http", tmp_path)
    handler = _Choices("allow_session_rule")
    tool = replace(
        http_request_tool,
        call=lambda args, runtime: ToolResult.success({"ok": True}),
    )
    executor = ToolDispatchService(
        {tool.name: tool},
        assemble_tool_capabilities(
            session, None, None,
            workspace_dir=session.workspace_dir,
            cwd_provider=session.get_cwd,
        ),
        session=session,
        permission_resolver=PermissionResolver(PermissionPolicy(), handler),
    )
    first = _run(
        executor,
        "http_request",
        {"url": "https://user:secret@example.com/info", "method": "GET"},
        "c1",
    )
    post = _run(
        executor,
        "http_request",
        {"url": "https://example.com/modify", "method": "POST", "body": {"x": 1}},
        "c2",
    )
    evil = _run(
        executor,
        "http_request",
        {"url": "https://example.com.evil.invalid/info", "method": "GET"},
        "c3",
    )
    other_port = _run(
        executor,
        "http_request",
        {"url": "https://example.com:8443/info", "method": "GET"},
        "c4",
    )
    same = _run(
        executor,
        "http_request",
        {"url": "https://example.com/other", "method": "GET"},
        "c5",
    )
    assert first.ok and same.ok
    assert not post.ok and not evil.ok and not other_port.ok
    assert "secret" not in handler.prompts[0].subject
    assert handler.prompts[0].http_method == "GET"
    assert "example.com" in handler.prompts[0].http_target
    assert len(handler.prompts) == 4


def _shell(session: Session, settings, handler=None) -> ToolDispatchService:
    identity = bind_identity(session)
    return ToolDispatchService(
        {execute_command_tool.name: execute_command_tool},
        assemble_tool_capabilities(
            session,
            None,
            RuntimeResources(
                session.session_id,
                commands=CommandExecution(session, identity),
                identity=identity,
            ),
            workspace_dir=session.workspace_dir,
            cwd_provider=session.get_cwd,
        ),
        session=session,
        permission_resolver=PermissionResolver(PermissionPolicy(settings), handler),
    )


def test_compound_shell_cannot_skip_deny_or_a_simple_allow(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    demo = workspace / "demo"
    demo.write_text("keep", encoding="utf-8")
    session = Session.create("shell", workspace)
    denied = PermissionSettings.from_dict({"version": 2,
        "mode": "bypass",
        "permissions": {"deny": ["execute_command(rm *)"]},
    })
    executor = _shell(session, denied)
    commands = [
        "rm demo",
        "rm demo && echo done",
        "rm demo; echo done",
        "rm demo | echo done",
        "rm demo\necho done",
        "echo $(rm demo)",
        "rm demo > out",
    ]
    for index, command in enumerate(commands):
        result = _run(executor, "execute_command", {"command": command}, f"d{index}")
        assert not result.ok, command
        assert demo.read_text(encoding="utf-8") == "keep"

    with pytest.raises(ValueError, match="Shell grants"):
        PermissionSettings.from_dict({"version": 2, "permissions": {"allow": ["execute_command(echo *)"]}})
    asked = PermissionSettings.from_dict({"version": 2, "mode": "default", "permissions": {}})
    handler = _Choices()
    asking = _shell(session, asked, handler)
    compound = _run(
        asking, "execute_command", {"command": "echo hi && echo done"}, "ask"
    )
    assert not compound.ok
    assert handler.prompts
    assert demo.read_text(encoding="utf-8") == "keep"
    assert "local process" in handler.prompts[0].shell_note
    assert "OS sandbox" in handler.prompts[0].shell_note


def test_checkpoint_failure_rolls_back_session_and_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    config = tmp_path / "permissions.json"
    config.write_text(
        '{"mode": "default", "permissions": {"allow": [], "deny": []}, "version": 2}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("WRIGHT_PERMISSION_CONFIG", str(config))
    session = Session.create("commit", workspace)

    def fail_save(_session) -> None:
        raise RuntimeError("checkpoint failed")

    def commit(change) -> None:
        commit_authorization(change, session=session, permissions=assemble_tool_capabilities(session, None, None).permissions, save_checkpoint=fail_save)

    handler = _Choices("allow_session_directory_write", "allow_user_rule")
    executor = ToolDispatchService(
        {write_file_tool.name: write_file_tool},
        assemble_tool_capabilities(
            session, None, None,
            workspace_dir=session.workspace_dir,
            cwd_provider=session.get_cwd,
        ),
        session=session,
        permission_resolver=PermissionResolver(PermissionPolicy(), handler),
        authorization_commit=commit,
    )
    outside = _run(
        executor,
        "write_file",
        {"file": str(extra / "a.txt"), "content": "no"},
        "c1",
    )
    assert not outside.ok
    assert permission_directories(session) == ()
    assert not (extra / "a.txt").exists()

    persistent = _run(
        executor, "write_file", {"file": "nested/a.txt", "content": "no"}, "c2"
    )
    assert not persistent.ok
    assert session.permission_rules == []
    assert not (workspace / "nested" / "a.txt").exists()
    assert '"kind"' not in config.read_text(encoding="utf-8")


def test_child_session_rule_does_not_widen_the_parent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = Session.create("parent", workspace)
    child = Session.create("child", workspace)

    def factory(target: Session):
        def commit(change) -> None:
            commit_authorization(change, session=target, permissions=assemble_tool_capabilities(target, None, None).permissions)

        return commit

    handler = _Choices("allow_session_rule")
    resolver = PermissionResolver(PermissionPolicy(), handler)
    child_executor = ToolDispatchService(
        {write_file_tool.name: write_file_tool},
        assemble_tool_capabilities(
            child, None, None,
            workspace_dir=child.workspace_dir,
            cwd_provider=child.get_cwd,
        ),
        session=child,
        permission_resolver=resolver,
        authorization_commit=factory(child),
    )
    parent_executor = ToolDispatchService(
        {write_file_tool.name: write_file_tool},
        assemble_tool_capabilities(
            parent, None, None,
            workspace_dir=parent.workspace_dir,
            cwd_provider=parent.get_cwd,
        ),
        session=parent,
        permission_resolver=resolver,
        authorization_commit=factory(parent),
    )
    assert _run(
        child_executor, "write_file", {"file": "nested/a.txt", "content": "a"}, "c1"
    ).ok
    parent_result = _run(
        parent_executor, "write_file", {"file": "nested/b.txt", "content": "b"}, "c2"
    )
    assert not parent_result.ok
    assert child.permission_rules
    assert parent.permission_rules == []
    assert not (workspace / "nested" / "b.txt").exists()


def _append_many(path: str, prefix: str) -> None:
    for index in range(15):
        append_allow_rule(f"{prefix}_{index}", Path(path))


def test_concurrent_config_updates_keep_both_writers(tmp_path):
    config = tmp_path / "permissions.json"
    config.write_text(
        '{"mode": "default", "permissions": {"allow": [], "deny": []}, "version": 2}\n',
        encoding="utf-8",
    )
    context = multiprocessing.get_context("spawn")
    first = context.Process(target=_append_many, args=(str(config), "alpha"))
    second = context.Process(target=_append_many, args=(str(config), "beta"))
    first.start()
    second.start()
    first.join(10)
    second.join(10)
    assert first.exitcode == 0 and second.exitcode == 0
    text = config.read_text(encoding="utf-8")
    for index in range(15):
        assert f"alpha_{index}" in text
        assert f"beta_{index}" in text


def _wait_request(hub: InteractionHub):
    deadline = time.time() + 2
    while time.time() < deadline:
        time.sleep(0.005)
        request = hub.poll()
        if request is not None:
            return request
    raise AssertionError("permission request was not delivered")


def test_approval_cancel_deny_and_allow_end_the_dispatch(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session.create("approval", workspace)
    hub = InteractionHub()
    prompts = []

    def handler(request):
        prompts.append(request.prompt)
        answer = hub.request("permission", request.prompt.to_dict())
        return PermissionResponse(str(answer or "deny"))

    executor = _files(session, handler)
    outcome: list = []

    def run_call(name: str, arguments: dict, call_id: str) -> None:
        outcome.append(_run(executor, name, arguments, call_id))

    cancelled = threading.Thread(
        target=run_call,
        args=("write_file", {"file": "cancelled.txt", "content": "no"}, "c1"),
    )
    cancelled.start()
    _wait_request(hub)
    hub.cancel_pending()
    cancelled.join(timeout=2)
    assert outcome and not outcome[-1].ok
    assert not (workspace / "cancelled.txt").exists()

    allowed = threading.Thread(
        target=run_call,
        args=("write_file", {"file": "kept.txt", "content": "yes"}, "c2"),
    )
    allowed.start()
    request = _wait_request(hub)
    assert hub.resolve(request.request_id, "allow_once") is True
    allowed.join(timeout=2)
    assert (workspace / "kept.txt").read_text(encoding="utf-8") == "yes"
    prompt = prompts[-1]
    assert "yes" in prompt.preview
    assert prompt.reason
    assert "kept.txt" in prompt.grant_summary
    assert prompt.choices[0].scope == "This invocation only"
