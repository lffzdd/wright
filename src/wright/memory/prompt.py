"""组装注入 system prompt 的静态记忆指令段。

只放【静态指令】(类型分类法、两步保存、何时存取、据记忆行动前先核实)。
MEMORY.md 索引内容和相关记忆全文【不】放这里——它们随会话变化,走 per-turn 注入
(见 recall.build_recall_block)保证新鲜,与 Claude Code memdir 的做法一致。
"""

from __future__ import annotations

from pathlib import Path

from .paths import memory_dir
from .types import (
    FRONTMATTER_EXAMPLE,
    TRUSTING_RECALL,
    TYPES_SECTION,
    WHAT_NOT_TO_SAVE,
    WHEN_TO_ACCESS,
)


def build_memory_instructions(directory: Path | None = None) -> str:
    """生成记忆系统的静态指令段,追加到 system prompt 末尾。"""
    directory = directory or memory_dir()

    how_to_save = f"""## 如何保存记忆

保存一条记忆分两步:

第 1 步——把记忆写进它自己的文件(如 `user-role.md`),用如下 frontmatter 格式:

{FRONTMATTER_EXAMPLE}

第 2 步——在 `MEMORY.md` 里加一行指向该文件的指针。`MEMORY.md` 是索引不是记忆,
每条一行、不带 frontmatter。绝不要把记忆正文直接写进 `MEMORY.md`。

(用 `create_memory` / `update_memory` 工具保存时,以上两步会自动完成：
记忆文件和索引会作为一次存储操作一起维护。)

- 按主题(语义)组织记忆,而非按时间顺序。
- 新主题使用 `create_memory`；已有主题使用 `update_memory`，不要静默覆盖。
- 发现某条记忆过时或错了,就更新；用户明确要求忘记时才删除。
- 不要写重复记忆:写新记忆前先用 `search_memory` 查找，并用 `get_memory` 阅读候选正文。"""

    sections = [
        "# 长期记忆",
        "",
        f"你有一套持久化、基于文件的记忆系统,位于:`{directory}`。它会跨会话保留。",
        "",
        "随着时间推移把它建设起来,让未来的对话能完整了解:用户是谁、他希望你如何协作、"
        "哪些行为该避免或重复、以及他交给你的工作背后的来龙去脉。",
        "",
        "如果用户明确要你记住某事,立刻按最贴合的类型保存。要你忘记某事,就找到并删除对应条目。",
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
        "## 记忆与其它持久化机制的边界",
        "记忆用于【未来对话】仍有用的信息。只在【当前对话】范围内有用的东西不要存记忆——"
        "当前任务的步骤与进度,用任务/计划机制承载;记忆留给跨会话的长期知识。",
        "历史 episode 由系统在每个 user turn 终止时自动记录；不要手工创建或修改它。"
        "它只能作为过去的执行经验，使用前必须重新核实当前状态。",
    ]
    return "\n".join(sections)
