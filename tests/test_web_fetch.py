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
        assert kwargs.get("follow_redirects") is not True
        return self.response


class _MapNetwork:
    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str, str | None]] = []

    def request(self, method, url, *, operation, **kwargs):
        self.calls.append((method, url, operation, kwargs.get("redirect_of")))
        assert kwargs.get("follow_redirects") is not True
        return self.responses[url]


def _response(status=200, body=b"", content_type="text/html", headers=None, url="https://example.com/page"):
    return httpx.Response(
        status,
        content=body,
        headers={"content-type": content_type, **(headers or {})},
        request=httpx.Request("GET", url),
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
    assert "[Home](/home)" not in text
    assert "alert" not in text and ".x{}" not in text


def test_html_prefers_article_or_main_and_drops_chrome():
    html = """<html><head><title>Post</title></head><body>
<nav>Menu</nav><aside>Related</aside>
<article><h1>Title</h1><p>Body <a href="/x">link</a>.</p></article>
<footer>Copyright</footer></body></html>"""

    title, text = html_to_text(html)

    assert title == "Post"
    assert "# Title" in text and "Body [link](/x)." in text
    assert "Menu" not in text and "Related" not in text and "Copyright" not in text


def test_empty_article_falls_back_to_the_rest_of_the_page():
    _, text = html_to_text("<article>  </article><p>Real content</p><footer>gone</footer>")

    assert "Real content" in text
    assert "gone" not in text


def test_web_fetch_is_a_get_network_read_and_pages_long_text():
    network = _Network(_response(body=PAGE))
    first = web_fetch("https://example.com/page", limit=20, network=network)

    assert network.calls == [("GET", "https://example.com/page", "network_read")]
    assert first.ok and first.data["title"] == "Docs"
    assert first.data["truncated"] is True
    rest = web_fetch("https://example.com/page", offset=first.data["next_offset"], limit=10_000, network=network)
    assert first.data["content"] + rest.data["content"] == html_to_text(PAGE.decode())[1]
    assert rest.data["truncated"] is False
    assert network.calls == [("GET", "https://example.com/page", "network_read")]


def test_same_host_redirect_is_followed_once():
    network = _MapNetwork({
        "https://example.com/page": _response(301, headers={"location": "/new"}),
        "https://example.com/new": _response(body=PAGE, url="https://example.com/new"),
    })

    result = web_fetch("https://example.com/page", network=network)

    assert result.ok and result.data["title"] == "Docs"
    assert result.data["url"] == "https://example.com/new"
    assert network.calls == [
        ("GET", "https://example.com/page", "network_read", None),
        ("GET", "https://example.com/new", "network_read", "https://example.com/page"),
    ]


def test_http_to_https_upgrade_is_followed():
    network = _MapNetwork({
        "http://example.com/page": _response(
            301, headers={"location": "https://example.com/page"}, url="http://example.com/page",
        ),
        "https://example.com/page": _response(body=PAGE, url="https://example.com/page"),
    })

    result = web_fetch("http://example.com/page", network=network)

    assert result.ok and result.data["url"] == "https://example.com/page"
    assert network.calls[1] == (
        "GET", "https://example.com/page", "network_read", "http://example.com/page",
    )


def test_https_to_http_is_not_followed():
    network = _Network(_response(301, headers={"location": "http://example.com/page"}))

    result = web_fetch("https://example.com/page", network=network)

    assert result.ok
    assert result.data["redirect"] == "http://example.com/page"
    assert len(network.calls) == 1


def test_www_host_swap_is_followed():
    network = _MapNetwork({
        "https://example.com/page": _response(
            301, headers={"location": "https://www.example.com/page"},
        ),
        "https://www.example.com/page": _response(body=PAGE, url="https://www.example.com/page"),
    })

    result = web_fetch("https://example.com/page", network=network)

    assert result.ok and result.data["url"] == "https://www.example.com/page"
    assert network.calls[1][1] == "https://www.example.com/page"


def test_cross_host_redirect_is_reported_as_an_absolute_url():
    network = _Network(_response(301, headers={"location": "https://evil.test/x"}))

    result = web_fetch("https://example.com/page", network=network)

    assert result.ok
    assert result.data["redirect"] == "https://evil.test/x"
    assert "Call web_fetch" in result.data["message"]
    assert len(network.calls) == 1


def test_second_same_host_redirect_is_reported():
    network = _MapNetwork({
        "https://example.com/a": _response(301, headers={"location": "/b"}, url="https://example.com/a"),
        "https://example.com/b": _response(302, headers={"location": "/c"}, url="https://example.com/b"),
    })

    result = web_fetch("https://example.com/a", network=network)

    assert result.ok
    assert result.data["redirect"] == "https://example.com/c"
    assert [call[1] for call in network.calls] == ["https://example.com/a", "https://example.com/b"]


def test_binary_content_is_refused():
    result = web_fetch("https://example.com/a.zip", network=_Network(_response(body=b"PK", content_type="application/zip")))

    assert not result.ok


def test_fetch_failures_are_not_cached():
    url = "https://example.com/mixed"
    refused = web_fetch(url, network=_Network(_response(body=b"PK", content_type="application/zip")))
    html = web_fetch(url, network=_Network(_response(body=PAGE)))

    assert not refused.ok
    assert html.ok and html.data["title"] == "Docs"


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
