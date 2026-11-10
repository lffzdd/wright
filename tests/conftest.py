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
