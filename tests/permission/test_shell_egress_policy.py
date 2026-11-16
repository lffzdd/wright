"""Per-host answers for a running sandboxed command."""

import threading
import time

from wright.application.permission.egress import ShellEgressPolicy
from wright.application.tool_execution.approval import InteractiveApprovalHandler
from wright.domain.gateway.runtime_types import ExecutionPath
from wright.domain.model.tool import ToolCall
from wright.domain.policy.permission.settings import (
    PermissionConfiguration,
    PermissionRule,
    PermissionSettings,
)
from wright.domain.policy.permission.types import (
    InvocationGrant,
    InvocationIdentity,
    PermissionResponse,
)


def make(*, allow=(), deny=(), session=None, answer="deny", network_enabled=False, interactive=True):
    rules = {"session": list(session or ())}
    settings = PermissionConfiguration.from_settings(PermissionSettings.from_dict(
        {"version": 2, "permissions": {"allow": list(allow), "deny": list(deny)}}))
    prompts, commits = [], []

    def approve(request):
        prompts.append(request.prompt)
        time.sleep(0.05)
        return PermissionResponse(answer)

    def commit(change):
        commits.append(change)
        rules["session"].extend(change.session_rules)

    cwd = ExecutionPath("local", "/work")
    grant = InvocationGrant(InvocationIdentity("s", "r", "call-1"), "local", cwd, frozenset({"shell"}),
                            command="pip install x", network_enabled=network_enabled)
    policy = ShellEgressPolicy(grant, rules=lambda: (settings, tuple(rules["session"]), None),
                               approve=approve if interactive else None, commit=commit,
                               tool_call=ToolCall("shell", {"command": "pip install x"}, "call-1"))
    return policy, prompts, commits, rules


def test_rules_answer_without_asking():
    policy, prompts, *_ = make(allow=["network(pypi.org:443)"], deny=["network(*.evil.com)"], network_enabled=True)
    assert policy.decide("pypi.org", 443, private=False)
    assert not policy.decide("x.evil.com", 443, private=False)
    assert policy.decide("files.pythonhosted.org", 443, private=False)  # approved for all public hosts
    assert not prompts


def test_unknown_host_prompt_offers_once_and_remembered_host_rules():
    policy, prompts, commits, _ = make(answer="allow_session_network_host")
    assert policy.decide("pypi.org", 443, private=False)
    prompt = prompts[0]
    assert prompt.operation == "network_connect" and prompt.subject == "pypi.org:443"
    assert prompt.command == "pip install x" and prompt.reason_code == "permission.network_host_unknown"
    assert [choice.id for choice in prompt.choices] == [
        "allow_once", "allow_session_network_host", "allow_project_network_host", "allow_user_network_host", "deny"]
    saved = commits[0].session_rules[0]
    assert saved.is_host_rule and (saved.host, saved.port, saved.lifetime, saved.allow_private) == ("pypi.org", 443, "session", False)
    # The saved rule now answers; no second question.
    assert policy.decide("pypi.org", 443, private=False) and len(prompts) == 1


def test_private_destinations_are_flagged_and_saved_as_such():
    policy, prompts, commits, _ = make(answer="allow_session_network_host")
    assert policy.decide("intranet.example", 443, private=True)
    assert "private_network" in prompts[0].risk_flags and prompts[0].risk_level == "elevated"
    assert commits[0].session_rules[0].allow_private


def test_once_approval_lasts_for_the_command_only():
    policy, prompts, commits, _ = make(answer="allow_once")
    assert policy.decide("pypi.org", 443, private=False)
    assert policy.decide("pypi.org", 443, private=False) and len(prompts) == 1 and not commits
    assert policy.still_allowed("pypi.org", 443, private=False)


def test_concurrent_connections_share_one_question():
    policy, prompts, *_ = make(answer="allow_once")
    results = []
    threads = [threading.Thread(target=lambda: results.append(policy.decide("pypi.org", 443, private=False))) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True] * 5 and len(prompts) == 1


def test_no_questions_in_background_or_after_the_call_returned():
    background, prompts, *_ = make(interactive=False, answer="allow_once")
    assert not background.decide("pypi.org", 443, private=False) and not prompts
    foreground, prompts, *_ = make(answer="allow_once")
    foreground.end_interaction()
    assert not foreground.decide("pypi.org", 443, private=False) and not prompts


def test_revoking_a_host_rule_withdraws_it_and_deny_beats_once():
    policy, _, _, rules = make(session=[PermissionRule.host_grant("pypi.org", 443)])
    assert policy.still_allowed("pypi.org", 443, private=False)
    rules["session"].clear()
    assert not policy.still_allowed("pypi.org", 443, private=False)
    once, *_ = make(answer="allow_once", deny=["network(pypi.org)"])
    assert not once.decide("pypi.org", 443, private=False)


def test_a_failing_rule_store_denies():
    cwd = ExecutionPath("local", "/work")
    grant = InvocationGrant(InvocationIdentity("s"), "local", cwd, frozenset({"shell"}), network_enabled=True)

    def broken():
        raise OSError("unreadable")
    policy = ShellEgressPolicy(grant, rules=broken)
    assert not policy.decide("pypi.org", 443, private=False)
    assert not policy.still_allowed("pypi.org", 443, private=False)


def test_prompts_during_a_run_return_the_tool_to_running():
    phases = []

    class Prompter:
        def prompt_permission(self, prompt):
            return "allow_once"
    handler = InteractiveApprovalHandler(Prompter(), notify_phase=lambda call, phase: phases.append(phase))
    policy, *_ = make()
    policy._approve = handler.during_run
    assert policy.decide("pypi.org", 443, private=False)
    assert phases == ["awaiting_approval", "running"]
