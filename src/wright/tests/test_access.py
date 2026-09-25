import fnmatch
import posixpath
import re
import shlex
import threading
from pathlib import Path

from ..app.tool_runtime import tool_runtime_for_session
from ..domain.session import Session
from ..engine.executor import ToolExecutor
from ..execution import AuthorizedExecution, LocalExecutionBackend
from ..permission import (
    AccessScope,
    AccessTarget,
    GrantTarget,
    InvocationGrant,
    InvocationIdentity,
    PathClass,
    ToolAccess,
    forbidden_paths,
)
from ..tool_capabilities import assemble_tool_capabilities
from ..tool_protocol import ToolCall, ToolResult
from ..tools.base import Tool
from ..tools.command_tools import execute_command
from ..tools.file_tools import (
    edit_file,
    grep_files,
    grep_tool,
    read_file,
    read_file_tool,
    write_file,
)


class _MemoryProcess:
    """A ProcessHandle test double with no subprocess implementation behind it."""

    def __init__(self, output: tuple[str, ...], cwd, *, complete: bool):
        self._output = output
        self._cwd = cwd
        self._done = threading.Event()
        self._returncode: int | None = None
        if complete:
            self._returncode = 0
            self._done.set()

    def iter_output(self):
        return iter(self._output)

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise TimeoutError("memory process did not finish")
        assert self._returncode is not None
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode if self._done.is_set() else None

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def terminate(self) -> None:
        self._returncode = -15
        self._done.set()

    def cwd_result(self):
        return self._cwd if self._done.is_set() else None


class _MemoryExecutionBackend:
    """Independent backend: all state is in dictionaries/events, never local I/O."""

    environment_id = "memory"

    def __init__(self):
        self.root = "/memory/workspace"
        self.files = {
            f"{self.root}/notes.txt": "needle\nsecond line\n",
        }
        self.directories = {self.root}
        self.modified: dict[str, int] = dict.fromkeys(self.files, 1)
        self.shells: list[tuple[str, _MemoryProcess]] = []

    def _normalize(self, value: str, cwd: str | None = None) -> str:
        raw = str(value)
        if not raw.startswith("/"):
            raw = posixpath.join(cwd or self.root, raw)
        return posixpath.normpath(raw)

    def cwd(self):
        from ..execution import ExecutionPath

        return ExecutionPath(self.environment_id, self.root)

    def resolve_path(self, requested: str, *, cwd=None):
        from ..execution import ExecutionPath

        return ExecutionPath(
            self.environment_id,
            self._normalize(requested, cwd.value if cwd is not None else None),
        )

    def revalidate_path(self, path):
        from ..execution import ExecutionPath

        if path.environment_id != self.environment_id:
            raise ValueError("wrong memory environment")
        return ExecutionPath(self.environment_id, self._normalize(path.value))

    def display_path(self, path):
        value = self.revalidate_path(path).value
        if value == self.root:
            return "."
        prefix = self.root + "/"
        return value.removeprefix(prefix) if value.startswith(prefix) else value

    def metadata(self, path):
        from ..execution import FileMetadata

        value = self.revalidate_path(path).value
        if value in self.files:
            return FileMetadata("file", len(self.files[value].encode()), self.modified[value])
        if value in self.directories:
            return FileMetadata("directory")
        return FileMetadata("missing")

    def read_bytes(self, path, limit=None):
        data = self.files[self.revalidate_path(path).value].encode()
        return data if limit is None else data[:limit]

    def read_text(self, path, *, encoding, errors="strict"):
        return self.files[self.revalidate_path(path).value]

    def write_text(self, path, content, *, encoding):
        value = self.revalidate_path(path).value
        self.files[value] = content
        self.modified[value] = self.modified.get(value, 0) + 1

    def ensure_directory(self, path):
        value = self.revalidate_path(path).value
        pending = []
        while value not in self.directories:
            pending.append(value)
            parent = posixpath.dirname(value)
            if parent == value:
                break
            value = parent
        self.directories.update(reversed(pending))

    def _children(self, root):
        root_value = self.revalidate_path(root).value.rstrip("/")
        prefix = root_value + "/"
        values = set(self.files) | self.directories
        children = set()
        for value in values:
            if not value.startswith(prefix):
                continue
            tail = value[len(prefix):]
            if "/" not in tail:
                children.add(value)
        return sorted(children)

    def iter_directory(self, path):
        from ..execution import DirectoryEntry

        for value in self._children(path):
            from ..execution import ExecutionPath

            child = ExecutionPath(self.environment_id, value)
            yield DirectoryEntry(child, self.metadata(child))

    def glob(self, path, pattern):
        from ..execution import DirectoryEntry, ExecutionPath

        root = self.revalidate_path(path).value.rstrip("/")
        prefix = root + "/"
        for value in sorted(set(self.files) | set(self.directories)):
            if value.startswith(prefix) and fnmatch.fnmatch(value[len(prefix):], pattern):
                child = ExecutionPath(self.environment_id, value)
                yield DirectoryEntry(child, self.metadata(child))

    def iter_search_candidates(self, path, *, glob=None, deadline=None, cancellation_check=None):
        from ..execution import ExecutionPath

        root = self.revalidate_path(path).value.rstrip("/")
        prefix = root + "/"
        candidates = [
            value for value in sorted(self.files)
            if value == root or value.startswith(prefix)
        ]
        for value in candidates:
            relative = value[len(prefix):] if value.startswith(prefix) else posixpath.basename(value)
            if glob and not fnmatch.fnmatch(relative, glob):
                continue
            yield ExecutionPath(self.environment_id, value)

    def search_files(self, paths, pattern, *, case_sensitive=False, fixed_string=False,
                     deadline=None, cancellation_check=None):
        from ..execution import ExecutionPath, SearchMatch

        expression = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        for path in paths:
            value = self.revalidate_path(path).value
            for line_number, line in enumerate(self.files[value].splitlines(), 1):
                if fixed_string:
                    haystack = line if case_sensitive else line.lower()
                    index = haystack.find(pattern if case_sensitive else pattern.lower())
                else:
                    match = expression.search(line)
                    index = match.start() if match else -1
                if index >= 0:
                    yield SearchMatch(
                        ExecutionPath(self.environment_id, value),
                        line_number,
                        index + 1,
                        line,
                    )

    def start_shell(self, command, *, cwd):
        complete = command != "background"
        output = ("fake-output\n",) if command == "printf fake-output" else ()
        process = _MemoryProcess(output, cwd, complete=complete)
        self.shells.append((command, process))
        return process


