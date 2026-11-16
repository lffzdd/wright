"""Host decisions for sandboxed Shell egress.

The egress proxy asks this once per connection, with the host the command
named (CONNECT authority, SOCKS domain or HTTP request target). It sees only
host and port, so method or path rules never answer it; host rules shared with
the HTTP tools do. The decision is pure: lookups, prompts and storage stay in
the application and infrastructure layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ...model.tool_names import SHELL
from .settings import MatchContext, PermissionConfiguration, PermissionRule

EgressVerdict = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class EgressDecision:
    verdict: EgressVerdict
    reason_code: str
    rule: PermissionRule | None = None


def decide_egress(
    host: str,
    port: int,
    *,
    private: bool,
    settings: PermissionConfiguration | None,
    session_rules: tuple[PermissionRule, ...] = (),
    all_public: bool = False,
) -> EgressDecision:
    """Deny and ask rules first, then host grants; unknown hosts are asked about.

    ``all_public`` is this command's own approval to use the network
    (``allow_once_network``, or a sandbox-network grant on file when it
    started). Like a sandbox-network grant, it never reaches local or private
    destinations; only a host rule with ``allow_private`` does.
    """

    context = MatchContext(network_host=host, network_port=port, network_private=private)
    deny = settings.deny if settings is not None else ()
    ask = settings.ask if settings is not None else ()
    allow = (*session_rules, *(settings.allow if settings is not None else ()))
    for rule in deny:
        if _binds(rule, "deny", context):
            return EgressDecision("deny", "permission.network_host_denied", rule)
    for rule in ask:
        if _binds(rule, "ask", context):
            return EgressDecision("ask", "permission.network_host_ask_rule", rule)
    for rule in allow:
        if rule.is_host_rule and rule.matches(SHELL, host, effect="allow", context=context):
            return EgressDecision("allow", "permission.network_host_allowed", rule)
    if not private:
        if all_public:
            return EgressDecision("allow", "permission.network_command_allowed")
        for rule in allow:
            if rule.kind == "sandbox_network" and rule.execution_mode == "sandbox":
                return EgressDecision("allow", "permission.network_sandbox_grant", rule)
    return EgressDecision("ask", "permission.network_private_host" if private else "permission.network_host_unknown")


def _binds(rule: PermissionRule, effect: str, context: MatchContext) -> bool:
    return rule.is_host_rule and rule.matches(SHELL, context.network_host, effect=effect, context=context)
