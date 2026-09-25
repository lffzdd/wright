"""Web search and crawling tools (Infrastructure)."""

from __future__ import annotations

from .web_tools import (
    http_request,
    web_search,
)

WebSearchTool = web_search

__all__ = [
    "WebSearchTool",
    "http_request",
    "web_search",
]
