import json

from .tools.base import ToolCall, ToolResult

# 入站解析(模型输出 → 结构化回合)已搬到 protocol.py。
# 本模块只留出站编码:把工具执行结果拼回喂给模型的 wire 消息。

# 按英文/JSON 经验约 4 字符/token，中文可能偏小。
# 服务端 P+C 提供每轮估算锚点；新增消息和折叠节省量仍是估算。
CHARS_PER_TOKEN = 4


def tool_image_references(message: dict) -> list[dict]:
    """Read managed image references from a native tool-result envelope."""
    if message.get("role") != "tool" or not isinstance(message.get("content"), str):
        return []
    try:
        result = json.loads(message["content"])
    except (ValueError, TypeError):
        return []
    refs = result.get("artifacts") if isinstance(result, dict) else None
    if not isinstance(refs, list):
        return []
    return [ref for ref in refs if isinstance(ref, dict)
            and str(ref.get("media_type", "")).startswith("image/")]


def estimate_tokens(text: str) -> int:
    """按字符数糙估一段文本的 token 数。"""
    return len(text) // CHARS_PER_TOKEN


def estimate_message_tokens(message: dict) -> int:
    """估算正文、原生工具参数和供应商推理字段的 token 数。"""
    content = message.get("content")
    count = estimate_tokens(content) if isinstance(content, str) else 0
    if message.get("tool_calls"):
        count += estimate_tokens(json.dumps(message["tool_calls"], ensure_ascii=False))
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        count += estimate_tokens(reasoning)
    # Before a provider reports usage we deliberately reserve a conservative
    # budget for every image.  Base64 length is not a token estimate and must
    # never be persisted merely to calculate this number.
    parts = message.get("parts")
    if isinstance(parts, list):
        count += 1024 * sum(
            isinstance(part, dict) and part.get("type") == "image"
            for part in parts
        )
    count += 1024 * len(tool_image_references(message))
    return count


def estimate_tools_tokens(tools: tuple[dict, ...] | list[dict]) -> int:
    """Estimate request schema input separately from history and actual usage."""
    return estimate_tokens(json.dumps(tools, ensure_ascii=False, sort_keys=True))


def build_tool_results_messages(
    tool_tuple: list[tuple[ToolCall, ToolResult]],
) -> list[dict]:
    """One native result per call, preserving the provider's call ID."""
    return [
        {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result.to_dict(), ensure_ascii=False),
        }
        for call, result in tool_tuple
    ]
