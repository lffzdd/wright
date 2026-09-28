"""Guard the runtime import graph against cycles.

src/wright 有若干模块在类型层面互相引用（TYPE_CHECKING 回边），但运行时导入图
必须保持无环。这个测试用 AST 静态分析只统计真正会在运行时执行的 import。
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.paths import PACKAGE_ROOT


def _module_name(path: Path) -> str:
    rel = path.relative_to(PACKAGE_ROOT)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


def _collect_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        modules[_module_name(path)] = path
    return modules


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    """id() of every node nested under an `if TYPE_CHECKING:` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
            for child in ast.walk(node):
                guarded.add(id(child))
    return guarded


def _runtime_edges(module: str, path: Path, known: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded = _type_checking_nodes(tree)
    is_package = path.name == "__init__.py"
    base = module if is_package else module.rpartition(".")[0]
    edges: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        targets: list[str] = []
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = base.split(".") if base else []
                for _ in range(node.level - 1):
                    if parts:
                        parts.pop()
                if node.module:
                    parts = [*parts, node.module]
                targets.append(".".join(parts))
            elif (node.module or "").startswith("wright"):
                targets.append(node.module[len("wright") :].lstrip("."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("wright"):
                    targets.append(alias.name[len("wright") :].lstrip("."))
        for target in targets:
            if target and target in known and target != module:
                edges.add(target)
    return edges


def _find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for neighbour in sorted(graph.get(node, ())):
            if color.get(neighbour, BLACK) == GREY:
                return [*stack[stack.index(neighbour) :], neighbour]
            if color.get(neighbour, BLACK) == WHITE:
                found = visit(neighbour)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in sorted(graph):
        if color[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None


def test_runtime_import_graph_is_acyclic() -> None:
    modules = _collect_modules()
    known = set(modules)
    graph = {
        name: _runtime_edges(name, path, known) for name, path in modules.items()
    }
    cycle = _find_cycle(graph)
    assert cycle is None, (
        "运行时导入出现循环依赖："
        + " -> ".join(cycle or [])
        + "\n如果这条边只是为了类型标注，请把它移到 `if TYPE_CHECKING:` 块里。"
    )


def test_tool_protocol_does_not_depend_on_runtime_assembly() -> None:
    modules = _collect_modules()
    known = set(modules)
    graph = {
        name: _runtime_edges(name, path, known) for name, path in modules.items()
    }

    base_mod = "infrastructure.tools.base" if "infrastructure.tools.base" in graph else "tools.base"
    runtime_mod = "infrastructure.tools.runtime" if "infrastructure.tools.runtime" in graph else "tools.runtime"
    proto_mod = "domain.model.tool" if "domain.model.tool" in graph else "domain.tool_protocol"

    assert not graph[base_mod] & {
        "application.tool_execution.capabilities",
        "tool_capabilities",
        "infrastructure.runtime",
        "execution",
        "core.processes",
        "processes",
    }
    assert not any(t.endswith("tool_execution.capabilities") for t in graph[runtime_mod])
    assert not any(
        "tools" in target for target in graph[proto_mod]
    )


def test_loop_tool_depends_on_a_port_not_the_engine() -> None:
    modules = _collect_modules()
    known = set(modules)
    graph = {
        name: _runtime_edges(name, path, known) for name, path in modules.items()
    }

    loop_mod = "infrastructure.tools.loop_tools" if "infrastructure.tools.loop_tools" in graph else "tools.loop_tools"
    ports_mod = "infrastructure.tools.ports" if "infrastructure.tools.ports" in graph else "tools.ports"

    assert ports_mod in graph[loop_mod]
    assert not any(
        target == "engine" or target.startswith(("engine.", "application"))
        for target in graph[loop_mod]
    )


def test_permission_resolver_does_not_depend_on_tools() -> None:
    modules = _collect_modules()
    known = set(modules)
    graph = {
        name: _runtime_edges(name, path, known) for name, path in modules.items()
    }

    resolver_mod = "domain.policy.permission.resolver"
    approval_mod = "domain.policy.permission.approval"

    assert not any("tools" in target for target in graph[resolver_mod])
    assert not any("tools" in target for target in graph[approval_mod])
    assert resolver_mod not in graph[approval_mod]

