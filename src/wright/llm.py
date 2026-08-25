"""
LLM 传输层：屏蔽"流式 / 非流式"的差异，对外统一吐出事件流。

核心思想：
    "内容是逐 token 到达，还是一次性到达"是底层传输细节，
    不应该泄漏给主循环和展示层。LLMClient 把这个差异在这里抹平：

    - 流式：边收 chunk 边 yield ReasoningDelta/ContentDelta（保证实时），
            chunk 循环结束后再 yield 一个 ContentDone（携带完整内容）。
    - 非流式：内部一次性拿到完整响应，直接 yield 一个 ContentDone。

    实时打印靠 Delta，完整内容靠 Done —— 上层永远只消费事件，
    不需要知道也不关心底层走的是哪条路径。
"""

import time
import random

from typing import Any, Iterator, Callable

from openai import OpenAI, APIConnectionError, APIStatusError, omit
from openai.types.chat import ChatCompletionMessageParam

from .events import LLMEvent, ReasoningDelta, ContentDelta, ContentDone, UsageEvent


class LLMClient:
    def __init__(
        self,
        base_url: str | None,
        api_key: str | None,
        model: str | None,
        context_limit: int | None = None,
        stream: bool = True,
        max_attempts: int = 3,
        base_wait: float = 1.0,
        max_wait: float = 60.0,
        response_format: dict | None = None,
    ):

        if base_url is None or api_key is None or model is None:
            raise ValueError(
                "base_url / api_key / model 不能为空，请指定或在设置环境变量"
            )

        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.context_limit = context_limit
        self.stream = stream
        self.max_attempts = max_attempts
        self.base_wait = base_wait
        self.max_wait = max_wait
        # 协议契约的"合法 JSON"那一层下沉到服务端:json_object 模式让服务端在
        # 解码阶段保证返回内容必能 json.loads(不再靠 prompt 自觉、也不靠本地抠大括号)。
        # 注意这只保证"是合法 JSON",不保证"形状对"——形状校验仍归本地 protocol 层。
        # 端点不支持时(如不认 json_schema 的服务)传 None 即退回纯文本。
        self.response_format: Any = (
            {"type": "json_object"} if response_format is None else response_format
        )

    def __call__(
        self,
        messages: list[ChatCompletionMessageParam],
    ) -> Iterator[LLMEvent]:
        """调用一次 LLM，以事件流的形式产出结果。

        Args:
            messages: 完整对话上下文

        Yields:
            LLMEvent: ReasoningDelta / ContentDelta /   ContentDone / UsageEvent
        """
        if self.stream:
            yield from self._call_stream(messages)
        else:
            yield from self._call_once(messages)

    def _call_stream(
        self, messages: list[ChatCompletionMessageParam]
    ) -> Iterator[LLMEvent]:
        """流式路径：逐 chunk 实时 yield Delta，最后汇总成 ContentDone。

        重试只包住 create()（请求建立）：流式下 429/5xx/连接错误都在这一步
        暴露，且此时尚未 yield 任何事件，重试对外完全不可见（首事件定界）。
        中途断流不重试——partial Delta 已经交给渲染层，重试会把半截回答
        打两遍且两遍内容不同；让本轮诚实失败，好过静默重复渲染。
        """

        resp = call_with_retry(
            lambda: self.client.chat.completions.create(
                messages=messages,
                model=self.model,
                stream=True,
                stream_options={"include_usage": True},
                # response_format 为空 dict 时按"不约束"处理:传 omit 哨兵
                # 等同于不带这个参数(退回纯文本路径)。
                response_format=self.response_format or omit,
            ),
            max_attempts=self.max_attempts,
            base_wait=self.base_wait,
            max_wait=self.max_wait,
        )

        content: list[str] = []
        reasoning: list[str] = []
        for chunk in resp:  # 走到这里说明连接已建立;中途断流让它往上抛
            usage = getattr(chunk, "usage", None)
            if chunk.choices:
                delta = chunk.choices[0].delta
                reasoning_piece = getattr(delta, "reasoning_content", None)
                content_piece = delta.content or ""
                if reasoning_piece:
                    reasoning.append(reasoning_piece)
                    yield ReasoningDelta(reasoning_piece)
                if content_piece:
                    content.append(content_piece)
                    yield ContentDelta(content_piece)
            if usage:
                yield UsageEvent(usage)
        yield ContentDone(content="".join(content), reasoning="".join(reasoning))

    def _call_once(
        self,
        messages: list[ChatCompletionMessageParam],
    ) -> Iterator[LLMEvent]:
        """非流式路径：一次性拿到完整响应，直接产出一个 ContentDone。"""
        resp = call_with_retry(
            lambda: self.client.chat.completions.create(
                messages=messages,
                model=self.model,
                stream=False,
                response_format=self.response_format or omit,
            ),
            max_attempts=self.max_attempts,
            base_wait=self.base_wait,
            max_wait=self.max_wait,
        )

        message = resp.choices[0].message
        content = message.content or ""
        reasoning = getattr(message, "reasoning_content", None) or ""

        if getattr(resp, "usage", None):
            yield UsageEvent(resp.usage)

        yield ContentDone(content=content, reasoning=reasoning)


def is_retryable(exception: Exception) -> bool:
    """判断这个异常是否值得重试。

    两个子类已被隐式覆盖,不是遗漏:APITimeoutError 是 APIConnectionError
    的子类,RateLimitError 是 APIStatusError(429) 的子类。
    """
    if isinstance(exception, APIConnectionError):
        return True  # 纯网络抖动
    if isinstance(exception, APIStatusError):
        # 429 限流、5xx 服务端错误 → 可重试
        # 4xx 客户端错误（除429）→ 不可重试
        return exception.status_code == 429 or exception.status_code >= 500
    return False


def call_with_retry(fn: Callable, max_attempts=3, base_wait=1.0, max_wait=60.0):
    """用指数退避重试 fn()，返回它的返回值。"""
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_retryable(exc):
                raise  # 不可重试：立即往上抛
            if attempt == max_attempts - 1:
                raise  # 已用完所有次数：放弃
            wait = min(base_wait * (2**attempt), max_wait)
            wait += random.uniform(0, 1)  # jitter
            time.sleep(wait)
