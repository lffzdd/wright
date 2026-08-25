"""
ReAct 事件类型定义（传输层与展示层之间的"中间结构"）

设计动机：
    LLM 的内容可能"逐 token 到达"（流式），也可能"一次性到达"（非流式）。
    我们不希望这种差异泄漏到主循环和展示层。

    解法：让传输层 call_llm 统一吐出下面这组事件，展示层只管消费事件。
    - 流式：边收 chunk 边 yield XxxDelta，最后 yield 一个 XxxDone
    - 非流式：不发 Delta，直接 yield 一个 Done（携带完整内容）

    这样实时打印靠 Delta，完整内容靠 Done，两边职责清清楚楚。
"""

from dataclasses import dataclass
from typing import Any

@dataclass
class ReasoningDelta:
    """一段实时到达的推理内容（逐 token）。仅流式场景产生。"""

    piece: str


@dataclass
class ContentDelta:
    """一段实时到达的正式回复内容（逐 token）。仅流式场景产生。"""

    piece: str


@dataclass
class ContentDone:
    """本轮回复结束，携带拼接好的完整内容，供主循环解析 JSON。

    无论流式还是非流式，每一轮 LLM 调用都必定以一个 ContentDone 收尾。
    """

    content: str
    reasoning: str = ""


@dataclass
class UsageEvent:
    """本轮的 token 用量信息（流式时通常在最后一个 chunk 里）。"""

    usage: Any


# 所有事件的联合类型，方便类型标注与 isinstance 判断
LLMEvent = ReasoningDelta | ContentDelta | ContentDone | UsageEvent