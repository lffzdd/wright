import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from wright.application.lifecycle import HookDecision
from wright.application.tool_execution.capabilities import assemble_tool_capabilities
from wright.application.tool_execution.dispatch import ToolDispatchService
from wright.application.tool_execution.runtime import tool_runtime_for_session
from wright.domain.model.agent import AgentProfile, CapabilitySnapshot
from wright.domain.model.session import Session
from wright.domain.model.tool import ToolCall, ToolResult
from wright.domain.policy import PermissionResolver, PermissionResponse, ToolAccess
from wright.infrastructure.runtime import ExecutionPath, LocalExecutionBackend
from wright.infrastructure.runtime import local as local_execution
from wright.infrastructure.tools.base import Tool
from wright.infrastructure.tools.file import grep_files, grep_tool, write_file_tool


class _RecordingLifecycle:
    def __init__(self, decision: HookDecision | None = None):
        self.decision = decision
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, payload=None, **_kwargs):
        self.events.append((event, dict(payload or {})))
        if event == "pre_tool_use" and self.decision is not None:
            return self.decision
        return HookDecision()


def _required_tool(call=None) -> Tool:
    return Tool(
        "required",
        "",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        call or (lambda _args, _runtime: ToolResult.success()),
        access_descriptor=lambda _args: ToolAccess.internal_read(
            reason="plan3 test operation"
        ),
    )


@pytest.mark.parametrize(
    "failure_kind",
    ["schema", "unknown", "capability", "hook_deny", "hook_rewrite"],
)
def test_pre_execution_failure_is_published_once(tmp_path, failure_kind):
    lifecycle = _RecordingLifecycle()
    tool = _required_tool()
    registry = {tool.name: tool}
    call = ToolCall(tool.name, {"value": "ok"}, "same-provider-id")
    kwargs = {"lifecycle": lifecycle}

    if failure_kind == "schema":
        call = ToolCall(tool.name, {}, call.id)
    elif failure_kind == "unknown":
        registry = {}
        call = ToolCall("missing", {}, call.id)
    elif failure_kind == "capability":
        kwargs["capability_snapshot"] = CapabilitySnapshot(
            AgentProfile("empty", frozenset()), ()
        )
    elif failure_kind == "hook_deny":
        lifecycle.decision = HookDecision("deny", "blocked by test")
    elif failure_kind == "hook_rewrite":
        lifecycle.decision = HookDecision("allow", updated_input={})

    callbacks = []
    outcome = ToolDispatchService(
        registry,
        assemble_tool_capabilities(None, None, None, workspace_dir=tmp_path),
        **kwargs,
    ).execute([call], on_result=lambda tool_call, result: callbacks.append((tool_call, result)))[0]

    failures = [event for event, _payload in lifecycle.events if event == "tool_failure"]
    assert not outcome.result.ok
    assert len(callbacks) == 1
    assert len(failures) == 1
    assert callbacks[0][0].id == call.id


