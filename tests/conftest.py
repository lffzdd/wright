"""Tests never read or modify the developer's real permission state."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_permission_state(tmp_path, monkeypatch, request):
    if request.node.get_closest_marker("native_sandbox") and os.getenv("WRIGHT_SANDBOX_INTEGRATION") == "1":
        return
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "wright-state"))
    monkeypatch.delenv("WRIGHT_PERMISSION_CONFIG", raising=False)


@pytest.fixture(autouse=True)
def no_session_title_queries(monkeypatch):
    """Session naming is an extra model call; tests that count calls opt in explicitly."""
    monkeypatch.setenv("WRIGHT_AUTO_TITLE", "0")


@pytest.fixture(autouse=True)
def public_test_resolver(monkeypatch, request):
    """HTTP tools check where a host resolves; tests use fake public hosts."""
    if request.node.get_closest_marker("real_resolver"):
        return
    from wright.infrastructure.runtime import network

    monkeypatch.setattr(network, "resolve_addresses", lambda host, port: ["93.184.215.14"])


@pytest.fixture(autouse=True)
def clear_web_tool_caches():
    from wright.infrastructure.tools.web_fetch import clear_web_fetch_cache
    from wright.infrastructure.tools.web_search import clear_web_search_cache

    clear_web_fetch_cache()
    clear_web_search_cache()
    yield
    clear_web_fetch_cache()
    clear_web_search_cache()
