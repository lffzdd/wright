"""Per-host decisions for one sandboxed Shell command's egress proxy.

The proxy calls ``decide`` for every connection the command opens. Rules are
read fresh each time, so grants added or revoked while the command runs take
effect on its next connection, and ``still_allowed`` lets open tunnels notice
revocation. Unknown hosts are put to the user while the tool call is in the
foreground; afterwards (background commands, auto-backgrounded commands,
non-interactive runs) they are denied and reported.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from ...domain.model.tool import ToolAccess, ToolCall
from ...domain.policy.permission.approval import PermissionRequest
from ...domain.policy.permission.egress import EgressDecision, decide_egress
from ...domain.policy.permission.hosts import format_authority
from ...domain.policy.permission.settings import PermissionConfiguration, PermissionRule
from ...domain.policy.permission.types import (
    AuthorizationChange,
    InvocationGrant,
    PermissionChoice,
    PermissionPrompt,
    PermissionResponse,
)

_LIFETIMES = (("session", "This conversation (including resume)"), ("project", "This project"), ("user", "All projects"))


class ShellEgressPolicy:
    def __init__(
        self,
        grant: InvocationGrant,
        *,
        rules: Callable[[], tuple[PermissionConfiguration | None, tuple[PermissionRule, ...], object]],
        approve: Callable[[PermissionRequest], PermissionResponse] | None = None,
        commit: Callable[[AuthorizationChange], None] | None = None,
        tool_call: ToolCall | None = None,
    ):
        """``rules`` returns (settings, session rules, access scope) as of now."""
        self._grant = grant
        self._rules = rules
        self._approve = approve
        self._commit = commit
        self._tool_call = tool_call or ToolCall("shell", {"command": grant.command or ""}, grant.identity.call_id)
        self._interactive = approve is not None
        self._lock = threading.Lock()
        self._once: set[tuple[str, int, bool]] = set()
        self._pending: dict[tuple[str, int, bool], threading.Event] = {}
        self._asked = 0

    def end_interaction(self) -> None:
        self._interactive = False

    def decide(self, host: str, port: int, *, private: bool) -> bool:
        try:
            decision = self._decision(host, port, private)
        except Exception:
            return False
        if decision.verdict == "allow":
            return True
        if decision.verdict == "deny" or not self._interactive:
            return False
        return self._ask(host, port, private, decision)

    def still_allowed(self, host: str, port: int, *, private: bool) -> bool:
        try:
            return self._decision(host, port, private).verdict == "allow"
        except Exception:
            return False

    def _decision(self, host: str, port: int, private: bool) -> EgressDecision:
        settings, session_rules, _ = self._rules()
        decision = decide_egress(host, port, private=private, settings=settings, session_rules=session_rules,
                                 all_public=self._grant.network_enabled)
        if decision.verdict != "deny" and (host, port, private) in self._once:
            return EgressDecision("allow", "permission.network_host_once")
        return decision

    def _ask(self, host: str, port: int, private: bool, decision: EgressDecision) -> bool:
        key = (host, port, private)
        with self._lock:
            waiting = self._pending.get(key)
            if waiting is None:
                self._pending[key] = threading.Event()
                self._asked += 1
                number = self._asked
        if waiting is not None:
            # One question per host: later connections wait for its answer.
            waiting.wait()
            return self.still_allowed(host, port, private=private)
        try:
            allowed = self._prompt(host, port, private, decision, number)
            if allowed:
                with self._lock:
                    self._once.add(key)
            return allowed
        except Exception:
            return False
        finally:
            with self._lock:
                event = self._pending.pop(key)
            event.set()

    def _prompt(self, host: str, port: int, private: bool, decision: EgressDecision, number: int) -> bool:
        assert self._approve is not None
        _, _, scope = self._rules()
        authority = format_authority(host, port)
        rule = PermissionRule.host_grant(host, port, allow_private=private)
        detail = "a local or private address" if private else "a host"
        choices = [PermissionChoice("allow_once", "Allow for this command", f"{authority} until this command ends", "Not saved",
                                    resource_kind="network_host", operations=("network_write",))]
        if self._commit is not None:
            for lifetime, label in _LIFETIMES:
                record = replace(rule, lifetime=lifetime)
                choices.append(PermissionChoice(f"allow_{lifetime}_network_host", label, record.describe(), label, lifetime,
                                                "network_host", ("network_read", "network_write"), record.to_persistent()))
        choices.append(PermissionChoice("deny", "Deny", "The connection is refused", "Not saved"))
        command = self._grant.command or ""
        prompt = PermissionPrompt(
            request_id=f"{self._tool_call.id or self._grant.identity.call_id}:egress:{number}",
            tool_name="shell",
            subject=authority,
            reason=f"A running command wants to connect to {detail}: {authority}",
            risk_flags=("private_network",) if private else (),
            targets=(authority,),
            choices=tuple(choices),
            principal=self._grant.identity.agent_task_id or self._grant.identity.session_id,
            operation="network_connect",
            grant_summary=f"Connections from this command to {authority}",
            cwd=self._grant.cwd.value,
            command=command,
            shell_note="Wright sees only the host and port. HTTPS content, methods and paths stay encrypted.",
            execution_mode="sandbox",
            reason_code=decision.reason_code,
            reason_params=(("host", host), ("port", str(port))),
            risk_level="elevated" if private else "review",
        )
        request = PermissionRequest(self._tool_call, dict(self._tool_call.arguments), ToolAccess(frozenset({"network_write"}), (), subject=authority),
                                    (), self._grant.cwd, scope, self._grant.identity, prompt)
        choice = self._approve(request).choice
        if choice == "allow_once":
            return True
        for lifetime, _ in _LIFETIMES:
            if choice == f"allow_{lifetime}_network_host" and self._commit is not None:
                record = replace(rule, lifetime=lifetime)
                field = {"session": "session_rules", "project": "project_rules", "user": "persistent_rules"}[lifetime]
                change = AuthorizationChange(**{field: (record,)})
                try:
                    self._commit(change)
                except Exception:
                    # The user allowed it; a failed save keeps it to this command.
                    pass
                return True
        return False
