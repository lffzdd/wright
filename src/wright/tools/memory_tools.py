"""Model-facing semantic memory CRUD tools.

Tools are built by ``build_memory_tools(directory)`` so the Agent and its tools
always operate on the same store.  Module-level tools remain for backwards
compatibility and resolve ``WRIGHT_MEMORY_DIR`` at call time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..memory.store import (
    MemoryStoreError,
    create_memory as store_create_memory,
    delete_memory as store_delete_memory,
    get_memory as store_get_memory,
    rebuild_index,
    search_memories,
    update_memory as store_update_memory,
    write_memory_file,
)
from ..memory.types import MEMORY_TYPES
from ..permission import PermissionCheckResult
from .base import Tool, ToolResult, ToolRuntime


def save_memory(
    name: str,
    description: str,
    type: str,
    content: str,
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    """Backward-compatible upsert; explicit CRUD should be preferred."""
    try:
        path = write_memory_file(name, description, type, content, directory)
        rebuild_index(directory)
        return ToolResult.success(
            {"message": "记忆已保存", "id": path.stem, "file": path.name, "type": type}
        )
    except (MemoryStoreError, OSError) as exc:
        return ToolResult.fail(str(exc))


def create_memory(
    name: str,
    description: str,
    type: str,
    content: str,
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    try:
        return ToolResult.success(
            store_create_memory(name, description, type, content, directory).to_dict()
        )
    except (MemoryStoreError, OSError) as exc:
        return ToolResult.fail(str(exc))


def get_memory(
    memory_id: str,
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    try:
        return ToolResult.success(store_get_memory(memory_id, directory).to_dict())
    except (MemoryStoreError, OSError) as exc:
        return ToolResult.fail(str(exc))


def update_memory(
    memory_id: str,
    name: str | None = None,
    description: str | None = None,
    type: str | None = None,
    content: str | None = None,
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    if all(value is None for value in (name, description, type, content)):
        return ToolResult.fail("至少提供一个要更新的字段")
    try:
        record = store_update_memory(
            memory_id,
            name=name,
            description=description,
            type_=type,
            content=content,
            directory=directory,
        )
        return ToolResult.success(record.to_dict())
    except (MemoryStoreError, OSError) as exc:
        return ToolResult.fail(str(exc))


def delete_memory(
    memory_id: str,
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    try:
        deleted = store_delete_memory(memory_id, directory)
        return ToolResult.success(
            {"message": "记忆已删除", "id": deleted.id, "name": deleted.name}
        )
    except (MemoryStoreError, OSError) as exc:
        return ToolResult.fail(str(exc))


def search_memory(
    query: str = "",
    type: str | None = None,
    limit: int = 20,
    runtime: ToolRuntime | None = None,
    *,
    directory: Path | None = None,
) -> ToolResult:
    try:
        records = search_memories(
            query, type_=type, limit=limit, directory=directory
        )
        # Keep the original tool's discovery behavior: an unmatched query still
        # returns the newest memories instead of pretending the store is empty.
        # The store-level search remains strict for programmatic callers.
        if query.strip() and not records:
            records = search_memories(
                "", type_=type, limit=limit, directory=directory
            )
        results = [
            {
                "id": record.id,
                "name": record.name,
                "description": record.description,
                "type": record.type,
                "updated_at": record.updated_at,
            }
            for record in records
        ]
        return ToolResult.success({
            "count": len(records),
            # `memories` 保留旧的可读清单形状，`results` 提供完整结构化结果。
            "memories": "\n".join(
                f"- [{record.type}] {record.id}.md: {record.description}"
                for record in records
            ) or "(暂无记忆)",
            "results": results,
        })
    except (MemoryStoreError, OSError) as exc:
        return ToolResult.fail(str(exc))


def _delete_permission(
    arguments: dict[str, Any], runtime: ToolRuntime
) -> PermissionCheckResult:
    return PermissionCheckResult(
        "ask",
        f"删除长期记忆 {arguments.get('memory_id', '')}，该操作会影响未来会话",
        ("deletes_data",),
        source="memory_tool",
    )


_MEMORY_FIELDS = {
    "name": {"type": "string", "minLength": 1, "maxLength": 120},
    "description": {"type": "string", "maxLength": 500},
    "type": {"type": "string", "enum": list(MEMORY_TYPES)},
    "content": {"type": "string", "minLength": 1, "maxLength": 12_000},
}


def build_memory_tools(
    directory: Path | None = None,
    *,
    include_legacy_save: bool = True,
) -> list[Tool]:
    def bind(function):
        return lambda args, runtime: function(
            **args, runtime=runtime, directory=directory
        )

    create_tool = Tool(
        name="create_memory",
        description=(
            "创建一条新的跨会话语义记忆。若同 id 已存在会失败；先 search_memory，"
            "已有内容应使用 update_memory，避免静默覆盖。"
        ),
        parameters={
            "type": "object",
            "properties": dict(_MEMORY_FIELDS),
            "required": ["name", "description", "type", "content"],
            "additionalProperties": False,
        },
        call=bind(create_memory),
    )
    get_tool = Tool(
        name="get_memory",
        description="按 memory_id 读取一条长期记忆的完整正文和元数据。",
        parameters={
            "type": "object",
            "properties": {"memory_id": {"type": "string", "minLength": 1}},
            "required": ["memory_id"],
            "additionalProperties": False,
        },
        call=bind(get_memory),
        is_concurrency_safe=lambda args: True,
    )
    update_tool = Tool(
        name="update_memory",
        description=(
            "更新已有长期记忆。只传需要改变的字段；memory_id 保持稳定，即使修改 name。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "minLength": 1},
                **_MEMORY_FIELDS,
            },
            "required": ["memory_id"],
            "anyOf": [
                {"required": ["name"]},
                {"required": ["description"]},
                {"required": ["type"]},
                {"required": ["content"]},
            ],
            "additionalProperties": False,
        },
        call=bind(update_memory),
    )
    delete_tool = Tool(
        name="delete_memory",
        description="删除一条已过期、错误或用户明确要求忘记的长期记忆。",
        parameters={
            "type": "object",
            "properties": {"memory_id": {"type": "string", "minLength": 1}},
            "required": ["memory_id"],
            "additionalProperties": False,
        },
        call=bind(delete_memory),
        check_permission=_delete_permission,
    )
    search_tool = Tool(
        name="search_memory",
        description=(
            "按关键词和可选类型搜索长期记忆，返回 id、类型和描述；需要正文再调用 get_memory。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "type": {"type": "string", "enum": list(MEMORY_TYPES)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
            "additionalProperties": False,
        },
        call=bind(search_memory),
        is_concurrency_safe=lambda args: True,
    )
    tools = [create_tool, get_tool, update_tool, delete_tool, search_tool]
    if include_legacy_save:
        tools.insert(0, Tool(
            name="save_memory",
            description=(
                "兼容性 upsert：保存长期记忆，同名会覆盖。新工作优先使用 "
                "create_memory/update_memory 的显式语义。"
            ),
            parameters={
                "type": "object",
                "properties": dict(_MEMORY_FIELDS),
                "required": ["name", "description", "type", "content"],
                "additionalProperties": False,
            },
            call=bind(save_memory),
        ))
    return tools


(
    save_memory_tool,
    create_memory_tool,
    get_memory_tool,
    update_memory_tool,
    delete_memory_tool,
    search_memory_tool,
) = build_memory_tools()

memory_tools = [
    save_memory_tool,
    create_memory_tool,
    get_memory_tool,
    update_memory_tool,
    delete_memory_tool,
    search_memory_tool,
]
