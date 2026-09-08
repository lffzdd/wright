import json
import sys
import threading
import time

from ...executor import ToolExecutor
from ...lifecycle import (
    HookDecision,
    HookRegistration,
    LifecycleManager,
    TraceRecorder,
    load_lifecycle_manager,
)
from ...tools.base import Tool, ToolCall, ToolResult


def _tool(call):
    return Tool(
        name="sample",
        description="sample",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        call=call,
    )


def test_trace_is_append_only_redacted_and_resumes_sequence(tmp_path):
    recorder = TraceRecorder(tmp_path / "trace.jsonl")
    first = LifecycleManager("session", recorder)
    first.emit("pre_tool_use", {
        "tool_name": "sample",
        "arguments": {"api_key": "secret", "value": "x" * 9_000},
    })
    second = LifecycleManager("session", recorder)
    second.emit("post_tool_use", {"tool_name": "sample", "result": {"ok": True}})

    rows = recorder.read()
    assert [row["sequence"] for row in rows] == [1, 2]
    assert rows[0]["payload"]["arguments"]["api_key"] == "[redacted]"
    assert rows[0]["payload"]["arguments"]["value"].endswith("[truncated]")


def test_trace_recovers_after_a_crash_truncated_tail(tmp_path):
    path = tmp_path / "trace.jsonl"
    first = LifecycleManager("session", TraceRecorder(path))
    first.emit("agent_start", {})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"partial":')

    resumed = LifecycleManager("session", TraceRecorder(path))
    resumed.emit("agent_stop", {"status": "completed"})

    rows = resumed.recorder.read()
    assert [row["sequence"] for row in rows] == [1, 2]


def test_explicit_pre_tool_deny_blocks_execution(tmp_path):
    called = []
    manager = LifecycleManager("session")
    manager.register(HookRegistration(
        event="pre_tool_use",
        matcher="sample",
        name="block-sample",
        callback=lambda event: HookDecision("deny", "blocked in test"),
    ))
    executor = ToolExecutor(
        {"sample": _tool(lambda arguments, runtime: called.append(arguments))},
        workspace_dir=tmp_path,
        lifecycle=manager,
    )

    outcome = executor.execute([ToolCall("sample", {"value": 1}, "call_1")])[0]

    assert not outcome.result.ok
    assert "Hook denied" in outcome.result.err
    assert called == []


def test_hook_rewrite_is_revalidated_and_does_not_mutate_recorded_call(tmp_path):
    received = []
    manager = LifecycleManager("session")
    manager.register(HookRegistration(
        event="pre_tool_use",
        matcher="sample",
        callback=lambda event: {"updated_input": {"value": 2}},
    ))
    tool = _tool(
        lambda arguments, runtime: (
            received.append(arguments.copy()) or ToolResult.success(arguments)
        )
    )
    original = ToolCall("sample", {"value": 1}, "call_1")

    outcome = ToolExecutor(
        {"sample": tool}, workspace_dir=tmp_path, lifecycle=manager
    ).execute([original])[0]

    assert outcome.result.ok
    assert received == [{"value": 2}]
    assert outcome.call.arguments == {"value": 2}
    assert original.arguments == {"value": 1}

    invalid = LifecycleManager("session-2")
    invalid.register(HookRegistration(
        event="pre_tool_use",
        matcher="sample",
        callback=lambda event: {"updated_input": {"value": "wrong"}},
    ))
    failed = ToolExecutor(
        {"sample": tool}, workspace_dir=tmp_path, lifecycle=invalid
    ).execute([ToolCall("sample", {"value": 1}, "call_2")])[0]
    assert not failed.result.ok
    assert "InputValidationError" in failed.result.err
    assert received == [{"value": 2}]


def test_hook_rewrite_is_applied_before_concurrency_partition(tmp_path):
    active = 0
    peak = 0
    lock = threading.Lock()

    def call(arguments, runtime):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return ToolResult.success(arguments)

    tool = Tool(
        name="mode_tool",
        description="mode",
        parameters={
            "type": "object",
            "properties": {"mode": {"enum": ["read", "write"]}},
            "required": ["mode"],
            "additionalProperties": False,
        },
        call=call,
        is_concurrency_safe=lambda arguments: arguments["mode"] == "read",
    )
    manager = LifecycleManager("session")
    manager.register(HookRegistration(
        event="pre_tool_use",
        matcher="mode_tool",
        callback=lambda event: (
            {"updated_input": {"mode": "write"}}
            if event.payload["tool_call_id"] == "c1"
            else None
        ),
    ))

    outcomes = ToolExecutor(
        {"mode_tool": tool}, workspace_dir=tmp_path, lifecycle=manager
    ).execute([
        ToolCall("mode_tool", {"mode": "read"}, "c1"),
        ToolCall("mode_tool", {"mode": "read"}, "c2"),
    ])

    assert [outcome.call.arguments["mode"] for outcome in outcomes] == [
        "write", "read"
    ]
    assert peak == 1


def test_hook_crash_is_traced_and_fails_open(tmp_path):
    recorder = TraceRecorder(tmp_path / "trace.jsonl")
    manager = LifecycleManager("session", recorder)

    def broken(event):
        raise RuntimeError("boom")

    manager.register(HookRegistration(
        event="pre_tool_use", matcher="sample", name="broken", callback=broken
    ))
    decision = manager.emit("pre_tool_use", {"tool_name": "sample"})

    assert decision.decision == "allow"
    assert [row["event"] for row in recorder.read()] == [
        "pre_tool_use", "hook_error"
    ]


def test_command_hook_config_uses_argv_and_matcher(tmp_path):
    config = tmp_path / ".wright_hooks.json"
    config.write_text(json.dumps({
        "hooks": {
            "pre_tool_use": [{
                "name": "rewrite",
                "matcher": "sample",
                "command": [
                    sys.executable,
                    "-c",
                    "import json; print(json.dumps({'updated_input': {'value': 7}}))",
                ],
            }]
        }
    }), encoding="utf-8")
    manager = load_lifecycle_manager(tmp_path, "session", config_path=config)

    matched = manager.emit(
        "pre_tool_use", {"tool_name": "sample", "arguments": {"value": 1}}
    )
    skipped = manager.emit(
        "pre_tool_use", {"tool_name": "other", "arguments": {"value": 1}}
    )

    assert matched.updated_input == {"value": 7}
    assert skipped.updated_input is None
    events = [row["event"] for row in manager.recorder.read()]
    assert events == ["pre_tool_use", "hook_result", "pre_tool_use"]
