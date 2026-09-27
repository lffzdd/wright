"""组装静态记忆指令，以及每次请求使用的 core memory 投影。

只放【静态指令】(类型分类法、两步保存、何时存取、据记忆行动前先核实)。
MEMORY.md 索引内容和相关记忆全文【不】放这里——它们随会话变化,走 per-turn 注入
(见 MemoryService.prepare_memory_context)保证新鲜,与 Claude Code memdir 的做法一致。
Core memory 由 project_core_memory 在请求投影中替换，不写入历史消息。
"""

from __future__ import annotations

from pathlib import Path

from ...domain.model.memory import (
    FRONTMATTER_EXAMPLE,
    TRUSTING_RECALL,
    TYPES_SECTION,
    WHAT_NOT_TO_SAVE,
    WHEN_TO_ACCESS,
)
from ...infrastructure.persistence.memory import memory_dir


def project_core_memory(system_prompt: str, core_block: str) -> str:
    """Replace a legacy pinned snapshot in a request copy of the system prompt."""
    # Earlier sessions persisted the block as the system prompt's prefix.
    # Remove only that prefix, preserving role instructions and tool catalogs.
    if system_prompt.startswith("<CORE_MEMORY>\n"):
        _, closing, remainder = system_prompt.partition("\n</CORE_MEMORY>")
        if closing:
            system_prompt = remainder.lstrip("\n")
    if core_block:
        return f"{core_block}\n\n{system_prompt}"
    return system_prompt


def build_memory_instructions(directory: Path | None = None) -> str:
    """生成记忆系统的静态指令段,追加到 system prompt 末尾。"""
    directory = directory or memory_dir()

    how_to_save = f"""## 如何保存记忆

使用记忆工具，不要手写文件，也不要改 `MEMORY.md`。

- `type` 是内容类别（user / feedback / project / reference）。`scope` 是适用范围，二者互不替代。
- 默认写入当前项目。即使用户偏好或反馈，也不要自动写成所有项目通用。
- 只有用户明确要一条所有项目都适用的记忆时，才在 `create_memory` 里传 `scope=global`。
- 没有当前项目时，项目写入会失败。不要改成 global。
- 新主题使用 `create_memory`。同名标题不会覆盖已有记忆，身份是稳定 id。
- 修改已有记忆使用 `update_memory`，必须带 `get_memory` 返回的 id 和 `expected_revision`。改标题不改 id。
- 记忆过时但还要留档时，把 `status` 设为 `inactive`。这不是删除，正文修改也不会自动重新启用。
- 用户明确要求忘记时才 `delete_memory`。
- 写之前先 `search_memory`。默认范围是当前项目加全局。其他项目要显式改 scope。
- 文件格式如下，工具会维护它。不要把正文写进 `MEMORY.md`：

{FRONTMATTER_EXAMPLE}"""

    sections = [
        "# 长期记忆",
        "",
        f"你有一套持久化、基于文件的记忆系统,位于:`{directory}`。它会跨会话保留。",
        "",
        ("随着时间推移把它建设起来,让未来的对话能完整了解:用户是谁、他希望你如何协作、"
        "哪些行为该避免或重复、以及他交给你的工作背后的来龙去脉。"),
        "",
        "如果用户明确要你记住某事,立刻按最贴合的类型保存。要你忘记某事,就找到并用 `delete_memory` 删除对应条目。",
        "",
        TYPES_SECTION,
        WHAT_NOT_TO_SAVE,
        "",
        how_to_save,
        "",
        WHEN_TO_ACCESS,
        "",
        TRUSTING_RECALL,
        "",
        "## Core Memory",
        ("Core Memory 是每次模型请求重新读取的少量常驻背景，保存的是可修正的长期信息，"
         "不能压过用户当前这一次的明确指令。"),
        "- `persona` 是固定角色设定，不包含未经确认的用户职业、系统或项目身份，工具不能修改或清空。",
        "- `human_profile` 只放当前用户跨项目的信息和偏好，存在全局 Core Memory。项目约束不要写到这里。",
        "- `project_anchor` 只放当前项目的少量长期约束，按 project_id 分开保存。没有项目上下文时写入会失败。",
        "- `append` / `replace` 必须提供非空内容。清空要用 `mode=clear`。空字符串不是清空。",
        "",
        "## 记忆与其它持久化机制的边界",
        ("记忆用于【未来对话】仍有用的信息。只在【当前对话】范围内有用的东西不要存记忆——"
        "当前任务的步骤与进度,用任务/计划机制承载;记忆留给跨会话的长期知识。"),
        ("历史 episode 由系统在每个 user turn 终止时自动记录；不要手工创建或修改它。"
        "它只能作为过去的执行经验，使用前必须重新核实当前状态。"),
    ]
    return "\n".join(sections)
