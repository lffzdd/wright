"""复用主 LLMClient 做一次性 side-query 的小工具。

selector(召回选择)和 extractor(记忆提取)都需要「给一组消息、拿回一段文本」,
而 LLMClient 对外吐的是事件流。这里把「drain 事件流取最终 content」收口成一个函数,
两处复用,不必新建 client。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...domain.model.events import ContentDone, UsageEvent
from ...domain.model.session import UsageRecord

if TYPE_CHECKING:
    from ...infrastructure.llm.llm import LLMClient


@dataclass(frozen=True)
class SideQueryResult:
    """One side-query. Missing usage is unknown, not zero."""

    content: str
    failed: bool
    failure_type: str
    usage: UsageRecord | None
    duration_ms: float
    model: str


def run_side_query(
    llm: LLMClient,
    system: str,
    user: str,
    observer=None,
    *,
    model: str = "",
) -> SideQueryResult:
    """Read the final ContentDone once and record the last usage snapshot once.

    Streaming deltas are ignored. A failed request still keeps usage that
    already arrived. Observer failures are logged and do not hide that usage.
    """
    from ...core.logger import get_logger

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    content = ""
    saw_done = False
    seen: list[UsageRecord] = []
    started = time.monotonic()
    model_name = model or str(getattr(llm, "model", "") or "")

    def observe(usage: UsageRecord) -> None:
        seen.append(usage)
        if observer is None:
            return
        try:
            observer(usage)
        except Exception:
            get_logger(__name__).debug("usage observer failed", exc_info=True)

    def events():
        nonlocal content, saw_done
        for event in llm(messages, response_format={"type": "json_object"}):
            if isinstance(event, ContentDone):
                if event.finish_reason not in {None, "stop"}:
                    raise ValueError(
                        f"Side query did not complete: {event.finish_reason}"
                    )
                content = event.content
                saw_done = True
            yield event
        if not saw_done:
            raise ValueError("Side query did not complete")

    error: Exception | None = None
    try:
        for _event in metered_events(events(), observe):
            pass
    except Exception as exc:
        error = exc
    return SideQueryResult(
        content="" if error is not None else content,
        failed=error is not None,
        failure_type=type(error).__name__ if error is not None else "",
        usage=seen[-1] if seen else None,
        duration_ms=(time.monotonic() - started) * 1000,
        model=model_name,
    )


def side_query(llm: LLMClient, system: str, user: str) -> str:
    """发一轮 system+user 的 side-query,返回最终文本内容。

    仅此类结构化数据查询使用 JSON mode；不传入 Agent 的工具清单。
    """
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    content = ""
    for event in llm(messages, response_format={"type": "json_object"}):
        if isinstance(event, ContentDone):
            if event.finish_reason not in {None, "stop"}:
                raise ValueError(f"Side query did not complete: {event.finish_reason}")
            content = event.content
    return content


def metered_events(events, observer):
    """Record the final usage snapshot once, including when a stream fails."""
    usage = None
    try:
        for event in events:
            if isinstance(event, UsageEvent):
                usage = UsageRecord.from_usage(event.usage)
            yield event
    finally:
        if usage is not None and observer is not None:
            observer(usage)