@pytest.mark.parametrize("approval_failure", ["denied", "commit"])
def test_approval_failure_is_published_once_and_does_not_execute(
    tmp_path, approval_failure
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    lifecycle = _RecordingLifecycle()
    resolver = PermissionResolver(
        approval_handler=(
            lambda _request: PermissionResponse(
                "deny", "rejected by test"
            )
            if approval_failure == "denied"
            else PermissionResponse("allow_session_directory_write")
        )
    )
    commit_calls = []

    def commit(_change):
        commit_calls.append(True)
        if approval_failure == "commit":
            raise OSError("checkpoint unavailable")

    callbacks = []
    outcome = ToolDispatchService(
        {write_file_tool.name: write_file_tool},
        assemble_tool_capabilities(None, None, None, workspace_dir=workspace),
        permission_resolver=resolver,
        authorization_commit=commit,
        lifecycle=lifecycle,
    ).execute(
        [
            ToolCall(
                write_file_tool.name,
                {"file": str(outside), "content": "must not run"},
                "approval-id",
            )
        ],
        on_result=lambda tool_call, result: callbacks.append((tool_call, result)),
    )[0]

    failures = [event for event, _payload in lifecycle.events if event == "tool_failure"]
    assert not outcome.result.ok
    assert len(callbacks) == 1
    assert len(failures) == 1
    assert not outside.exists()
    assert commit_calls == ([] if approval_failure == "denied" else [True])


def test_success_publishes_result_once_without_failure(tmp_path):
    lifecycle = _RecordingLifecycle()
    callbacks = []
    tool = Tool(
        "success_once",
        "",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _args, _runtime: ToolResult.success("ok"),
        access_descriptor=lambda _args: ToolAccess.internal_read(
            reason="success callback test"
        ),
    )

    outcome = ToolDispatchService(
        {tool.name: tool},
        assemble_tool_capabilities(None, None, None, workspace_dir=tmp_path),
        lifecycle=lifecycle,
    ).execute(
        [ToolCall(tool.name, {}, "success-id")],
        on_result=lambda tool_call, result: callbacks.append((tool_call, result)),
    )[0]

    assert outcome.result.ok
    assert len(callbacks) == 1
    assert [event for event, _payload in lifecycle.events if event == "tool_failure"] == []
    assert [event for event, _payload in lifecycle.events if event == "post_tool_use"] == [
        "post_tool_use"
    ]


def test_actual_execution_failure_publishes_once(tmp_path):
    lifecycle = _RecordingLifecycle()
    callbacks = []
    tool = Tool(
        "failure_once",
        "",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda _args, _runtime: ToolResult.fail("expected execution failure"),
        access_descriptor=lambda _args: ToolAccess.internal_read(
            reason="execution failure test"
        ),
    )

    outcome = ToolDispatchService(
        {tool.name: tool},
        assemble_tool_capabilities(None, None, None, workspace_dir=tmp_path),
        lifecycle=lifecycle,
    ).execute(
        [ToolCall(tool.name, {}, "execution-failure-id")],
        on_result=lambda tool_call, result: callbacks.append((tool_call, result)),
    )[0]

    assert not outcome.result.ok
    assert len(callbacks) == 1
    assert [event for event, _payload in lifecycle.events if event == "tool_failure"] == [
        "tool_failure"
    ]


def test_pre_execution_failure_remains_a_concurrency_barrier(tmp_path):
    active = 0
    peak = 0
    lock = threading.Lock()

    def safe_call(_args, _runtime):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.04)
            return ToolResult.success()
        finally:
            with lock:
                active -= 1

    safe = Tool(
        "safe",
        "",
        {"type": "object", "properties": {}, "additionalProperties": False},
        safe_call,
        access_descriptor=lambda _args: ToolAccess.internal_read(
            reason="barrier test"
        ),
        is_concurrency_safe=lambda _args: True,
    )
    invalid = _required_tool()
    outcomes = ToolDispatchService(
        {safe.name: safe, invalid.name: invalid},
        assemble_tool_capabilities(None, None, None, workspace_dir=tmp_path),
    ).execute(
        [
            ToolCall(safe.name, {}, "before"),
            ToolCall(invalid.name, {}, "barrier"),
            ToolCall(safe.name, {}, "after"),
        ]
    )

    assert len(outcomes) == 3
    assert [outcome.call.id for outcome in outcomes] == ["before", "barrier", "after"]
    assert not outcomes[1].result.ok
    assert peak == 1


