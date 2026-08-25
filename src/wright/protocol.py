"""回合协议层:把模型一轮的原始输出解析 + 校验成结构化的 ParsedTurn。

这一层是"契约"的代码化身,而且是【唯一事实来源】。在此之前,契约的形状在两处
各写一份——prompt 里手写的 JSON 示例 + 这里手写的校验逻辑——改一处忘另一处就漂。
现在改成路线 a:用 pydantic 模型 AgentTurn 定义一次,两端都从它派生——
  - 给模型看的格式描述  = AgentTurn.model_json_schema()(见 prompt.py)
  - 回包校验            = AgentTurn.model_validate()(见 parse_turn)
一份定义喂两端,不可能漂移。

两条边界仍然成立:
  - 服务端的 json_object 保证"是合法 JSON",本层只管"形状/语义对不对"。
    顶层 JSON 非法 / 不符 schema / 二选一违规 → TurnAbort(整轮中止,喂回模型重答)。
  - 工具"存不存在"不归本层,归 executor 查 registry;本层只验形状。

设计变更(相对手写版):tool_calls 现在被 pydantic 严格逐条校验,单条坏掉
即整轮 TurnAbort(带 pydantic 的精确定位),不再造 error 占位单独 fail。
严格校验与逐条优雅降级本质冲突,这里选了前者——回包既已是合法 JSON,单条
schema 错属模型笔误,把精确报错喂回比静默占位更可操作。
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from .tools.base import ToolCall


class TurnAbort(Exception):
    """本轮 LLM 输出无法解析或校验,这一轮没法继续。

    与"单个工具失败"区分开:工具失败是数据(ToolResult.fail),整轮照常;
    TurnAbort 是整轮中止,只能把错误喂回 LLM 让它重答。
    主循环专门捕获它,其它异常一律放行(那是真 bug,不该被静默吞掉)。
    """


class ToolCallSpec(BaseModel):
    """单个工具调用的形状契约:name + arguments。

    它和 prompt 里"调用长什么样"的描述同源——model_json_schema() 生成给模型看的
    部分,model_validate() 做回包校验。改这里两端一起变。
    """

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentTurn(BaseModel):
    """一轮模型输出的信封契约——本协议的唯一事实来源。

    注意:"二选一"是【语义约束】,JSON Schema 表达不了,所以它既要在下面的
    model_validator 里用代码兜底,也要在 prompt 里用散文讲一遍(schema 生成不出
    这条规则)。这正是"形状能同源,语义约束难完全同源"的活例子。
    """

    reasoning: str = ""
    tool_calls: list[ToolCallSpec] = Field(default_factory=list)
    final_answer: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "AgentTurn":
        has_tools = len(self.tool_calls) > 0
        has_final = self.final_answer is not None
        # 恰好一个:两者同真(都有)或同假(都无)都违规。
        if has_tools == has_final:
            raise ValueError(
                "每轮必须恰好二选一:非空 tool_calls 或 非空 final_answer"
            )
        return self


@dataclass
class ParsedTurn:
    """一轮模型输出解析校验后的结构化结果。

    kind 决定主循环走哪条路:final 直接收尾返回 final_answer;tool_calls 把
    tool_calls 交给执行器。parsed 保留原始 dict,供 session 记账留档。
    """

    kind: Literal["final", "tool_calls"]
    parsed: dict
    final_answer: Any = None
    tool_calls: list[ToolCall] = field(default_factory=list)


def parse_turn(raw: str) -> ParsedTurn:
    """解析 + 校验模型一轮原始输出。任何形状/语义级错误抛 TurnAbort。"""
    data = _loads(raw)
    try:
        turn = AgentTurn.model_validate(data)
    except ValidationError as e:
        # pydantic 报错带精确定位(哪个字段、缺什么、类型错在哪),原样喂回最有用
        raise TurnAbort(f"回合不符合 schema:{e}") from e

    # _exactly_one 已保证恰好一侧成立,这里据 final_answer 是否为 None 分流即可。
    if turn.final_answer is not None:
        return ParsedTurn(kind="final", parsed=data, final_answer=turn.final_answer)

    # id 由系统盖章(执行阶段靠 id 对账);spec 已是校验过的合法调用。
    tool_calls = [
        ToolCall(
            name=spec.name,
            arguments=spec.arguments,
            id=f"call_{uuid.uuid4().hex[:6]}",
        )
        for spec in turn.tool_calls
    ]
    return ParsedTurn(kind="tool_calls", parsed=data, tool_calls=tool_calls)


def _strip_markdown_wrapper(text: str) -> str:
    """剥离模型输出首尾的 Markdown 代码块标记（如 ```json ... ```）。"""
    cleaned = text.strip().lstrip("\ufeff")
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def _fix_unescaped_control_chars(text: str) -> str:
    """转义字符串内所有未转义的 JSON 控制字符（U+0000—U+001F）。"""
    result: list[str] = []
    in_string = False
    escaped = False

    for char in text:
        if in_string:
            if escaped:
                result.append(char)
                escaped = False
            elif char == "\\":
                result.append(char)
                escaped = True
            elif char == '"':
                result.append(char)
                in_string = False
            elif ord(char) < 0x20:
                # json.dumps 会为 \b/\f/\n/\r/\t 选短转义，其余用 \u00xx。
                result.append(json.dumps(char)[1:-1])
            else:
                result.append(char)
            continue

        if char == '"':
            in_string = True
        result.append(char)

    return "".join(result)


def _fix_trailing_commas(text: str) -> str:
    """移除 JSON 对象或数组末尾多余的逗号（如 {"a": 1,} 或 [1, 2,]）。"""
    # 不能用正则：字符串值本身可能合法地包含 `,}` 或 `,]`。
    result: list[str] = []
    in_string = False
    escaped = False
    length = len(text)

    for index, char in enumerate(text):
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            result.append(char)
            continue

        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead].isspace():
                lookahead += 1
            if lookahead < length and text[lookahead] in "}]":
                continue

        result.append(char)

    return "".join(result)


def _close_unfinished_containers(text: str) -> str:
    """补齐输出末尾遗漏的 `}`/`]`，但不猜测未闭合字符串或错配结构。"""
    stack: list[str] = []
    in_string = False
    escaped = False

    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]":
            # 中途闭合错配不是简单的输出截尾，不能擅自猜测修复。
            if not stack or stack[-1] != char:
                return text
            stack.pop()

    # 字符串本身未结束往往表示回答真的被截断；此时不要把半段内容当成功。
    if in_string:
        return text
    return text + "".join(reversed(stack))


def _object_candidates(text: str) -> list[tuple[int, dict]]:
    """找出文本中可独立解码的 JSON 对象，不被字符串内的花括号干扰。"""
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, dict]] = []
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((start, value))
    return candidates


def _decode_embedded_object(text: str) -> dict | None:
    """从带说明文字的输出中选择最像回合信封的 JSON 对象。"""
    candidates = _object_candidates(text)
    if not candidates:
        return None

    protocol_keys = {"reasoning", "tool_calls", "final_answer"}
    # 优先包含最多协议字段的对象；同分时采用最先出现的对象。这样不会误选
    # tool_calls.arguments 中可独立解析的嵌套对象。
    _, value = max(
        candidates,
        key=lambda item: (len(protocol_keys.intersection(item[1])), -item[0]),
    )
    return value


def _decode_once_or_twice(text: str) -> Any:
    """解析完整 JSON；兼容少数网关把整个 JSON 信封再次编码成字符串。"""
    value = json.loads(text)
    if isinstance(value, str):
        nested = value.strip()
        if nested.startswith("{"):
            value = json.loads(nested)
    return value


def _format_decode_error(error: json.JSONDecodeError, text: str) -> str:
    """给重试提示保留准确位置和一小段上下文。"""
    start = max(0, error.pos - 30)
    end = min(len(text), error.pos + 30)
    excerpt = text[start:end].replace("\n", "\\n").replace("\r", "\\r")
    return (
        f"{error.msg} (第 {error.lineno} 行, 第 {error.colno} 列); "
        f"附近内容: {excerpt!r}"
    )


def _loads(raw: str) -> dict:
    """强健地解析 JSON 并确认顶层是对象。

    包含多重容错与防御机制：
    1. 剥离 Markdown 代码块（如 ```json ... ```）
    2. 尝试标准解析
    3. 修复字符串内未转义的原生换行符/控制字符
    4. 移除末尾多余逗号
    5. 补齐末尾遗漏的对象/数组闭合括号
    6. 从说明文字中定位可独立解码的协议对象
    """
    if not isinstance(raw, str):
        raise TurnAbort(f"LLM 输出必须是字符串, 得到 {type(raw).__name__}")
    if not raw.strip():
        raise TurnAbort("LLM 输出为空")

    cleaned = _strip_markdown_wrapper(raw)
    fixed_controls = _fix_unescaped_control_chars(cleaned)
    closed = _close_unfinished_containers(fixed_controls)
    repaired = _fix_trailing_commas(closed)
    variants = [cleaned] if repaired == cleaned else [cleaned, repaired]
    errors: list[tuple[json.JSONDecodeError, str]] = []
    non_object_type: str | None = None

    # 先要求整段是 JSON，避免对本来正确的内容做任何修改。
    for variant in variants:
        try:
            data = _decode_once_or_twice(variant)
        except json.JSONDecodeError as error:
            errors.append((error, variant))
        else:
            if isinstance(data, dict):
                return data
            non_object_type = type(data).__name__

    # 再处理代码块外说明文字、<think> 标签等包装。raw_decode 会在正确的
    # 对象闭括号处停止，因此不会被后缀中的 `}` 或字符串里的花括号带偏。
    for variant in variants:
        data = _decode_embedded_object(variant)
        if data is not None:
            return data

    if non_object_type is not None:
        raise TurnAbort(f"LLM 输出顶层必须是对象, 得到 {non_object_type}")

    if errors:
        # 修复后走得最远的错误通常最接近真正病灶，比最后重跑原文更有用。
        error, source = max(errors, key=lambda item: item[0].pos)
        detail = _format_decode_error(error, source)
        raise TurnAbort(f"LLM 输出不是合法 JSON: {detail}") from error

    raise TurnAbort("LLM 输出中没有找到 JSON 对象")
