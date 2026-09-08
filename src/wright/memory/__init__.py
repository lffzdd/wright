"""长期记忆系统(对标 Claude Code memdir)。

对外主要暴露 MemoryManager(Agent 的协作者)与 paths 辅助。记忆工具
(save_memory / search_memory)在 tools/memory_tools.py 里定义。
"""

from .episode import EpisodeRecord, EpisodeStore
from .manager import MemoryManager
from .paths import ensure_memory_dir, memory_dir
from .types import MEMORY_TYPES, MemoryType

__all__ = [
    "MEMORY_TYPES",
    "EpisodeRecord",
    "EpisodeStore",
    "MemoryManager",
    "MemoryType",
    "ensure_memory_dir",
    "memory_dir",
]
