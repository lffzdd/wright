"""Host-level network rules shared by Shell egress, web_fetch and http_request."""

import pytest

from wright.domain.gateway.runtime_types import ExecutionPath
from wright.domain.policy.permission.egress import decide_egress
from wright.domain.policy.permission.hosts import (
    is_private_literal,
    is_public_address,
    normalize_host,
)
from wright.domain.policy.permission.resolver import PermissionPolicy
from wright.domain.policy.permission.settings import (
    MatchContext,
    PermissionConfiguration,
    PermissionRule,
    PermissionSettings,
)
from wright.domain.policy.permission.types import (
    InvocationGrant,
    InvocationIdentity,
    NetworkTarget,
)
from wright.infrastructure.runtime.network import (
    AuthorizedNetwork,
    is_permitted_redirect,
)


def config(**lists) -> PermissionConfiguration:
    return PermissionConfiguration.from_settings(PermissionSettings.from_dict({"version": 2, "permissions": lists}))


@pytest.mark.parametrize("raw, expected", [
    ("Example.COM.", "example.com"), ("[::1]", "::1"), ("127.0.0.1", "127.0.0.1"),
    ("bücher.de", "xn--bcher-kva.de"), ("user@example.com", None), ("*.example.com", None),
    ("a..b", None), ("-bad.com", None), ("2130706433", None), ("127.1", None), ("", None),
])
def test_hosts_normalize_to_one_identity(raw, expected):
    assert normalize_host(raw) == expected


@pytest.mark.parametrize("address, public", [
    ("93.184.215.14", True), ("10.0.0.1", False), ("172.16.3.4", False), ("192.168.1.1", False),
    ("127.0.0.1", False), ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False),
    ("224.0.0.1", False), ("::1", False), ("fc00::1", False), ("fe80::1", False),
    ("::ffff:10.0.0.1", False), ("::ffff:93.184.215.14", True), ("64:ff9b::a00:1", False),
    ("2606:4700::1111", True), ("198.18.0.20", True),
])
def test_public_addresses(address, public):
    assert is_public_address(address) is public


def test_private_literals_need_no_lookup():
    assert is_private_literal("localhost") and is_private_literal("api.localhost")
    assert is_private_literal("10.1.2.3") and not is_private_literal("example.com")


def test_host_rules_parse_roundtrip_and_validate():
    rule = PermissionRule.parse("network(*.github.com)")
    assert rule.is_host_rule and rule.host == "github.com" and rule.host_match == "subdomains" and rule.port == 0
    assert PermissionRule.from_mapping(rule.to_persistent()) == rule
    assert PermissionRule.parse("network(example.com:8443)").port == 8443
    assert PermissionRule.parse("network([::1]:80)").host == "::1"
    for bad in ("network(*.com)", "network(exa mple.com)", "network(example.com:x)"):
        with pytest.raises(ValueError):
            PermissionRule.parse(bad)
    with pytest.raises(ValueError):
        PermissionRule.from_mapping({"kind": "network", "tool_name": "*", "host": "example.com", "port": 443, "methods": ["GET"]})
    with pytest.raises(ValueError):
        PermissionRule.from_mapping({"kind": "network", "tool_name": "*", "host": "Example.com", "port": 443})


def test_method_rules_keep_their_records_unchanged():
    rule = PermissionRule.network_grant("http_request", "https", "example.com", 443, ("GET",))
    assert "host_match" not in rule.to_persistent() and "allow_private" not in rule.to_persistent()


def test_one_host_rule_binds_all_three_tools():
    rule = PermissionRule.host_grant("example.com", 443)
    url = MatchContext(url="https://example.com/a", method="POST")
    assert rule.matches("http_request", "https://example.com/a", context=url)
    assert rule.matches("web_fetch", "https://example.com/a", context=url)
    assert rule.matches("shell", "example.com", context=MatchContext(network_host="example.com", network_port=443))
    assert not rule.matches("web_search", "x", context=url)
    assert not rule.matches("http_request", "x", context=MatchContext(url="http://example.com/"))
    # A Shell call is judged before it runs by its command, never by a host.
    assert not rule.matches("shell", "curl https://example.com", context=MatchContext())


def test_host_allow_rules_do_not_reach_private_destinations_unless_marked():
    plain = PermissionRule.host_grant("intranet.example", 443)
    marked = PermissionRule.host_grant("intranet.example", 443, allow_private=True)
    private = MatchContext(network_host="intranet.example", network_port=443, network_private=True)
    assert not plain.matches("shell", "", context=private)
    assert marked.matches("shell", "", context=private)
    assert plain.matches("shell", "", effect="deny", context=private)


def test_host_deny_rule_blocks_http_tools_before_any_allow():
    policy = PermissionPolicy(config(deny=["network(*.evil.com)"]))
    from wright.domain.model.tool import AccessTarget, ToolAccess
    access = ToolAccess(frozenset({"network_read"}), (AccessTarget("url", "https://a.evil.com/", "network_read", kind="url"),))
    decision, *_ = policy.evaluate(access, tool_name="web_fetch", subject="https://a.evil.com/", in_scope=True,
                                   context=MatchContext(url="https://a.evil.com/"),
                                   session_rules=(PermissionRule.host_grant("a.evil.com", 443),))
    assert decision == "deny"


