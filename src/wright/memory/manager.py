"""MemoryManager:记忆系统对 Agent 暴露的唯一协作者。

与 ContextCompactor / ToolExecutor 一致的接法——Agent 只持有它、在主循环里
「喊一声」,记忆的所有具体逻辑(召回/提取/落盘)都收在 memory 包内部。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..llm import LLMClient
from ..logger import get_logger
from .episode import EpisodeRecord, EpisodeStore, episode_from_session
from .extract import extract_and_save
from .paths import memory_dir
from .prompt import build_memory_instructions
from .recall import build_recall_block

logger = get_logger(__name__)


class MemoryManager:
    """主 Agent 的长期记忆协作者。

    Args:
        llm: 主对话用的 LLMClient。
        selector_llm: 可选,做召回/提取 side-query 的更便宜模型;默认复用 llm。
        directory: 可选,记忆目录;默认 paths.memory_dir()。
    """

    def __init__(
        self,
        llm: LLMClient,
        selector_llm: LLMClient | None = None,
        directory: Path | None = None,
    ) -> None:
        self.llm = llm
        self.selector_llm = selector_llm or llm
        self.directory = (directory or memory_dir()).expanduser().resolve()
        # 必须创建实例实际绑定的目录，而不是 paths.memory_dir() 的默认目录。
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.episode_store = EpisodeStore(self.directory)

    def instructions(self) -> str:
        """注入 system prompt 的静态记忆指令段。"""
        return build_memory_instructions(self.directory)

    def recall_block(self, query: str) -> str:
        """针对本轮 query 的召回文本块(MEMORY.md 索引 + 相关记忆),无则 ""。"""
        try:
            return build_recall_block(query, self.selector_llm, self.directory)
        except Exception as exc:  # recall is a sidecar, never a task dependency
            logger.debug("记忆召回失败: %s", exc)
            return ""

    def extract(self, session_state: Any) -> int:
        """会话收口后从 transcript 提取并落盘记忆,返回写入条数(best-effort)。"""
        return extract_and_save(session_state, self.selector_llm, self.directory)

    def record_episode(
        self, session_state: Any, final_answer: str | None
    ) -> EpisodeRecord | None:
        """Deterministically persist one execution episode; best-effort."""
        try:
            episode = episode_from_session(session_state, final_answer)
            return self.episode_store.save(episode)
        except Exception as exc:  # memory must never break task delivery
            logger.debug("记录 episodic memory 失败: %s", exc)
            return None

    def finalize_turn(
        self,
        session_state: Any,
        final_answer: str | None,
        *,
        extract_semantic: bool,
    ) -> dict[str, Any]:
        """Persist the episode, then optionally extract durable semantics."""
        episode = self.record_episode(session_state, final_answer)
        extracted = self.extract(session_state) if extract_semantic else 0
        return {
            "episode_id": episode.id if episode is not None else None,
            "semantic_memories_written": extracted,
        }

    def tools(self):
        """Build CRUD tools bound to this manager's exact directory."""
        from ..tools.episode_tools import build_episode_tools
        from ..tools.memory_tools import build_memory_tools

        return [
            *build_memory_tools(self.directory, include_legacy_save=False),
            *build_episode_tools(self.directory),
        ]