def _spawn_python(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _writer_script(chunks: list[bytes], *, delay: float = 0.0, tail: str = "") -> str:
    encoded = [base64.b64encode(chunk).decode("ascii") for chunk in chunks]
    return (
        "import base64,sys,time\n"
        f"for value in {encoded!r}:\n"
        "    sys.stdout.buffer.write(base64.b64decode(value))\n"
        "    sys.stdout.buffer.flush()\n"
        f"    time.sleep({delay!r})\n"
        f"{tail}"
    )


def _local_path(tmp_path: Path) -> ExecutionPath:
    return ExecutionPath("local", str(tmp_path.resolve()))


def test_silent_rg_timeout_terminates_and_reaps_process(tmp_path, monkeypatch):
    processes = []

    def start(_argv):
        process = _spawn_python("import time; time.sleep(0.8)")
        processes.append(process)
        return process

    monkeypatch.setattr(local_execution, "_start_rg", start)
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    file = tmp_path / "one.txt"
    file.write_text("needle\n", encoding="utf-8")
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="timed out"):
        list(
            backend.search_files(
                [ExecutionPath("local", str(file))],
                "needle",
                deadline=time.monotonic() + 0.05,
            )
        )

    assert time.monotonic() - started < 0.35
    process = processes[0]
    process.wait(timeout=1)
    assert process.returncode is not None


def test_silent_rg_cancellation_terminates_and_reaps_process(tmp_path, monkeypatch):
    processes = []
    cancelled = threading.Event()

    def start(_argv):
        process = _spawn_python("import time; time.sleep(0.8)")
        processes.append(process)
        return process

    monkeypatch.setattr(local_execution, "_start_rg", start)
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    file = tmp_path / "one.txt"
    file.write_text("needle\n", encoding="utf-8")
    timer = threading.Timer(0.05, cancelled.set)
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            list(
                backend.search_files(
                    [ExecutionPath("local", str(file))],
                    "needle",
                    deadline=time.monotonic() + 5,
                    cancellation_check=cancelled.is_set,
                )
            )
    finally:
        timer.cancel()

    assert time.monotonic() - started < 0.35
    process = processes[0]
    process.wait(timeout=1)
    assert process.returncode is not None


def test_rg_drains_large_stderr_without_deadlock(tmp_path, monkeypatch):
    processes = []

    def start(_argv):
        process = _spawn_python(
            "import sys; sys.stderr.buffer.write(b'x' * 524288); "
            "sys.stderr.buffer.flush(); raise SystemExit(1)"
        )
        processes.append(process)
        return process

    monkeypatch.setattr(local_execution, "_start_rg", start)
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    file = tmp_path / "one.txt"
    file.write_text("needle\n", encoding="utf-8")
    result = []
    error = []

    def collect():
        try:
            result.extend(
                backend.search_files(
                    [ExecutionPath("local", str(file))], "needle"
                )
            )
        except BaseException as exc:  # surfaced in the main test thread
            error.append(exc)

    worker = threading.Thread(target=collect)
    worker.start()
    worker.join(timeout=1)
    if worker.is_alive():
        for process in processes:
            process.terminate()
        worker.join(timeout=2)
        pytest.fail("search remained blocked while stderr filled its pipe")

    assert error == []
    assert result == []
    assert processes[0].returncode == 1


def test_rg_candidate_nul_records_and_json_chunks_are_preserved(tmp_path, monkeypatch):
    odd = tmp_path / ("odd name Ω.txt" if os.name == "nt" else "odd\n\r name.txt")
    ordinary = tmp_path / "ordinary.txt"
    odd.write_text("needle\n", encoding="utf-8")
    ordinary.write_text("needle\n", encoding="utf-8")
    processes = []
    calls = 0
    match = {
        "type": "match",
        "data": {
            "path": {"text": str(odd)},
            "lines": {"text": "needle\n"},
            "line_number": 1,
            "submatches": [{"start": 0, "end": 6}],
        },
    }

    def start(_argv):
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = os.fsencode(str(ordinary)) + b"\0" + os.fsencode(str(odd)) + b"\0"
            script = _writer_script([payload[:5], payload[5:]], delay=0.01)
        else:
            payload = json.dumps(match).encode() + b"\n"
            script = _writer_script([payload[:7], payload[7:19], payload[19:]], delay=0.01)
        process = _spawn_python(script)
        processes.append(process)
        return process

    monkeypatch.setattr(local_execution, "_start_rg", start)
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    candidates = list(backend.iter_search_candidates(_local_path(tmp_path)))
    assert {path.value for path in candidates} == {
        str(ordinary.resolve()), str(odd.resolve())
    }
    matches = list(
        backend.search_files(candidates, "needle", fixed_string=True)
    )
    assert [(item.path.value, item.line, item.column) for item in matches] == [
        (str(odd.resolve()), 1, 1)
    ]
    for process in processes:
        process.wait(timeout=1)
        assert process.returncode == 0


