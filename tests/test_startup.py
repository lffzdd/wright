"""Entry-point smoke tests without a model request or user-level state."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx

from tests.paths import REPO_ROOT, SRC_ROOT


def _startup_env(tmp_path):
    env = os.environ.copy()
    env["WRIGHT_HOME"] = str(tmp_path / "wright-home")
    env["PYTHONPATH"] = str(SRC_ROOT)
    return env


def test_cli_help_starts_in_a_fresh_interpreter(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "wright.main", "--help"],
        cwd=REPO_ROOT,
        env=_startup_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--ui" in result.stdout


def test_web_entry_point_serves_the_frontend(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable, "-m", "wright.main", "--ui", "web", "--no-open",
            "--web-port", str(port), "--workspace", str(tmp_path),
        ],
        cwd=REPO_ROOT,
        env=_startup_env(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(process.communicate(timeout=3)[1])
                try:
                    response = client.get(f"http://127.0.0.1:{port}/")
                except httpx.TransportError:
                    time.sleep(0.05)
                    continue
                assert response.status_code == 200
                assert "text/html" in response.headers["content-type"]
                break
            else:
                raise AssertionError("Web entry point did not start in time")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
