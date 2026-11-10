"""Regressions found by driving the web console against a scripted model."""

from __future__ import annotations

import pytest

from .conftest import ENABLED, tool_call

pytestmark = pytest.mark.skipif(not ENABLED, reason="set WRIGHT_E2E_WEB=1 to run browser end-to-end tests")


def test_a_model_error_ends_the_turn_not_the_session(stack, console):
    console.new_task()
    stack.script([{"quirk": "http400"}])
    console.send("trigger an error")
    assert "Model request failed (HTTP 400): fake bad request" in console.timeline()

    stack.script([{"content": "recovered after error", "finish_reason": "stop"}])
    console.send("try again")
    text = console.timeline()
    assert "recovered after error" in text
    assert text.index("Model request failed") < text.index("try again")


@pytest.mark.parametrize(("case", "reply"), [
    ("empty_args", {"tool_calls": [tool_call("b1", "ls", "")]}),
    ("repeat_id_name", {"tool_calls": [tool_call("c1", "ls", {})], "quirk": "repeat_id_name"}),
    ("no_index", {"tool_calls": [tool_call("i1", "read", {"file": "hello.txt"}),
                                 tool_call("i2", "ls", {})], "quirk": "no_index"}),
])
def test_openai_compatible_stream_variants(stack, console, case, reply):
    console.new_task()
    stack.script([reply, {"content": f"done {case}", "finish_reason": "stop"}])
    console.send(case, wait_ms=4500)
    text = console.timeline()
    assert f"done {case}" in text
    assert "could not be parsed" not in text


def test_live_timeline_matches_history(stack, console):
    console.new_task()
    stack.script([
        {"content": "Step one narration.", "tool_calls": [tool_call("m1", "ls", {})]},
        {"content": "Step two narration.", "tool_calls": [tool_call("m2", "read", {"file": "hello.txt"})]},
        {"content": "Final summary.", "finish_reason": "stop"},
    ])
    console.send("multi step", wait_ms=5000)

    def segment() -> str:
        text = console.timeline()
        return text[text.rfind("multi step"):]

    live = segment()
    assert live.index("Step one narration.") < live.index("Step two narration.") < live.index("Final summary.")
    assert "entries" in live  # list_directory reports its entry count
    console.page.reload()
    console.page.wait_for_timeout(2500)
    replay = segment()
    assert all(text in replay for text in ("Step one narration.", "Step two narration.", "Final summary."))


def test_stop_interrupts_streaming_and_leaves_no_stale_banner(stack, console):
    console.new_task()
    stack.script([{"content": "slow " * 40, "quirk": "slow", "finish_reason": "stop"}])
    console.send("stream slowly", wait_ms=2200)
    console.page.get_by_role("button", name="Stop", exact=False).first.click()
    console.page.wait_for_timeout(2500)
    assert console.timeline().count("slow") < 30

    stack.script([{"content": "fine answer", "finish_reason": "stop"}])
    console.send("normal one")
    text = console.timeline()
    after = text[text.rfind("normal one"):]
    assert "fine answer" in after
    assert "cancelled" not in after.lower()


def test_finished_sessions_never_block_a_new_task(stack, console):
    for index in range(5):
        console.new_task()
        stack.script([{"content": f"answer {index}", "finish_reason": "stop"}])
        console.send(f"filler {index}", wait_ms=2500)
        assert f"answer {index}" in console.timeline()
    sessions = console.sessions()
    assert sum(1 for item in sessions if item.get("active")) <= 4
    assert not any(item["session_id"] == "open_requests" for item in sessions)


def test_one_session_rule_covers_repeated_edits(stack, console):
    console.new_task()
    stack.script([
        {"content": "Reading.", "tool_calls": [tool_call("e0", "read", {"file": "src/calc.py"})]},
        {"content": "Editing.", "tool_calls": [tool_call("e1", "edit", {"file": "src/calc.py", "old_text": "a - b", "new_text": "a + b"})]},
        {"content": "Again.", "tool_calls": [tool_call("e2", "edit", {"file": "src/calc.py", "old_text": "a + b", "new_text": "b + a"})]},
        {"content": "Both edits done.", "finish_reason": "stop"},
    ])
    console.send("edit twice", wait_ms=4000)
    assert "Wright wants to change a file" in console.page.inner_text(".approval-card")
    console.page.get_by_role("button", name="Allow edits to everything in src/", exact=False).first.click()
    console.page.wait_for_timeout(4000)
    assert "Both edits done." in console.timeline()
    assert console.page.query_selector(".approval-card") is None
    assert (stack.workspace / "src" / "calc.py").read_text().strip().endswith("b + a")
    console.page.get_by_text("Permissions", exact=True).first.click()
    console.page.wait_for_timeout(1500)
    assert "Edit anything in" in console.page.inner_text("body")


def test_reading_outside_the_project_makes_no_failing_requests(stack, console):
    failures: list[str] = []

    def record(response) -> None:
        if response.status >= 400 and "/api/" in response.url:
            failures.append(f"{response.status} {response.url}")

    console.page.on("response", record)
    try:
        console.new_task()
        stack.script([
            {"tool_calls": [tool_call("o1", "read", {"file": "/etc/hostname"})]},
            {"content": "read it", "finish_reason": "stop"},
        ])
        console.send("read a system file", wait_ms=3500)
        card = console.page.inner_text(".approval-card")
        assert "Outside this project" in card
        console.page.get_by_role("button", name="Allow Once").first.click()
        console.page.wait_for_timeout(3000)
        console.page.get_by_text("Task", exact=True).first.click()
        console.page.wait_for_timeout(2500)
        assert "read it" in console.timeline()
    finally:
        console.page.remove_listener("response", record)
    assert not [item for item in failures if "/tree" in item or "/changes" in item], failures
