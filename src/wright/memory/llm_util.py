"""复用主 LLMClient 做一次性 side-query 的小工具。

selector(召回选择)和 extractor(记忆提取)都需要「给一组消息、拿回一段文本」,
而 LLMClient 对外吐的是事件流。这里把「drain 事件流取最终 content」收口成一个函数,
两处复用,不必新建 client。
"""

from __future__ import annotations

from openai.types.chat import ChatCompletionMessageParam

from ..events import ContentDone
from ..llm import LLMClient


def side_query(llm: LLMClient, system: str, user: str) -> str:
    """发一轮 system+user 的 side-query,返回最终文本内容。

    LLMClient 默认 response_format=json_object,故 selector/extractor 拿到的就是
    合法 JSON 字符串。调用方负责 json.loads 与异常兜底。
    """
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    content = ""
    for event in llm(messages):
        if isinstance(event, ContentDone):
            content = event.content
    return content
