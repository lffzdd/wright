import json
import logging

import httpx
import pytest

from wright.infrastructure.runtime.network import request_target
from wright.infrastructure.tools.web_search import (
    DEFAULT_RESULTS,
    MAX_RESULTS,
    build_web_search_tool,
    optional_web_search_tools,
    resolve_search_config,
)


class _Network:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def request(self, method, url, *, operation, params=None, **kwargs):
        self.calls.append({"method": method, "url": url, "operation": operation, "params": params, **kwargs})
        return self.response


class _Runtime:
    def __init__(self, network) -> None:
        self.network = network


def _json(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://search.test/"))


def _tool(env):
    config = resolve_search_config(env)
    assert config is not None
    return build_web_search_tool(config)


def test_no_key_registers_no_search_tool():
    assert optional_web_search_tools({}) == []
    # An empty value in .env is the same as no key.
    assert optional_web_search_tools({"TAVILY_API_KEY": "  "}) == []


def test_provider_is_chosen_from_the_configured_key():
    assert resolve_search_config({"TAVILY_API_KEY": "t"}).provider.name == "tavily"
    assert resolve_search_config({"BRAVE_API_KEY": "b"}).provider.name == "brave"
    both = {"TAVILY_API_KEY": "t", "BRAVE_API_KEY": "b"}
    assert resolve_search_config(both).provider.name == "tavily"
    assert resolve_search_config({**both, "WRIGHT_SEARCH_PROVIDER": "Brave"}).provider.name == "brave"


def test_explicit_provider_without_its_key_or_unknown_provider_is_not_registered(caplog):
    with caplog.at_level(logging.WARNING):
        assert resolve_search_config({"WRIGHT_SEARCH_PROVIDER": "brave", "TAVILY_API_KEY": "t"}) is None
        assert resolve_search_config({"WRIGHT_SEARCH_PROVIDER": "bing", "TAVILY_API_KEY": "t"}) is None
    assert "BRAVE_API_KEY" in caplog.text
    assert "bing" in caplog.text


def test_invalid_base_url_is_not_registered(caplog):
    with caplog.at_level(logging.WARNING):
        assert resolve_search_config({"TAVILY_API_KEY": "t", "TAVILY_BASE_URL": "file:///tmp"}) is None
    assert "TAVILY_BASE_URL" in caplog.text


def test_tavily_request_uses_configured_base_url_and_parses_results():
    tool = _tool({"TAVILY_API_KEY": "secret", "TAVILY_BASE_URL": "https://proxy.test/tavily/"})
    network = _Network(_json({"results": [{"title": "Py", "url": "https://python.org", "content": "Python"}]}))

    result = tool.call({"query": "python"}, _Runtime(network))

    assert result.ok
    assert result.data == {"results": [{"title": "Py", "url": "https://python.org", "snippet": "Python"}]}
    [call] = network.calls
    assert (call["method"], call["url"], call["operation"]) == ("POST", "https://proxy.test/tavily/search", "network_read")
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["json"]["query"] == "python"
    assert call["json"]["max_results"] == DEFAULT_RESULTS


def test_brave_request_is_a_get_with_token_header_and_strips_markup():
    tool = _tool({"BRAVE_API_KEY": "secret"})
    network = _Network(_json({"web": {"results": [
        {"title": "A &amp; B", "url": "https://a.test", "description": "<strong>fast</strong> search"},
    ]}}))

    result = tool.call({"query": "rust", "max_results": 3}, _Runtime(network))

    assert result.data == {"results": [{"title": "A & B", "url": "https://a.test", "snippet": "fast search"}]}
    [call] = network.calls
    assert (call["method"], call["url"]) == ("GET", "https://api.search.brave.com/res/v1/web/search")
    assert call["params"] == {"q": "rust", "count": 3}
    assert call["headers"]["X-Subscription-Token"] == "secret"


@pytest.mark.parametrize("requested,sent", [(None, DEFAULT_RESULTS), (0, 1), (-4, 1), (50, MAX_RESULTS), ("3", 3), ("x", DEFAULT_RESULTS)])
def test_max_results_is_optional_and_clamped(requested, sent):
    tool = _tool({"TAVILY_API_KEY": "t"})
    network = _Network(_json({"results": []}))
    args = {"query": "q"} if requested is None else {"query": "q", "max_results": requested}

    tool.call(args, _Runtime(network))

    assert network.calls[0]["json"]["max_results"] == sent
    assert "max_results" not in tool.parameters["required"]


@pytest.mark.parametrize("env,args", [
    ({"TAVILY_API_KEY": "t", "TAVILY_BASE_URL": "https://proxy.test"}, {"query": "q", "max_results": 99}),
    ({"BRAVE_API_KEY": "b"}, {"query": "a b&c", "max_results": 2}),
])
def test_approved_target_is_exactly_the_request_sent(env, args):
    """AuthorizedNetwork rejects any request that differs from the approved target."""
    tool = _tool(env)
    network = _Network(_json({"results": [], "web": {"results": []}}))

    access = tool.describe_access(args)
    tool.call(args, _Runtime(network))

    [call] = network.calls
    [target] = access.targets
    assert access.operations == frozenset({"network_read"})
    assert target.http_method == call["method"]
    assert target.value == request_target(call["method"], call["url"], call["params"])
    assert access.subject == args["query"]


def test_credentials_stay_out_of_access_and_approval_details():
    tool = _tool({"BRAVE_API_KEY": "very-secret"})
    args = {"query": "q"}

    shown = json.dumps([repr(tool.describe_access(args)), tool.approval_details(args, "/")])

    assert "very-secret" not in shown


def test_empty_query_fails_without_a_request():
    tool = _tool({"TAVILY_API_KEY": "t"})
    network = _Network(_json({}))

    result = tool.call({"query": "   "}, _Runtime(network))

    assert not result.ok
    assert network.calls == []


def test_provider_http_error_is_a_readable_failure_without_the_key():
    tool = _tool({"TAVILY_API_KEY": "very-secret"})
    network = _Network(_json({"detail": "Unauthorized"}, status=401))

    result = tool.call({"query": "q"}, _Runtime(network))

    assert not result.ok
    assert "401" in result.err and "tavily" in result.err.lower()
    assert "very-secret" not in result.err
