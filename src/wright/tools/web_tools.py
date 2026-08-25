import httpx
import os
from typing import Any

from ..permission import PermissionCheckResult
from .base import Tool, ToolResult


def web_search(query: str, max_results: int, timeout: int = 20):
    resp = httpx.post(
        url="https://api.tavily.com/search",
        headers={
            "Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "max_results": max_results,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("results", []):
        title = item.get("title", "")
        url = item.get("url", "")
        content = item.get("content", "")
        results.append({"title": title, "snippet": content, "url": url})
    return ToolResult.success({"results": results})


def http_request(
    url: str,
    method: str = "GET",
    params: dict | None = None,
    body: dict | str | None = None,
    headers: dict | None = None,
    timeout: int = 20,
):
    kwargs: dict[str, Any] = {"params": params, "headers": headers, "timeout": timeout}

    if isinstance(body, dict):
        kwargs["json"] = body
    elif isinstance(body, str):
        kwargs["content"] = body

    resp = httpx.request(method, url, **kwargs)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    try:
        body = resp.json() if "application/json" in content_type else resp.text
    except Exception:
        body = resp.text

    if isinstance(body, str) and len(body) > 4000:
        body = body[:4000]
        truncated = True
    else:
        truncated = False

    return ToolResult.success({"response": body, "truncated": truncated})


def _ask_http_request(args: dict, runtime) -> PermissionCheckResult:
    flags = ("accesses_network",)
    return PermissionCheckResult(
        "ask",
        f"{runtime.tool_name}: requires user approval by web tool policy; risks={', '.join(flags)}",
        flags,
        source="tool",
    )


web_search_tool = Tool(
    name="web_search",
    description="Search the web for information.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return",
            },
        },
        "required": ["query", "max_results"],
    },
    call=lambda args, runtime: web_search(**args),
    is_concurrency_safe=lambda args: True,
)

http_request_tool = Tool(
    name="http_request",
    description="Make an HTTP request and return the response.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to request"},
            "method": {
                "type": "string",
                "description": "HTTP method (GET, POST, etc.)",
                "default": "GET",
            },
            "params": {
                "type": "object",
                "description": "Query parameters for GET requests",
            },
            "body": {
                "description": "Request body for POST/PUT requests",
            },
            "headers": {
                "type": "object",
                "description": "HTTP headers",
            },
        },
        "required": ["url"],
    },
    call=lambda args, runtime: http_request(**args),
    check_permission=_ask_http_request,
    is_concurrency_safe=lambda args: str(args.get("method", "GET")).upper()
    in {"GET", "HEAD"},
)