def test_search_generator_close_reaps_process(tmp_path, monkeypatch):
    file = tmp_path / "one.txt"
    file.write_text("needle\n", encoding="utf-8")
    event = {
        "type": "match",
        "data": {
            "path": {"text": str(file)},
            "lines": {"text": "needle\n"},
            "line_number": 1,
            "submatches": [{"start": 0, "end": 6}],
        },
    }
    processes = []

    def start(_argv):
        process = _spawn_python(
            _writer_script(
                [json.dumps(event).encode() + b"\n"],
                tail="time.sleep(10)\n",
            )
        )
        processes.append(process)
        return process

    monkeypatch.setattr(local_execution, "_start_rg", start)
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    results = backend.search_files(
        [ExecutionPath("local", str(file))], "needle", fixed_string=True
    )
    assert next(results).path.value == str(file.resolve())
    results.close()
    processes[0].wait(timeout=1)
    assert processes[0].returncode is not None


def test_grep_max_results_reaps_search_process(tmp_path, monkeypatch):
    file = tmp_path / "one.txt"
    file.write_text("needle\nneedle\n", encoding="utf-8")
    session = Session.create("max results", tmp_path)
    runtime = tool_runtime_for_session(session, workspace_dir=tmp_path)
    events = []
    processes = []
    match = {
        "type": "match",
        "data": {
            "path": {"text": str(file)},
            "lines": {"text": "needle\n"},
            "line_number": 1,
            "submatches": [{"start": 0, "end": 6}],
        },
    }

    def start(_argv):
        if not events:
            script = _writer_script([os.fsencode(str(file)) + b"\0"])
            events.append("candidates")
        else:
            payload = json.dumps(match).encode() + b"\n"
            script = _writer_script(
                [payload, payload], tail="time.sleep(10)\n"
            )
        process = _spawn_python(script)
        processes.append(process)
        return process

    monkeypatch.setattr(local_execution, "_start_rg", start)
    result = grep_files("needle", max_results=1, runtime=runtime)

    assert result.ok
    assert len(result.data["matches"]) == 1
    processes[-1].wait(timeout=1)
    assert processes[-1].returncode is not None


def test_grep_uses_one_deadline_for_candidates_and_multiple_batches(tmp_path):
    files = []
    for index in range(129):
        file = tmp_path / f"{index:03}.txt"
        file.write_text("needle\n", encoding="utf-8")
        files.append(file)

    class RecordingBackend(LocalExecutionBackend):
        def __init__(self):
            super().__init__(tmp_path, lambda: tmp_path)
            self.deadlines = []

        def iter_search_candidates(self, path, **kwargs):
            self.deadlines.append(kwargs["deadline"])
            yield from (ExecutionPath("local", str(file)) for file in files)

        def search_files(self, paths, pattern, **kwargs):
            paths = list(paths)
            for _start in range(0, len(paths), 128):
                self.deadlines.append(kwargs["deadline"])
            yield from ()

    backend = RecordingBackend()
    runtime = tool_runtime_for_session(
        Session.create("deadline", tmp_path),
        workspace_dir=tmp_path,
        execution_backend=backend,
    )
    result = grep_files("needle", runtime=runtime)

    assert result.ok
    assert len(backend.deadlines) == 3
    assert len(set(backend.deadlines)) == 1


