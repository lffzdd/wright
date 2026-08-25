"""长期记忆系统(对标 Claude Code memdir)。

对外主要暴露 MemoryManager(Agent 的协作者)与 paths 辅助。记忆工具
(save_memory / search_memory)在 tools/memory_tools.py 里定义。
"""

from .manager import MemoryManager
from .episode import EpisodeRecord, EpisodeStore
from .paths import ensure_memory_dir, memory_dir
from .types import MEMORY_TYPES, MemoryType

__all__ = [
    "MemoryManager",
    "EpisodeRecord",
    "EpisodeStore",
    "ensure_memory_dir",
    "memory_dir",
    "MEMORY_TYPES",
    "MemoryType",
]
