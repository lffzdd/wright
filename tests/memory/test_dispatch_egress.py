"""The dispatcher enforces the egress verdict before a tool runs."""

from __future__ import annotations

from dataclasses import replace

from wright.application.composition.capabilities import assemble_tool_capabilities
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.domain.model.memory import EgressHit, EgressVerdict
from wright.domain.model.tool import ToolCall, ToolResult
from wright.domain.model.tool_definition import Tool
from wright.domain.policy import PermissionResponse, ToolAccess


class FixedGuard:
    def __init__(self, verdict=None, error=None):
        self.verdict = verdict
        self.error = error
        self.calls = []

    def inspect_call(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.verdict


def _dispatcher(tmp_path, guard, handler=None):
    ran = []

    def call(args, runtime):
        ran.append(args)
        return ToolResult.success({"ok": True})

    tool = Tool("publish", "", {}, call, access_descriptor=lambda args: ToolAccess.internal_read(reason="test"))
    assembly = replace(assemble_tool_capabilities(None, None, None, workspace_dir=tmp_path), egress_guard=guard)
    dispatcher = ToolDispatchService({tool.name: tool}, assembly, permission_approval_handler=handler)
    return dispatcher, ran


HELD = EgressVerdict("ask", (EgressHit("personal", "content", "面试其他公司", "f-0000000000000001"),), "private")


def test_allowed_call_runs(tmp_path):
    guard = FixedGuard(EgressVerdict.allow())
    dispatcher, ran = _dispatcher(tmp_path, guard)
    outcome = dispatcher.execute([ToolCall("publish", {"body": "x"}, "c1")])[0]
    assert outcome.result.ok and ran == [{"body": "x"}]
    assert guard.calls[0]["tool_name"] == "publish"


def test_held_call_without_anyone_to_ask_is_skipped(tmp_path):
    """L19: unattended runs skip the action and report why."""
    dispatcher, ran = _dispatcher(tmp_path, FixedGuard(HELD))
    outcome = dispatcher.execute([ToolCall("publish", {"body": "x"}, "c1")])[0]
    assert not outcome.result.ok and ran == []
    assert "No one is available" in outcome.result.err
    assert outcome.result.data["reason_code"] == "memory.egress_ask"


def test_user_can_allow_one_held_call(tmp_path):
    prompts = []

    def handler(request):
        prompts.append(request.prompt)
        return PermissionResponse("allow_once")

    dispatcher, ran = _dispatcher(tmp_path, FixedGuard(HELD), handler)
    outcome = dispatcher.execute([ToolCall("publish", {"body": "x"}, "c1")])[0]
    assert outcome.result.ok and ran == [{"body": "x"}]
    assert prompts[0].risk_flags == ("memory_egress",)
    assert "面试其他公司" in prompts[0].preview
    assert [choice.id for choice in prompts[0].choices] == ["allow_once", "deny"]


def test_user_denial_keeps_the_call_from_running(tmp_path):
    dispatcher, ran = _dispatcher(tmp_path, FixedGuard(HELD), lambda request: PermissionResponse("deny"))
    outcome = dispatcher.execute([ToolCall("publish", {"body": "x"}, "c1")])[0]
    assert not outcome.result.ok and ran == []
    assert "denied" in outcome.result.err


def test_credentials_are_refused_without_asking(tmp_path):
    asked = []
    verdict = EgressVerdict("reject", (EgressHit("secret", "credential", "ghp_…****"),), "credential")
    dispatcher, ran = _dispatcher(tmp_path, FixedGuard(verdict), lambda request: asked.append(request) or PermissionResponse("allow_once"))
    outcome = dispatcher.execute([ToolCall("publish", {"body": "x"}, "c1")])[0]
    assert not outcome.result.ok and ran == [] and asked == []


def test_a_crashing_guard_holds_the_call(tmp_path):
    """L18: an exception in the check must never let the call through."""
    dispatcher, ran = _dispatcher(tmp_path, FixedGuard(error=RuntimeError("boom")))
    outcome = dispatcher.execute([ToolCall("publish", {"body": "x"}, "c1")])[0]
    assert not outcome.result.ok and ran == []
