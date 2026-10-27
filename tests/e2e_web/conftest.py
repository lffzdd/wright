"""Real browser against a real `wright --ui web` and a scripted model.

These tests start processes and a headless Chromium, so they only run when
asked: ``WRIGHT_E2E_WEB=1 uv run --group e2e pytest tests/e2e_web`` (install
the browser once with ``uv run --group e2e playwright install chromium``).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).parent

ENABLED = os.environ.get("WRIGHT_E2E_WEB") == "1"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def tool_call(call_id: str, name: str, arguments: dict | str) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}


@dataclass
class Stack:
    url: str
    root: Path
    workspace: Path
    queue_file: Path

    @property
    def base(self) -> str:
        return self.url.split("#", 1)[0]

    def script(self, replies: list[dict]) -> None:
        """Replace whatever the model will answer next."""
        self.queue_file.write_text(json.dumps(replies))


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    root = tmp_path_factory.mktemp("wright-e2e")
    workspace = root / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "hello.txt").write_text("hello from the workspace\n")
    (workspace / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.email=e2e@test", "-c", "user.name=e2e", "commit", "-qm", "init"], cwd=workspace, check=True)
    queue = root / "queue.json"
    queue.write_text("[]")
    llm_port, web_port = _free_port(), _free_port()
    env = {**os.environ, "FAKE_LLM_QUEUE": str(queue), "FAKE_LLM_LOG": str(root / "requests.jsonl")}
    model = subprocess.Popen([sys.executable, str(HERE / "fake_llm.py"), str(llm_port)], env=env)
    web_log = (root / "web.log").open("w")
    web = subprocess.Popen(
        [sys.executable, "-m", "wright.main", "--ui", "web", "--no-open",
         "--web-port", str(web_port), "--workspace", str(workspace)],
        env={**os.environ, "OPENAI_BASE_URL": f"http://127.0.0.1:{llm_port}/v1",
             "OPENAI_API_KEY": "sk-e2e", "OPENAI_MODEL": "fake-model",
             "WRIGHT_HOME": str(root / "home")},
        stdout=web_log, stderr=subprocess.STDOUT,
    )
    url = ""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not url:
        match = re.search(r"Wright Web: (\S+)", (root / "web.log").read_text())
        url = match.group(1) if match else ""
        time.sleep(0.2)
    try:
        assert url, (root / "web.log").read_text()
        yield Stack(url=url, root=root, workspace=workspace, queue_file=queue)
    finally:
        web.terminate()
        model.terminate()
        web.wait(timeout=10)
        model.wait(timeout=10)
        web_log.close()


@pytest.fixture(scope="module")
def page(stack):
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as driver:
        browser = driver.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        tab = context.new_page()
        tab.goto(stack.url)
        tab.wait_for_timeout(2000)
        yield tab
        browser.close()


class Console:
    """Small vocabulary for the test bodies."""

    def __init__(self, stack: Stack, page) -> None:
        self.stack = stack
        self.page = page

    def new_task(self) -> None:
        self.page.get_by_role("button", name="New session").first.click()
        self.page.wait_for_timeout(800)

    def send(self, text: str, wait_ms: int = 3500) -> None:
        box = self.page.get_by_placeholder("Instruct the agent", exact=False)
        box.click()
        box.fill(text)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(wait_ms)

    def timeline(self) -> str:
        return self.page.inner_text("main")

    def sessions(self) -> list[dict]:
        return self.page.request.get(self.stack.base + "api/v1/sessions").json()


@pytest.fixture
def console(stack, page) -> Console:
    stack.script([])
    return Console(stack, page)
