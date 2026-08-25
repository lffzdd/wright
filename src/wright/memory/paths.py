"""记忆目录的路径解析与创建。

记忆是【跨会话持久化】的长期记忆,存放位置和 workspace 解耦:
    - 默认落在包内 `memory_store/`(随仓库走,与 workspace 隔离);
    - 可用环境变量 `WRIGHT_MEMORY_DIR` 覆盖到任意绝对/相对路径。

记忆目录【不】受 file_tools 的 workspace 沙箱约束——它本就该在 workspace 之外,
所以记忆工具不走 ToolRuntime.workspace_dir,而是统一从这里解析。
"""

from __future__ import annotations

import os
from pathlib import Path

# MEMORY.md:始终注入上下文的索引文件名(对标 Claude Code memdir 的 ENTRYPOINT)。
MEMORY_INDEX = "MEMORY.md"

# 默认记忆目录:本包同级的 memory_store/。用包文件定位而非 cwd,
# 保证无论从哪里启动进程,主 Agent 都读写同一份记忆。
_DEFAULT_MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory_store"


def memory_dir() -> Path:
    """解析记忆目录的绝对路径。

    优先级:环境变量 WRIGHT_MEMORY_DIR > 包内 memory_store/。
    环境变量给的相对路径按【当前工作目录】解析后再 resolve。
    """
    override = os.getenv("WRIGHT_MEMORY_DIR")
    base = Path(override) if override else _DEFAULT_MEMORY_DIR
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
