from typing import Any

from ...domain.model.tool import AccessTarget, ToolAccess, ToolResult
from ...domain.model.tool_definition import ALL_ROLES, Tool
from ..runtime.network import request_target


def http_request(
    url: str,
    method: str = "GET",
    params: dict | None = None,
    body: dict | str | None = None,
    headers: dict | None = None,
    timeout: int = 20,
    *, network,
):
    kwargs: dict[str, Any] = {"params": params, "headers": headers, "timeout": timeout}

    if isinstance(body, dict):
        kwargs["json"] = body
    elif isinstance(body, str):
        kwargs["content"] = body

    operation = "network_read" if method.upper() in {"GET", "HEAD", "OPTIONS"} else "network_write"
    resp = network.request(method, url, operation=operation, **kwargs)
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


def _describe_http_request(args: dict) -> ToolAccess:
    method = str(args.get("method", "GET")).upper()
    operation = "network_read" if method in {"GET", "HEAD", "OPTIONS"} else "network_write"
    flags = ("network_read",) if operation == "network_read" else (
        "network_write", "mutates_remote_state"
    )
    url = str(args.get("url", ""))
    return ToolAccess(
        frozenset({operation}),
        targets=(AccessTarget("url", request_target(method, url, args.get("params")), operation, kind="url", http_method=method),),
        subject=url,
        risk_flags=flags,
        reason=f"HTTP {method} request",
    )


http_request_tool = Tool(
    name="http_request",
    roles=ALL_ROLES,
    description=(
        "Call an HTTP API with a custom method, headers, or body. "
        "Returns the raw response (text is truncated at 4000 characters). "
        "Use web_fetch to read a web page as readable text."
    ),
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
    call=lambda args, runtime: http_request(**args, network=runtime.network),
    access_descriptor=_describe_http_request,
    required_capabilities=frozenset({"network"}),
    approval_details=lambda args, cwd: {"http_method": str(args.get("method", "GET")).upper(), "http_target": args.get("url", ""), "header_count": len(args.get("headers") or {}), "has_body": args.get("body") not in (None, "", {})},
    is_concurrency_safe=lambda args: str(args.get("method", "GET")).upper()
    in {"GET", "HEAD"},
)
