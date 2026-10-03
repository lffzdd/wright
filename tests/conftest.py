"""Tests never read or modify the developer's real permission state."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_permission_state(tmp_path, monkeypatch, request):
    if request.node.get_closest_marker("native_sandbox") and os.getenv("WRIGHT_SANDBOX_INTEGRATION") == "1":
        return
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "wright-state"))
    monkeypatch.delenv("WRIGHT_PERMISSION_CONFIG", raising=False)
