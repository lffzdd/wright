"""会话结束时自动从 transcript 提取值得长期保留的记忆并落盘。

对标 Claude Code 的 extractMemories:在一个 user turn 收口(模型给出 final_answer)后
触发一次。全程 best-effort——任何异常都吞掉,绝不影响 Agent.run() 的返回值。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..llm import LLMClient
from ..logger import get_logger
from .llm_util import side_query
from .store import (
    MemoryStoreError,
    create_memory,
    format_manifest,
    scan_memory_files,
    slugify,
    update_memory,
)
from .types import MEMORY_TYPES, TYPES_SECTION, WHAT_NOT_TO_SAVE

logger = get_logger(__name__)

# 喂给提取器的 transcript 体量上限(字符)。取尾部即可——最近的交互最值得提取。
MAX_TRANSCRIPT_CHARS = 12_000

EXTRACT_SYSTEM_PROMPT = f"""你在一段 AI Agent 与用户的对话结束后,从中提取值得【长期、跨会话】保留的记忆。

{TYPES_SECTION}

{WHAT_NOT_TO_SAVE}

请只提取明显值得保留的内容,挑剔、克制。对每条已存在的相似记忆,优先 update 而非新建。
若这段对话没有任何值得长期保留的东西,返回空列表。

update 时必须把已有清单中的文件 id 放进 memory_id；create 时不要填写 memory_id。
只输出严格 JSON,格式:
{{"memories": [
  {{"memory_id": "已有-id（仅 update）", "name": "简短主题名", "description": "一句话描述", "type": "{' | '.join(MEMORY_TYPES)}",
    "content": "记忆正文(feedback/project 请含 Why 和 How to apply)", "action": "create | update | skip"}}
]}}"""


def _build_transcript(session_state: Any) -> str:
    """只提取当前 user turn，避免多轮 REPL 反复学习旧内容。"""
    parts: list[str] = []
    records = getattr(session_state, "message_records", [])
    start = int(getattr(session_state, "active_turn_start_message_index", 0))
    for record in records[start:]:
        msg = record.message
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if content.lstrip().startswith("<system-reminder>"):
            continue
        parts.append(f"[{role}] {content}")
    text = "\n".join(parts)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = "…(前文略)\n" + text[-MAX_TRANSCRIPT_CHARS:]
    return text


def extract_and_save(
    session_state: Any,
    llm: LLMClient,
    directory: Path | None = None,
) -> int:
    """提取并保存记忆,返回写入条数。任何失败返回 0(已记录调试日志)。"""
    try:
        transcript = _build_transcript(session_state)
        if not transcript.strip():
            return 0

        manifest = format_manifest(scan_memory_files(directory)) or "(暂无)"
        user_msg = (
            f"已有记忆清单:\n{manifest}\n\n"
            f"本次对话 transcript:\n{transcript}"
        )
        raw = side_query(llm, EXTRACT_SYSTEM_PROMPT, user_msg)
        memories = json.loads(raw).get("memories", [])
    except Exception as e:  # noqa: BLE001 — 旁路,任何异常都不该冒泡
        logger.debug("记忆提取失败: %s", e)
        return 0

    if not isinstance(memories, list):
        return 0

    written = 0
    for item in memories:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if action == "skip":
            continue
        name = item.get("name")
        type_ = item.get("type")
        content = item.get("content")
        if not (name and content and type_ in MEMORY_TYPES):
            continue
        try:
            if action == "create":
                create_memory(
                    name=str(name),
                    description=str(item.get("description") or ""),
                    type_=str(type_),
                    content=str(content),
                    directory=directory,
                )
            elif action == "update":
                memory_id = item.get("memory_id") or slugify(str(name))
                update_memory(
                    str(memory_id),
                    name=str(name),
                    description=str(item.get("description") or ""),
                    type_=str(type_),
                    content=str(content),
                    directory=directory,
                )
            else:
                continue
            written += 1
        except (OSError, MemoryStoreError) as e:
            logger.debug("写记忆文件失败 (%s): %s", name, e)
    return written
