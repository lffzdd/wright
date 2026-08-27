"""记忆目录的路径解析与创建。

记忆是【跨会话持久化】的长期记忆,存放位置和 workspace 解耦:
    - 默认落在 `~/.wright/memory`(可用 WRIGHT_HOME 改根目录);
    - 可用环境变量 `WRIGHT_MEMORY_DIR` 覆盖到任意绝对/相对路径。

记忆目录【不】受 file_tools 的 workspace 沙箱约束——它本就该在 workspace 之外,
所以记忆工具不走 ToolRuntime.workspace_dir,而是统一从这里解析。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..paths import wright_home

# MEMORY.md:始终注入上下文的索引文件名(对标 Claude Code memdir 的 ENTRYPOINT)。
MEMORY_INDEX = "MEMORY.md"


def memory_dir() -> Path:
    """解析记忆目录的绝对路径。

    优先级:环境变量 WRIGHT_MEMORY_DIR > ~/.wright/memory。
    环境变量给的相对路径按【当前工作目录】解析后再 resolve。
    """
    override = os.getenv("WRIGHT_MEMORY_DIR")
    base = Path(override) if override else wright_home() / "memory"
    return base.expanduser().resolve()


def entrypoint_path() -> Path:
    """MEMORY.md 索引文件的绝对路径。"""
    return memory_dir() / MEMORY_INDEX


def ensure_memory_dir() -> Path:
    """幂等创建记忆目录(含父链),返回其绝对路径。

    在装配层调用一次即可,之后写记忆无需再检查目录是否存在
    ——对标 memdir 的 ensureMemoryDirExists。
    """
    d = memory_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d
