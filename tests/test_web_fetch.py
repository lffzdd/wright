import httpx
import pytest

from wright.domain.policy.permission.settings import PermissionRule
from wright.infrastructure.tools import tools as built_in_tools
from wright.infrastructure.tools.web_fetch import (
    html_to_text,
    web_fetch,
    web_fetch_tool,
)


class _Network:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method, url, *, operation, **kwargs):
        self.calls.append((method, url, operation))
        assert "follow_redirects" not in kwargs
        return self.response


def _response(status=200, body=b"", content_type="text/html", headers=None):
    return httpx.Response(
        status,
        content=body,
        headers={"content-type": content_type, **(headers or {})},
        request=httpx.Request("GET", "https://example.com/page"),
    )


PAGE = b"""<!doctype html><html><head><title>Docs</title><style>.x{}</style>
<script>alert(1)</script></head><body><nav><a href="/home">Home</a></nav>
<h1>Install</h1><p>Run   the <code>setup</code> step.</p>
<ul><li>one</li><li>two</li></ul><pre>line 1
  line 2</pre></body></html>"""


def test_html_is_converted_to_readable_text():
    title, text = html_to_text(PAGE.decode())

    assert title == "Docs"
    assert "# Install" in text
    assert "Run the `setup` step." in text
    assert "- one" in text and "- two" in text
    assert "line 1\n  line 2" in text
    assert "[Home](/home)" in text
    assert "alert" not in text and ".x{}" not in text


def test_web_fetch_is_a_get_network_read_and_pages_long_text():
    network = _Network(_response(body=PAGE))
    first = web_fetch("https://example.com/page", limit=20, network=network)

    assert network.calls == [("GET", "https://example.com/page", "network_read")]
    assert first.ok and first.data["title"] == "Docs"
    assert first.data["truncated"] is True
    rest = web_fetch("https://example.com/page", offset=first.data["next_offset"], limit=10_000, network=network)
    assert first.data["content"] + rest.data["content"] == html_to_text(PAGE.decode())[1]
    assert rest.data["truncated"] is False


def test_redirects_are_reported_not_followed():
    network = _Network(_response(301, headers={"location": "https://example.com/new"}))

    result = web_fetch("https://example.com/page", network=network)

    assert result.ok
    assert result.data["redirect"] == "https://example.com/new"
    assert len(network.calls) == 1


def test_binary_content_is_refused():
    result = web_fetch("https://example.com/a.zip", network=_Network(_response(body=b"PK", content_type="application/zip")))

    assert not result.ok


def test_access_is_network_read_on_the_exact_get_target():
    access = web_fetch_tool.access_descriptor({"url": "https://example.com/page?q=1"})

    assert access.operations == frozenset({"network_read"})
    assert access.targets[0].http_method == "GET"
    assert access.targets[0].value == "https://example.com/page?q=1"
    with pytest.raises(PermissionError):
        web_fetch_tool.access_descriptor({"url": "file:///etc/passwd"})


def test_web_fetch_rules_are_network_rules_and_the_tool_is_baseline():
    rule = PermissionRule.parse("web_fetch(http://*)", effect="deny")

    assert rule.kind == "network" and rule.scheme == "http"
    assert any(tool.name == "web_fetch" and not tool.defer_to_model for tool in built_in_tools)