def test_independent_backend_covers_files_search_shell_and_cancellation():
    backend = _MemoryExecutionBackend()
    root = Path(backend.root)
    session = Session.create("memory backend", root)
    runtime = tool_runtime_for_session(
        session,
        workspace_dir=root,
        execution_backend=backend,
    )

    read_result = read_file("notes.txt", runtime=runtime)
    assert read_result.ok and "needle" in read_result.data["content"]
    edited = edit_file(
        "notes.txt", "needle", "changed", runtime=runtime
    )
    assert edited.ok
    written = write_file("nested/new.txt", "new content", runtime=runtime)
    assert written.ok
    searched = grep_files("changed", runtime=runtime)
    assert searched.ok and searched.data["matches"][0]["path"] == "notes.txt"

    shell = execute_command("printf fake-output", runtime=runtime)
    assert shell.ok and "fake-output" in shell.data["output"]

    background = execute_command("background", run_in_background=True, runtime=runtime)
    assert background.ok
    task_id = background.data["task_id"]
    resources = runtime.runtime_resources.process_registry.get(task_id)
    assert resources is not None and resources.process.poll() is None
    resources.process.terminate()
    assert resources.done.wait(1)
    assert resources.process.wait(1) == -15
    assert backend.shells[-1][0] == "background"
    runtime.runtime_resources.close()


