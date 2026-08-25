import json
from openai.types.chat import ChatCompletionMessageParam

from .tools.base import ToolCall, ToolResult

# 入站解析(模型输出 → 结构化回合)已搬到 protocol.py。
# 本模块只留出站编码:把工具执行结果拼回喂给模型的 wire 消息。

# 糙估系数:按英文/JSON 经验 ~4 字符/token,中文会偏小。它只用于"还没被服务端
# usage 校准的那截尾巴"(running total 每轮会被 P+C 校准回真值),误差被限制在一轮
# 工具输出内。要精确就换 tiktoken(OpenAI) 或 count_tokens 接口(Anthropic),接口不变。
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """按字符数糙估一段文本的 token 数。"""
    return len(text) // CHARS_PER_TOKEN


def estimate_message_tokens(message: ChatCompletionMessageParam) -> int:
    """估算单条 wire 消息的 token 数(只数 str 形态的 content)。"""
    content = message.get("content")
    return estimate_tokens(content) if isinstance(content, str) else 0


def build_tool_results_message(
    tool_tuple: list[tuple[ToolCall, ToolResult]],
) -> ChatCompletionMessageParam:
    msg: ChatCompletionMessageParam = {
        "role": "user",
        "content": json.dumps(
            {
                "tool_results": [
                    {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "result": tool_result.to_dict(),
                    }
                    for tool_call, tool_result in tool_tuple
                ]
            },
            ensure_ascii=False,
        ),
    }
    return msg
