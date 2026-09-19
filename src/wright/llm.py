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

import base64
import random
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlparse

from openai import APIConnectionError, APIStatusError, OpenAI, omit

from .attachments import AttachmentError, AttachmentStore
from .events import ContentDelta, ContentDone, LLMEvent, ReasoningDelta, UsageEvent
from .model import ModelRequest
from .model_adapters import ChatAdapter, ResponsesAdapter


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
        transport: str = "auto",
        attachment_store: AttachmentStore | None = None,
    ):

        if base_url is None or api_key is None or model is None:
            raise ValueError(
                "base_url / api_key / model 不能为空，请指定或在设置环境变量"
            )

        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.base_url = base_url
        self.transport_name = resolve_transport(base_url, transport)
        self.attachment_store = attachment_store
        self.session_attachments: dict[str, Any] = {}
        self.model = model
        self.context_limit = context_limit
        self.stream = stream
        self.max_attempts = max_attempts
        self.base_wait = base_wait
        self.max_wait = max_wait
        self.response_format = response_format
        self.chat_adapter = ChatAdapter(self._attachment_data_url)
        self.responses_adapter = ResponsesAdapter(self._attachment_data_url)

    def __call__(
        self,
        request: ModelRequest | list[dict[str, Any]],
        *,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
    ) -> Iterator[LLMEvent]:
        """调用一次 LLM，以事件流的形式产出结果。

        Args:
            messages: 完整对话上下文

        Yields:
            LLMEvent: ReasoningDelta / ContentDelta /   ContentDone / UsageEvent
        """
        if isinstance(request, ModelRequest):
            messages = request.copied_messages()
            tools = list(request.tools) if request.tools else tools
            response_format = request.response_format or response_format
            model = request.model or self.model
            transport = request.transport or self.transport_name
        else:
            messages = request
            model = self.model
            transport = self.transport_name
        if transport != "responses" and any(
            isinstance(message.get("provider_state"), dict)
            and "responses_output" in message["provider_state"]
            for message in messages
        ):
            raise ValueError(
                "Responses continuation state cannot be sent through Chat transport"
            )
        options = {
            "tools": tools or omit,
            "response_format": response_format or self.response_format or omit,
        }
        if transport == "responses":
            if self.stream:
                yield from self._call_responses_stream(messages, options, model)
            else:
                yield from self._call_responses_once(messages, options, model)
        elif self.stream:
            yield from self._call_stream(
                self.chat_adapter.encode_messages(messages),
                {**options, "tools": self._chat_tools(options["tools"])},
                model,
            )
        else:
            yield from self._call_once(
                self.chat_adapter.encode_messages(messages),
                {**options, "tools": self._chat_tools(options["tools"])},
                model,
            )

    def _attachment_data_url(self, attachment_id: str) -> str:
        if self.attachment_store is None:
            raise AttachmentError("image attachments are unavailable in this runtime")
        record = self.session_attachments.get(attachment_id)
        if record is None:
            raise AttachmentError("unknown attachment id")
        data = base64.b64encode(self.attachment_store.read_bytes(record)).decode("ascii")
        return f"data:{record.media_type};base64,{data}"

    def _chat_tools(self, tools: Any) -> Any:
        return omit if tools is omit else self.chat_adapter.encode_tools(tools)

    def _call_stream(
        self, messages: list[dict[str, Any]], options: dict, model: str
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
                model=model,
                stream=True,
                stream_options={"include_usage": True},
                **options,
            ),
            max_attempts=self.max_attempts,
            base_wait=self.base_wait,
            max_wait=self.max_wait,
        )

        content: list[str] = []
        reasoning: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish_reason = None
        for chunk in resp:  # 走到这里说明连接已建立;中途断流让它往上抛
            usage = getattr(chunk, "usage", None)
            if chunk.choices:
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta
                for call in delta.tool_calls or []:
                    target = tool_calls.setdefault(call.index, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if call.id:
                        target["id"] += call.id
                    if call.type:
                        target["type"] = call.type
                    if call.function:
                        target["function"]["name"] += call.function.name or ""
                        target["function"]["arguments"] += call.function.arguments or ""
                reasoning_piece = getattr(delta, "reasoning_content", None)
                content_piece = delta.content or getattr(delta, "refusal", None) or ""
                if reasoning_piece:
                    reasoning.append(reasoning_piece)
                    yield ReasoningDelta(reasoning_piece)
                if content_piece:
                    content.append(content_piece)
                    yield ContentDelta(content_piece)
            if usage:
                yield UsageEvent(usage)
        yield ContentDone(
            content="".join(content), reasoning="".join(reasoning),
            tool_calls=[tool_calls[index] for index in sorted(tool_calls)],
            finish_reason=finish_reason or "incomplete",
        )

    def _call_once(
        self,
        messages: list[dict[str, Any]],
        options: dict,
        model: str,
    ) -> Iterator[LLMEvent]:
        """非流式路径：一次性拿到完整响应，直接产出一个 ContentDone。"""
        resp = call_with_retry(
            lambda: self.client.chat.completions.create(
                messages=messages,
                model=model,
                stream=False,
                **options,
            ),
            max_attempts=self.max_attempts,
            base_wait=self.base_wait,
            max_wait=self.max_wait,
        )

        message = resp.choices[0].message
        content = message.content or message.refusal or ""
        reasoning = getattr(message, "reasoning_content", None) or ""

        if getattr(resp, "usage", None):
            yield UsageEvent(resp.usage)

        yield ContentDone(
            content=content, reasoning=reasoning,
            tool_calls=[call.model_dump(exclude_none=True) for call in message.tool_calls or []],
            finish_reason=resp.choices[0].finish_reason or "incomplete",
        )

    def _responses_tools(self, tools: Any) -> Any:
        return omit if tools is omit else self.responses_adapter.encode_tools(tools)

    def _responses_input(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self.responses_adapter.encode_input(messages)

    def _responses_options(self, options: dict) -> dict[str, Any]:
        result: dict[str, Any] = {
            "tools": self._responses_tools(options["tools"]),
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        response_format = options["response_format"]
        if response_format is not omit:
            result["text"] = {"format": response_format}
        return result

    def _call_responses_once(
        self, messages: list[dict[str, Any]], options: dict, model: str
    ) -> Iterator[LLMEvent]:
        response = call_with_retry(
            lambda: self.client.responses.create(
                model=model, input=self._responses_input(messages), stream=False,
                **self._responses_options(options),
            ),
            max_attempts=self.max_attempts, base_wait=self.base_wait, max_wait=self.max_wait,
        )
        if getattr(response, "usage", None):
            yield UsageEvent(response.usage)
        yield self.responses_adapter.decode_response(response)

    def _call_responses_stream(
        self, messages: list[dict[str, Any]], options: dict, model: str
    ) -> Iterator[LLMEvent]:
        response = call_with_retry(
            lambda: self.client.responses.create(
                model=model, input=self._responses_input(messages), stream=True,
                **self._responses_options(options),
            ),
            max_attempts=self.max_attempts, base_wait=self.base_wait, max_wait=self.max_wait,
        )
        completed = None
        for event in response:
            events, finished, error = self.responses_adapter.decode_stream_event(event)
            yield from events
            if finished is not None:
                completed = finished
            if error is not None:
                if completed is not None and getattr(completed, "usage", None):
                    yield UsageEvent(completed.usage)
                raise RuntimeError(f"Responses request failed: {error}")
        if completed is None:
            raise RuntimeError("Responses stream ended without response.completed")
        if getattr(completed, "usage", None):
            yield UsageEvent(completed.usage)
        yield self.responses_adapter.decode_response(completed)


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


def resolve_transport(base_url: str, requested: str | None) -> str:
    """Resolve automatic transport without speculative duplicate API calls."""
    choice = (requested or "auto").strip().lower()
    if choice not in {"auto", "chat", "responses"}:
        raise ValueError("WRIGHT_LLM_TRANSPORT must be auto, chat, or responses")
    if choice != "auto":
        return choice
    return "responses" if urlparse(base_url).hostname == "api.openai.com" else "chat"


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
