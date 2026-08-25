"""system prompt 组装。

回合格式不再手写——由协议层的唯一来源 AgentTurn 生成 JSON Schema 注入,
prompt 与回包校验同源,改 AgentTurn 两端一起变,不会漂。

仍保留散文 Rules:二选一、工具名必须匹配这些是【语义约束】,JSON Schema
表达不了(model_validator 在代码里兜),所以得在 prompt 里讲清楚。
"""

import json
from typing import Any

from .protocol import AgentTurn


def _curate(node: Any) -> Any:
    """剔掉自动生成的 schema 里不该给模型看的噪音。

    pydantic 会把【类的 docstring】塞进对象级 description——那是给开发者看的
    内部设计注释,泄漏给模型只会干扰。规则:凡是带 properties 的对象节点
    (即某个 BaseModel 生成的 schema),删掉它的 description;title 一律删(纯噪音)。
    字段级描述(写在属性里、自身不带 properties 的 Field(description=...))保留。
    这一步就是"主动筛选模型该看到什么"。
    """
    if isinstance(node, dict):
        node = {k: _curate(v) for k, v in node.items() if k != "title"}
        if "properties" in node:
            node.pop("description", None)
        return node
    if isinstance(node, list):
        return [_curate(v) for v in node]
    return node


# 模块加载时生成一次(静态)。schema 里带 {} ,所以不能直接拼进 .format 模板,
# 而是作为 format 的实参传入(实参里的花括号是字面量,不会被 format 再解释)。
TURN_SCHEMA = json.dumps(
    _curate(AgentTurn.model_json_schema()), ensure_ascii=False, indent=2
)

SYSTEM_PROMPT = """
You are an assistant with the following available tools:

Available tools:
{tools}

Each turn you MUST output exactly one JSON object that conforms to this schema:

{turn_schema}

Rules (semantic constraints the schema above cannot express):
- Output strict JSON parsable by `json.loads()`, with no surrounding commentary.
- Exactly one of the following holds each turn:
    (a) `tool_calls` is non-empty AND `final_answer` is null -> system runs the calls and returns results.
    (b) `tool_calls` is `[]` AND `final_answer` is non-null  -> session ends.
- Each `tool_calls[].name` must exactly match one of the tool names in "Available tools".
"""


def build_system_prompt(tools_json: str, memory_section: str = "") -> str:
    """把工具清单 + 回合 schema 注入模板,生成完整 system prompt。

    memory_section:可选的长期记忆指令段(由 MemoryManager.instructions() 提供),
    非空时追加到末尾。普通(无记忆的)子 Agent 传空串,行为与原来完全一致。
    """
    prompt = SYSTEM_PROMPT.format(tools=tools_json, turn_schema=TURN_SCHEMA)
    if memory_section:
        prompt = f"{prompt}\n{memory_section}\n"
    return prompt
