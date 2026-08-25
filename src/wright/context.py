"""上下文压缩:token 逼近上限时,折叠旧的工具结果以腾出上下文。

这一块从 Agent 里独立出来,职责单一——只认 message_records(wire 记录)、
一个 context_tokens(running total)和 context_limit,不依赖完整 SessionState,
因此可以拿一组 MessageRecord + int 直接单测。

折叠是【就地改写】MessageRecord.message;调用方(Agent)负责把返回的 token_savings
从 session 的 running total 里扣掉。

compaction 可以改写、删除、合并非 assistant records;被 TurnRecord.message_id
引用的 assistant record 必须保留,除非未来把 assistant 原文归档到 TurnRecord。

注意:_is_tool_result_message / _fold_old_tool_results 编码了工具结果消息的
wire 格式({"tool_results": [...]}),必须和 util.build_tool_results_message
造出来的结构保持一致——两边改一处,另一处要跟着改。
"""

import json

from openai.types.chat import ChatCompletionMessageParam

from .renderer import Renderer
from .session import MessageRecord
from .util import estimate_message_tokens


class ContextCompactor:
    def __init__(
        self,
        renderer: Renderer,
        context_watermark: float = 0.75,
        keep_recent_tool_results: int = 3,
    ):
        self.renderer = renderer
        self.context_watermark = context_watermark
        self.keep_recent_tool_results = keep_recent_tool_results

    def compact_if_needed(
        self,
        message_records: list[MessageRecord],
        context_tokens: int,
        context_limit: int | None,
    ) -> tuple[int, int]:
        """超水位就折叠旧工具结果,返回 (折叠条数, 省下的 token 数)。

        调用方拿到 token_savings 后应 context_tokens -= savings 来维护 running total。
        只要越过水位线就 on_context_compact 上报一次——哪怕没有可折叠的旧结果
        (folded_count=0),也要让用户知道"上下文吃紧了但腾不出空间"。
        """
        if context_limit is None:
            return 0, 0

        if context_tokens <= context_limit * self.context_watermark:
            return 0, 0

        folded_count, token_savings = self._fold_old_tool_results(message_records)
        self.renderer.on_context_compact(
            folded_count,
            context_tokens,
            context_limit,
            self.context_watermark,
        )
        return folded_count, token_savings

    @staticmethod
    def _is_tool_result_message(msg: ChatCompletionMessageParam) -> bool:
        if msg.get("role") != "user":
            return False

        content = msg.get("content")
        if not isinstance(content, str):
            return False

        try:
            content_json = json.loads(content)
        except json.JSONDecodeError:
            return False

        return isinstance(content_json, dict) and isinstance(
            content_json.get("tool_results"), list
        )

    def _fold_old_tool_results(
        self, message_records: list[MessageRecord]
    ) -> tuple[int, int]:
        """压缩旧工具结果,保留最近 keep_recent 条完整结果。

        返回 (折叠条数, 省下的估算 token 数)。token_savings 用来让调用方
        从 running total 里扣减——折叠是就地改写 content,省下的就是
        (旧 content 估算 - 新 content 估算) 的差值。
        """
        keep_recent = max(0, self.keep_recent_tool_results)
        tool_result_indexes = [
            idx
            for idx, record in enumerate(message_records)
            if self._is_tool_result_message(record.message)
        ]
        indexes_to_fold = (
            tool_result_indexes[:-keep_recent] if keep_recent else tool_result_indexes
        )

        folded_count = 0
        token_savings = 0
        for idx in indexes_to_fold:
            msg = message_records[idx].message
            content = msg.get("content")
            if not isinstance(content, str):
                continue

            try:
                content_json = json.loads(content)
            except json.JSONDecodeError:
                continue

            if content_json.get("folded"):
                continue

            # 记下折叠前的估算
            old_tokens = estimate_message_tokens(msg)

            folded_results = []
            for item in content_json["tool_results"]:
                if not isinstance(item, dict):
                    continue

                result = item.get("result")
                ok = result.get("ok", True) if isinstance(result, dict) else True
                err = result.get("err", "") if isinstance(result, dict) else ""
                folded_results.append(
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "result": {
                            "ok": ok,
                            "err": err,
                            "data": "[旧工具结果已折叠以节省上下文]",
                        },
                    }
                )

            msg["content"] = json.dumps(
                {"tool_results": folded_results, "folded": True},
                ensure_ascii=False,
            )
            folded_count += 1

            # 折叠后的估算差值就是省下的 token
            new_tokens = estimate_message_tokens(msg)
            token_savings += old_tokens - new_tokens

        return folded_count, token_savings
