"""McpManager startup: one slow or broken server never blocks the others."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from wright.infrastructure.tools.mcp_client import McpManager, McpServerConfig


class _FakeSession:
    def __init__(self, tool_names):
        self._tools = [
            SimpleNamespace(
                name=name,
                description=f"{name} tool",
                inputSchema={"type": "object", "properties": {}},
            )
            for name in tool_names
        ]

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)


def _config(name: str) -> McpServerConfig:
    return McpServerConfig(name=name, transport="stdio", command="unused")


def _manager(behaviour, *, startup_timeout: float) -> tuple[McpManager, list[str]]:
    closed: list[str] = []
    manager = McpManager(
        [_config(name) for name in behaviour], startup_timeout=startup_timeout
    )

    async def connect(stack, cfg):
        stack.callback(closed.append, cfg.name)
        action = behaviour[cfg.name]
        if action == "hang":
            await asyncio.sleep(3600)
        if action == "fail":
            raise ConnectionError("refused")
        return _FakeSession(action)

    manager._connect_one = connect
    return manager, closed


def test_hanging_server_is_marked_failed_and_others_still_load():
    manager, closed = _manager(
        {"slow": "hang", "good": ["search"], "bad": "fail"}, startup_timeout=0.5
    )
    started = time.monotonic()
    try:
        tools = manager.start()
        elapsed = time.monotonic() - started
        statuses = {row["name"]: row for row in manager.servers()}
    finally:
        manager.shutdown()

    assert [tool.name for tool in tools] == ["mcp__good__search"]
    assert elapsed < 5
    assert statuses["good"]["status"] == "connected"
    assert statuses["slow"]["status"] == "failed"
    assert "TimeoutError" in statuses["slow"]["error"]
    assert statuses["bad"]["status"] == "failed"
    # Every server's resources are released, the cancelled one included.
    assert sorted(closed) == ["bad", "good", "slow"]


def test_servers_connect_in_parallel():
    manager, _closed = _manager(
        {"a": ["one"], "b": ["two"], "c": ["three"]}, startup_timeout=5
    )

    async def slow_connect(stack, cfg, original=manager._connect_one):
        await asyncio.sleep(0.4)
        return await original(stack, cfg)

    manager._connect_one = slow_connect
    started = time.monotonic()
    try:
        tools = manager.start()
        elapsed = time.monotonic() - started
    finally:
        manager.shutdown()

    # Configuration order is kept even though connections overlap.
    assert [tool.name for tool in tools] == [
        "mcp__a__one",
        "mcp__b__two",
        "mcp__c__three",
    ]
    assert elapsed < 1.0