def test_egress_decision_order():
    settings = config(allow=["network(example.com:443)"], deny=["network(*.evil.com)"], ask=["network(ask.example.org)"])
    assert decide_egress("example.com", 443, private=False, settings=settings).verdict == "allow"
    assert decide_egress("example.com", 80, private=False, settings=settings).verdict == "ask"
    assert decide_egress("x.evil.com", 443, private=False, settings=settings, all_public=True).verdict == "deny"
    assert decide_egress("ask.example.org", 443, private=False, settings=settings, all_public=True).verdict == "ask"
    assert decide_egress("other.org", 443, private=False, settings=settings, all_public=True).verdict == "allow"
    sandbox = (PermissionRule.sandbox_network_grant(),)
    assert decide_egress("other.org", 443, private=False, settings=settings, session_rules=sandbox).verdict == "allow"
    # Neither a command-wide approval nor a sandbox-network grant reaches private destinations.
    private = decide_egress("10.0.0.5", 443, private=True, settings=settings, session_rules=sandbox, all_public=True)
    assert private.verdict == "ask" and private.reason_code == "permission.network_private_host"
    marked = (PermissionRule.host_grant("10.0.0.5", 443, allow_private=True),)
    assert decide_egress("10.0.0.5", 443, private=True, settings=settings, session_rules=marked).verdict == "allow"


def grant_with(target: NetworkTarget) -> InvocationGrant:
    cwd = ExecutionPath("local", "/")
    return InvocationGrant(InvocationIdentity("s"), "local", cwd, frozenset({"network_read"}), network_targets=(target,))


def test_same_host_redirect_is_allowed_only_via_the_approved_origin():
    sent = []
    approved = "https://example.com/page"
    network = AuthorizedNetwork(
        grant_with(NetworkTarget(approved, "GET", "network_read")),
        transport=lambda method, url, **k: sent.append((method, url, k)) or "ok",
        resolver=lambda host, port: ["93.184.215.14"],
    )
    assert is_permitted_redirect(approved, "https://example.com/new")
    assert is_permitted_redirect(approved, "https://www.example.com/page")
    assert is_permitted_redirect("http://example.com/page", "https://example.com/page")
    assert is_permitted_redirect("http://example.com/page", "https://www.example.com/page")
    assert not is_permitted_redirect(approved, "https://evil.test/page")
    assert not is_permitted_redirect(approved, "http://example.com/page")
    assert network.request("GET", "https://example.com/new", operation="network_read", redirect_of=approved) == "ok"
    with pytest.raises(PermissionError, match="host"):
        network.request("GET", "https://evil.test/page", operation="network_read", redirect_of=approved)
    with pytest.raises(PermissionError):
        network.request("GET", "https://example.com/new", operation="network_read")
    assert sent == [("GET", "https://example.com/new", {"params": None, "follow_redirects": False})]
    http_origin = "http://example.com/page"
    upgraded = AuthorizedNetwork(
        grant_with(NetworkTarget(http_origin, "GET", "network_read")),
        transport=lambda *a, **k: "ok",
        resolver=lambda host, port: ["93.184.215.14"],
    )
    assert upgraded.request(
        "GET", "https://example.com/page", operation="network_read", redirect_of=http_origin,
    ) == "ok"


def test_http_tools_refuse_names_that_resolve_privately():
    sent = []
    target = NetworkTarget("https://intranet.example/", "GET", "network_read")
    network = AuthorizedNetwork(grant_with(target), transport=lambda *a, **k: sent.append(a) or "ok",
                                resolver=lambda host, port: ["10.1.2.3"])
    with pytest.raises(PermissionError, match="private"):
        network.request("GET", "https://intranet.example/", operation="network_read")
    assert not sent
    allowed = AuthorizedNetwork(grant_with(NetworkTarget(target.url, "GET", "network_read", allow_private=True)),
                                transport=lambda *a, **k: "ok", resolver=lambda host, port: ["10.1.2.3"])
    assert allowed.request("GET", "https://intranet.example/", operation="network_read") == "ok"
    public = AuthorizedNetwork(grant_with(target), transport=lambda *a, **k: "ok", resolver=lambda host, port: ["93.184.215.14"])
    assert public.request("GET", "https://intranet.example/", operation="network_read") == "ok"


def test_resolver_marks_explicit_private_targets():
    from wright.domain.policy.permission.resolver import _private_allowed
    assert _private_allowed("http://localhost:3000/", ())
    assert not _private_allowed("https://intranet.example/", ())
    rules = (PermissionRule.host_grant("intranet.example", 443, allow_private=True),)
    assert _private_allowed("https://intranet.example/x", rules)
    assert not _private_allowed("https://intranet.example:8443/x", rules)