def test_invocation_grant_is_minimal_and_closes_cleanly(tmp_path):
    (tmp_path / "allowed.txt").write_text("allowed", encoding="utf-8")
    (tmp_path / "sibling.txt").write_text("sibling", encoding="utf-8")
    backend = LocalExecutionBackend(tmp_path, lambda: tmp_path)
    cwd = backend.cwd()
    allowed = backend.resolve_path("allowed.txt", cwd=cwd)
    grant = InvocationGrant(
        identity=InvocationIdentity("session", call_id="call"),
        environment_id=backend.environment_id,
        cwd=cwd,
        operations=frozenset({"file_read"}),
        targets=(GrantTarget(allowed, "file_read"),),
    )
    execution = AuthorizedExecution(backend, grant)

    assert execution.read_text(allowed, encoding="utf-8") == "allowed"
    sibling = backend.resolve_path("sibling.txt", cwd=cwd)
    try:
        execution.read_text(sibling, encoding="utf-8")
    except PermissionError:
        pass
    else:
        raise AssertionError("sibling file escaped the exact invocation grant")
    try:
        execution.write_text(allowed, "changed", encoding="utf-8")
    except PermissionError:
        pass
    else:
        raise AssertionError("read-only grant acquired write access")

    execution.close()
    try:
        execution.read_text(allowed, encoding="utf-8")
    except PermissionError:
        pass
    else:
        raise AssertionError("closed invocation remained usable")


def test_production_executor_exposes_only_authorized_execution(tmp_path):
    allowed = tmp_path / "allowed.txt"
    sibling = tmp_path / "sibling.txt"
    outside = tmp_path.parent / "outside.txt"
    allowed.write_text("allowed", encoding="utf-8")
    sibling.write_text("sibling", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    observed = {}

    def describe(args):
        value = str(args["file"])
        return ToolAccess(
            frozenset({"file_read"}),
            (AccessTarget("file", value, "file_read", kind="file"),),
            subject=value,
        )

    def call(args, runtime):
        execution = runtime.execution
        assert execution is not None
        observed["capabilities"] = runtime.capabilities
        observed["execution"] = execution
        path = execution.resolve_path(args["file"])
        assert execution.read_text(path, encoding="utf-8") == "allowed"
        for candidate in ("sibling.txt", str(outside)):
            try:
                execution.resolve_path(candidate)
            except PermissionError:
                pass
            else:
                raise AssertionError(f"unauthorized path resolved: {candidate}")
        for operation in (
            lambda: execution.write_text(path, "changed", encoding="utf-8"),
            lambda: execution.start_shell("printf escaped"),
        ):
            try:
                operation()
            except PermissionError:
                pass
            else:
                raise AssertionError("read-only invocation acquired extra authority")
        return ToolResult.success()

    tool = Tool(
        "capture_execution",
        "",
        {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]},
        call,
        access_descriptor=describe,
        required_capabilities=frozenset({"execution"}),
    )
    result = ToolExecutor(
        {tool.name: tool},
        assemble_tool_capabilities(
            None, None, None, workspace_dir=tmp_path, cwd_provider=lambda: tmp_path,
        ),
    ).execute([ToolCall(tool.name, {"file": "allowed.txt"}, "c1")])[0].result

    assert result.ok
    assert observed["execution"].grant.operations == frozenset({"file_read"})
    assert not hasattr(observed["capabilities"], "execution")
    assert not hasattr(observed["capabilities"], "access_scope")
    assert not hasattr(observed["execution"], "backend")
    try:
        observed["execution"].cwd()
    except PermissionError:
        pass
    else:
        raise AssertionError("invocation wrapper remained usable after execution")


def test_internal_tool_without_execution_capability_gets_no_wrapper(tmp_path):
    observed = []
    tool = Tool(
        "internal_only",
        "",
        {"type": "object", "properties": {}},
        lambda _args, runtime: (
            observed.append(runtime.execution) or ToolResult.success()
        ),
        access_descriptor=lambda _args: ToolAccess.internal_read(),
    )

    result = ToolExecutor(
        {tool.name: tool},
        assemble_tool_capabilities(
            None, None, None, workspace_dir=tmp_path, cwd_provider=lambda: tmp_path,
        ),
    ).execute([ToolCall(tool.name, {}, "c1")])[0].result

    assert result.ok
    assert observed == [None]