def _grep_rows_from_rg(root: Path, glob: str | None = None):
    argv = ["rg", "--json", "--color", "never", "--no-heading"]
    if glob is not None:
        argv.extend(("--glob", glob))
    argv.extend(("-e", "needle", "--", str(root)))
    completed = subprocess.run(argv, check=False, capture_output=True)
    assert completed.returncode in (0, 1)
    rows = []
    for raw in completed.stdout.splitlines():
        event = json.loads(raw)
        if event.get("type") != "match":
            continue
        data = event["data"]
        path = Path(data["path"]["text"]).resolve().relative_to(root.resolve())
        text = data["lines"]["text"].rstrip("\r\n")
        for submatch in data["submatches"]:
            rows.append((path.as_posix(), data["line_number"], submatch["start"] + 1, text))
    return sorted(rows)


def _grep_rows(result):
    return sorted(
        (item["path"], item["line"], item["column"], item["text"])
        for item in result.data["matches"]
    )


def test_grep_matches_actual_rg_for_gitignore_hidden_and_special_names(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("needle\n", encoding="utf-8")
    odd = tmp_path / ("odd name Ω.txt" if os.name == "nt" else "odd\n\r name.txt")
    odd.write_text("needle\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "code.py").write_text("needle\n", encoding="utf-8")
    (nested / "notes.md").write_text("needle\n", encoding="utf-8")
    runtime = tool_runtime_for_session(
        Session.create("rg comparison", tmp_path), workspace_dir=tmp_path
    )

    for glob in (None, "*.py", "!*.py", "nested/*.py"):
        result = grep_files("needle", glob=glob, runtime=runtime)
        assert result.ok
        assert _grep_rows(result) == _grep_rows_from_rg(tmp_path, glob)


def test_grep_explicit_file_ignores_directory_glob_filter(tmp_path):
    ordinary = tmp_path / "ordinary.txt"
    hidden = tmp_path / ".hidden.txt"
    ordinary.write_text("needle\n", encoding="utf-8")
    hidden.write_text("needle\n", encoding="utf-8")
    runtime = tool_runtime_for_session(
        Session.create("explicit file", tmp_path), workspace_dir=tmp_path
    )

    for target in (ordinary, hidden):
        for glob in (None, "*.py", "!*.py"):
            result = grep_files(
                "needle", path=str(target), glob=glob, runtime=runtime
            )
            assert result.ok
            assert [item["path"] for item in result.data["matches"]] == [
                target.name
            ]


def test_protected_and_external_symlink_candidates_never_reach_search_backend(
    tmp_path, monkeypatch
):
    protected = tmp_path / "permission_settings.json"
    protected.write_text("needle protected\n", encoding="utf-8")
    allowed = tmp_path / "allowed.txt"
    allowed.write_text("needle allowed\n", encoding="utf-8")
    external = tmp_path.parent / "external-needle.txt"
    external.write_text("needle external\n", encoding="utf-8")
    link = tmp_path / "external-link.txt"
    link.symlink_to(external)
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path))

    class RecordingBackend(LocalExecutionBackend):
        def __init__(self):
            super().__init__(tmp_path, lambda: tmp_path)
            self.seen = []

        def search_files(self, paths, pattern, **kwargs):
            paths = list(paths)
            self.seen.extend(paths)
            return super().search_files(paths, pattern, **kwargs)

    backend = RecordingBackend()
    result = ToolDispatchService(
        {grep_tool.name: grep_tool},
        assemble_tool_capabilities(
            None, None, None,
            workspace_dir=tmp_path,
            cwd_provider=lambda: tmp_path,
            execution_backend=backend,
        ),
    ).execute([ToolCall(grep_tool.name, {"pattern": "needle"}, "protected-symlink")])[0].result

    assert result.ok
    assert [item["path"] for item in result.data["matches"]] == ["allowed.txt"]
    assert all(path.value not in {str(protected.resolve()), str(external.resolve())} for path in backend.seen)
