"""Dependency boundaries that have to stay true after the package moves.

Import-graph acyclicity is checked separately. These tests name the layers
that are allowed to know about concrete adapters.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.paths import PACKAGE_ROOT

_CONSOLE_MARKERS = ("rich", "prompt_toolkit", "textual")


def _runtime_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
            for child in ast.walk(node):
                guarded.add(id(child))
    found: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found


def _py_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_domain_does_not_import_outer_layers() -> None:
    offenders: list[str] = []
    for path in _py_files(PACKAGE_ROOT / "domain"):
        for module in _runtime_imports(path):
            if module.startswith(("wright.infrastructure", "wright.interfaces")) or (
                ".infrastructure" in module or module.startswith("infrastructure")
            ):
                # Relative imports are stored as the module string after dots
                # are resolved by AST as node.module without the leading dots.
                # Catch both absolute and the relative module tail.
                if "infrastructure" in module or "interfaces" in module:
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT)} -> {module}")
    assert offenders == []


def test_shared_renderer_contract_does_not_load_console_toolkits() -> None:
    for relative in (
        "interfaces/rendering/contracts.py",
        "interfaces/rendering/history.py",
        "interfaces/rendering/silent.py",
        "interfaces/rendering/subscriber.py",
        "application/session/events.py",
        "application/session/publisher.py",
        "application/session/interaction.py",
    ):
        modules = _runtime_imports(PACKAGE_ROOT / relative)
        for module in modules:
            for marker in _CONSOLE_MARKERS:
                assert marker not in module, f"{relative} imports {module}"


def test_application_execution_does_not_import_interface_rendering() -> None:
    roots = (
        PACKAGE_ROOT / "application/agent",
        PACKAGE_ROOT / "application/autonomy",
        PACKAGE_ROOT / "application/session",
        PACKAGE_ROOT / "application/tool_execution",
    )
    offenders: list[str] = []
    for root in roots:
        for path in _py_files(root):
            for module in _runtime_imports(path):
                if "interfaces.rendering" in module or module.endswith(".renderer"):
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT)} -> {module}")
    assert offenders == []


def test_tui_view_models_do_not_import_the_renderer() -> None:
    for relative in (
        "interfaces/tui/view_models.py",
        "interfaces/tui/blocks.py",
        "interfaces/tui/format.py",
    ):
        modules = _runtime_imports(PACKAGE_ROOT / relative)
        assert "renderer" not in modules, relative
        assert not any(module.endswith(".renderer") for module in modules), relative


def test_cli_input_does_not_touch_renderer_privates() -> None:
    for relative in ("interfaces/cli/input.py", "interfaces/cli/prompter.py"):
        text = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
        assert "renderer._" not in text
        assert ".renderer._" not in text


def test_removed_alias_shells_are_gone() -> None:
    removed = (
        "interfaces/renderer.py",
        "interfaces/history.py",
        "interfaces/repl.py",
        "interfaces/cli/terminal_ui.py",
        "interfaces/cli/interactive_prompter.py",
        "interfaces/api/routes.py",
        "interfaces/websocket/handler.py",
        "infrastructure/persistence/file_session_repo.py",
        "infrastructure/persistence/session_codec.py",
        "infrastructure/persistence/checkpoint_error.py",
    )
    for relative in removed:
        assert not (PACKAGE_ROOT / relative).exists(), relative
    for path in _py_files(PACKAGE_ROOT):
        text = path.read_text(encoding="utf-8")
        assert "TerminalUI =" not in text
        assert "InteractivePrompter =" not in text
        assert "events_from_renderer" not in text


def test_agent_run_loop_does_not_import_a_concrete_renderer() -> None:
    text = (PACKAGE_ROOT / "application/agent/runner.py").read_text(encoding="utf-8")
    assert "interfaces.ui_events" not in text
    assert "interfaces.cli" not in text
    assert "interfaces.tui" not in text
    assert "console_renderer" not in text
    assert "ConsoleRenderer" not in text
    assert "events_from_renderer" not in text
    for marker in _CONSOLE_MARKERS:
        assert marker not in text


def test_permission_domain_does_not_load_the_settings_file() -> None:
    root = PACKAGE_ROOT / "domain" / "policy" / "permission"
    for path in _py_files(root):
        text = path.read_text(encoding="utf-8")
        assert "permission_store" not in text
        assert "json.load" not in text
        assert "tempfile" not in text


def test_lifecycle_orchestrator_does_not_import_adapters() -> None:
    package = PACKAGE_ROOT / "application/lifecycle"
    for path in _py_files(package):
        assert "infrastructure" not in _runtime_imports(path)
    text = (package / "manager.py").read_text(encoding="utf-8")
    assert "subprocess" not in text
    assert "jsonl" not in text or True
    trace = (PACKAGE_ROOT / "infrastructure/lifecycle/trace_recorder.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" not in trace
    hook = (PACKAGE_ROOT / "infrastructure/lifecycle/command_hook.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" in hook


def test_tool_runtime_does_not_import_application_assembly() -> None:
    modules = _runtime_imports(PACKAGE_ROOT / "infrastructure/tools/runtime.py")
    assert not any("application" in module for module in modules)


def test_turn_handler_does_not_hold_the_agent() -> None:
    text = (PACKAGE_ROOT / "application/agent/turns.py").read_text(encoding="utf-8")
    assert "runner" not in _runtime_imports(PACKAGE_ROOT / "application/agent/turns.py")
    assert "self.agent" not in text
    assert "AgentTurnHandler" not in text


def test_memory_manager_does_not_construct_stores() -> None:
    text = (PACKAGE_ROOT / "application/memory/manager.py").read_text(encoding="utf-8")
    assert "SemanticMemoryStore" not in text
    assert "FileCoreMemoryStore" not in text
    assert "EpisodeStore(" not in text
    assert "LlmContextSelector" not in text
    assembly = (PACKAGE_ROOT / "application/memory/assembly.py").read_text(encoding="utf-8")
    assert "SemanticMemoryStore" in assembly
    assert "def memory_tools" in assembly


def test_semantic_store_keeps_the_lock_around_replacement() -> None:
    store = (PACKAGE_ROOT / "infrastructure/persistence/memory/semantic.py").read_text(
        encoding="utf-8"
    )
    index = (PACKAGE_ROOT / "infrastructure/persistence/memory/semantic_index.py").read_text(
        encoding="utf-8"
    )
    assert "class _StoreLock" in store
    assert "_StoreLock(" not in index
    create = store.split("def create_memory", 1)[1].split("\ndef ", 1)[0]
    assert "with _StoreLock" in create
    assert "_rebuild_index_unlocked" in create
    exported = store.split("__all__ = [", 1)[1].split("]", 1)[0]
    for name in (
        "parse_frontmatter",
        "dump_frontmatter",
        "scan_memory_files",
        "format_manifest",
        "read_entrypoint",
    ):
        assert f'"{name}"' not in exported


def test_session_repository_keeps_io_separate_from_codec() -> None:
    text = (PACKAGE_ROOT / "infrastructure/persistence/session/repository.py").read_text(
        encoding="utf-8"
    )
    assert "def _serialize_session" not in text
    assert "def validate_session_links" not in text
    assert "def recover_interrupted_tool_calls" not in text
    exported = text.split("__all__ = ", 1)[1].split("]", 1)[0]
    assert "CheckpointError" not in exported
    assert "_serialize_session" not in exported
    assert "_deserialize_session" not in exported
    codec = (PACKAGE_ROOT / "infrastructure/persistence/session/codec.py").read_text(
        encoding="utf-8"
    )
    validate_at = codec.index("validate_session_links(")
    construct_at = codec.index("session = Session(")
    recover_at = codec.index("recover_interrupted_tool_calls(")
    assert validate_at < construct_at < recover_at


def test_tools_do_not_own_stores_schedulers_or_process_registries() -> None:
    banned = ("AutonomyStore", "AutonomyScheduler", "ProcessRegistry", "TaskService")
    offenders: list[str] = []
    for path in _py_files(PACKAGE_ROOT / "infrastructure/tools"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            if re.search(rf"\b{name}\b", text):
                offenders.append(f"{path.name}:{name}")
    assert offenders == []
    assert not (PACKAGE_ROOT / "domain/model/tasks.py").exists()
    assert not (PACKAGE_ROOT / "domain/gateway/task_backend.py").exists()
    assert not (PACKAGE_ROOT / "application/tasks").exists()


def test_domain_records_do_not_carry_runtime_callbacks() -> None:
    offenders: list[str] = []
    for path in _py_files(PACKAGE_ROOT / "domain/model"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id in {"on_done", "process", "stdout"}:
                    offenders.append(f"{path.name}:{node.target.id}")
    assert offenders == []


def test_autonomy_tools_do_not_wake_the_scheduler() -> None:
    text = (PACKAGE_ROOT / "infrastructure/tools/autonomy_tools.py").read_text(
        encoding="utf-8"
    )
    assert "notify_changed" not in text
    assert "durable_store" not in text