def test_access_scope_is_immutable_and_classifies_roots(tmp_path):
    origin = tmp_path / "workspace"
    extra = tmp_path / "extra"
    other = tmp_path / "other"
    origin.mkdir()
    extra.mkdir()
    other.mkdir()

    scope = AccessScope(origin, [extra])
    expanded = scope.with_additional([extra / "nested", origin / "inside"])

    assert scope.additional == (extra.absolute(),)
    assert expanded.additional == (extra.absolute(),)
    assert scope.classify(origin / "a.txt") == PathClass.IN_ORIGIN
    assert scope.classify(extra / "nested" / "b.txt") == PathClass.IN_GRANTED
    assert scope.classify(other / "c.txt") == PathClass.OUTSIDE
    assert scope.contains(extra / "nested")
    assert not scope.contains(other)


def test_backend_resolves_paths_but_authorized_wrapper_enforces_scope(tmp_path):
    origin = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    origin.mkdir()
    backend = LocalExecutionBackend(origin, lambda: origin)
    resolved = backend.resolve_path(str(outside))
    assert resolved.value == str(outside.resolve())

    executor = ToolExecutor(
        {read_file_tool.name: read_file_tool},
        assemble_tool_capabilities(
            None, None, None, workspace_dir=origin, cwd_provider=lambda: origin,
        ),
    )
    result = executor.execute([
        ToolCall("read_file", {"file": str(outside)}, "c1")
    ])[0].result
    assert not result.ok
    assert "path_outside_scope" in result.data["permission"]["risk_flags"]


def test_forbidden_permission_settings_path_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIGHT_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    settings = forbidden_paths()[0]
    settings.write_text("{}", encoding="utf-8")
    origin = tmp_path / "workspace"
    origin.mkdir()
    executor = ToolExecutor(
        {read_file_tool.name: read_file_tool},
        assemble_tool_capabilities(
            None, None, None, workspace_dir=origin, cwd_provider=lambda: origin,
        ),
    )
    result = executor.execute([
        ToolCall("read_file", {"file": str(settings)}, "c1")
    ])[0].result
    assert not result.ok
    assert result.data["permission"]["source"] == "protected_path"


def test_recursive_search_defaults_to_directory_and_skips_protected_files(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "allowed.txt").write_text("needle allowed", encoding="utf-8")
    monkeypatch.setenv("WRIGHT_HOME", str(workspace))
    (workspace / "permission_settings.json").write_text(
        "needle protected", encoding="utf-8"
    )

    result = ToolExecutor(
        {grep_tool.name: grep_tool},
        assemble_tool_capabilities(
            None, None, None, workspace_dir=workspace, cwd_provider=lambda: workspace,
        ),
    ).execute([ToolCall("grep", {"pattern": "needle"}, "c1")])[0].result

    assert result.ok
    paths = [item["path"] for item in result.data["matches"]]
    assert paths == ["nested/allowed.txt"]


def test_display_path_is_relative_only_for_workspace(tmp_path):
    origin = tmp_path / "workspace"
    extra = tmp_path / "extra"
    origin.mkdir()
    extra.mkdir()
    backend = LocalExecutionBackend(origin, lambda: origin)
    assert backend.display_path(backend.resolve_path(str(origin / "src" / "a.py"))) == "src/a.py"
    assert backend.display_path(backend.resolve_path(str(extra / "a.py"))) == str((extra / "a.py").resolve())


def test_command_cwd_snaps_back_when_leaving_granted_roots(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session.create("snap", workspace)
    runtime = tool_runtime_for_session(session, workspace_dir=workspace)

    result = execute_command("cd ..", runtime=runtime)

    assert result.ok
    assert session.get_cwd() == workspace
    assert result.data["cwd"] == "."
    assert result.data["cwd_reset"] is True


def test_command_cwd_stays_inside_granted_extra_root(tmp_path):
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    session = Session.create("keep extra cwd", workspace)
    session.add_working_directory(extra)
    runtime = tool_runtime_for_session(session, workspace_dir=workspace)

    result = execute_command(f"cd {shlex.quote(str(extra))}", runtime=runtime)

    assert result.ok
    assert session.get_cwd() == extra.resolve()
    assert result.data["cwd"] == str(extra.resolve())
    assert "cwd_reset" not in result.data
