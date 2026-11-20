# 原生工具调用与旧版复盘

## 旧版保存在哪里

`legacy-json-react` 是迁移前的附注标签，指向提交
`4a3e062`（完整版本可用 `git rev-parse legacy-json-react^{commit}` 查看）。
迁移在同一仓库的 `codex/native-tool-calling` 分支完成。无需另建仓库。
标签完整保留旧版代码、测试和当时的文档；本地 `.env` 等被忽略的配置不属于 Git 快照。

只看旧文件，不切换当前目录：

```bash
git show legacy-json-react:src/wright/prompt.py
git show legacy-json-react:src/wright/protocol.py
git diff legacy-json-react -- src/wright/agent.py src/wright/llm.py
```

需要同时打开两份代码时，从仓库根目录执行：

```bash
git worktree add --detach ../wright-legacy-study legacy-json-react
```

这会创建同一仓库的旧版工作目录。依赖可在那个目录中用 `uv sync` 安装；
配置不会自动复制。用于运行旧会话时，使用旧版程序明确选择对应的会话。

## 按什么顺序理解

旧版阅读路线：

1. `prompt.py`：把工具列表、回合 JSON Schema 和二选一规则写进提示词。
2. `llm.py`：请求 JSON mode，接收正文字符串。
3. `protocol.py`：解析、修复、校验 JSON，生成 `ParsedTurn`。
4. `agent.py`：决定执行工具还是完成回答。
5. `executor.py`：校验参数、处理权限、执行工具、收集结果。
6. `util.py` / `session.py`：把结果装进用户消息，保存记录，继续下一轮。

新版沿用同一个执行循环，模型交互改为：

1. `protocol.encode_tools` 把模型可见工具放入 API `tools` 参数；
   过长或含特殊字符的 MCP 名称使用稳定别名，执行和权限仍使用原名。
   内置文件和 Shell 工具名为 `read`、`write`、`edit`、`ls`、`shell`。旧名
   （`read_file`、`write_file`、`edit_file`、`list_directory`、`execute_command`）
   只在 `domain/model/tool_names.py` 一处映射：模型回放旧名、checkpoint 中的调用、
   权限规则（含已保存的授权文件）、钩子 matcher 和 skill `allowed-tools`
   读入时换成新名，只输出新名。旧规则按新名匹配，范围不变，不会因改名失效。
   后台命令的 `get_command`、`wait_command` 合并为 `shell_output`（带 `timeout`
   时先等待），`terminate_command` 改为 `shell_kill`，同样登记在这张表里。
   计划工具 `create_plan`、`replan`、`get_plan` 已删除，`update_plan` 改为提交
   步骤列表：按 id（未给时按标题）匹配已有步骤，未列出的步骤保持原状，放弃步骤要显式标
   `skipped`；不同的 objective 加全新步骤表示替换计划。未完成的计划会带到下一轮用户消息，
   本轮更新前不参与完成检查。参数形状不同，不做别名，模型调用旧工具会收到“不可用”错误后改用新工具。
2. `llm.py` 接收 `assistant.tool_calls`，按索引拼接流式函数名和参数；
   保留供应商调用 ID、正文、可用的推理字段和结束原因。
3. `protocol.parse_turn` 严格解码工具参数。有工具就执行；无工具的正文就是回答。
   正文即便恰好是 JSON，也不会被解释为控制指令。
4. `executor.py` 继续承担参数校验、权限、超时、并发和结果收集。
5. `util.build_tool_results_messages` 为每个调用生成一条 `role=tool` 消息，
   用 `tool_call_id` 配对，再请求模型继续。

模型可同时输出说明文字和工具调用。工具定义逐请求传入，不在共享客户端上修改；
因此主 Agent、子 Agent 和记忆查询不会互相覆盖工具列表。

## 如何复盘新版

现有会话 checkpoint 和 lifecycle trace 仍保留：

- `message_records`：实际发送的原生 assistant 消息与 tool 结果消息。
- `turns[].parsed`：程序整理的正文、工具名称、参数、结束原因及最终答案。
- `tool_executions`：按调用 ID 保存执行状态、完整结果和错误。
- lifecycle trace：模型调用耗时、工具执行事件、用量和完成验证事件。

`parsed.final_answer` 等是程序内部记录字段，不再要求模型生成这个信封。
`reasoning` 只记录供应商实际返回的推理内容；它不代表模型完整的内部思考。
上下文压缩只作用于当次请求投影：它折叠可丢弃的旧工具结果正文，保留角色和调用 ID；完整结果仍在执行记录中。带 `retention=instruction` 的结果（技能正文、`tool_search` 返回的 schema）不会被换成占位符。

## 延迟工具：占位 schema、`tool_search` 与 `call_tool`

`defer_to_model=True` 的工具（MCP、定时任务、子 Agent 管理、部分记忆工具等）
已注册、受权限约束，但完整 schema 不进入请求的 `tools` 参数。`encode_tools`
只给它们发一个占位：真实名称、描述的第一句，参数是 `{"type": "object"}`。
模型这样使用它们：

1. `tool_search(query | names)` 返回匹配工具的完整 schema（名称、描述、参数），
   每项带 `invoke`：`direct` 或 `call_tool`。结果带 `retention=instruction`，
   `retention_key` 是 `tool_search:` 加排序后的工具名，压缩时保留；超过水位线折叠时，
   同一组工具较早的副本标成 superseded。
2. `invoke=direct` 的工具用真实名称直接发起原生调用。`ToolDispatchService` 用真实
   schema 做 jsonschema 校验，不用占位 schema。模型没先搜索就直接调用时，校验失败的
   结果里附上完整 `parameters`，同样带 `retention=instruction`，下一次即可改对。
3. 延迟工具超过 `DEFERRED_STUB_LIMIT`（64，见 `domain/protocol.py`）个时，超出部分
   不发占位，只能经 `call_tool(name, arguments)` 调用。内置延迟工具先占名额，MCP 工具
   排在后面，所以溢出的总是 MCP 工具的尾部。上限是为了给服务商的函数个数上限
   （OpenAI 每次请求 128 个）留出基础工具的位置。只有存在溢出时 `tools` 里才有
   `call_tool`。
4. 两条路径共用分发层的同一套流程。`call_tool` 在任何检查之前还原成真实调用：交互模式、
   MCP 开关、能力快照、参数校验、钩子、出口检查、权限和审批都按真实工具名执行，
   事件和 trace 里也是真实工具名；transcript 保留模型实际发出的 `call_tool` 调用。
   `call_tool` 只能到达已暴露的延迟工具。名称未知、指向基础工具、指向
   `tool_search`/`call_tool` 自身或 `expose_to_model=False` 的工具时，只让这一个调用失败。

为什么这样做：`tools` 参数排在服务商前缀缓存的最前面，任何改动都让整段缓存失效。
占位 schema 在整个会话里固定不变，发现新工具只往 transcript 末尾追加内容，不改 `tools`，
也不需要"已激活集合"、上限和淘汰。模型用原生方式调用真实名称，比间接的 `call_tool`
更稳；`call_tool` 只兜底超出上限的部分。这一方案不依赖任何服务商的 beta 能力。
参数校验本来就由 Wright 在执行前用 jsonschema 完成（请求里发的是 `strict: false`），
所以占位 schema 不会让校验变弱。

基础工具（文件、shell、计划、网络、`spawn_agent` 等）直接调用。模型调用一个不在本轮
`tools` 里的名称时，这一个调用收到失败结果（溢出的延迟工具会提示改用 `call_tool`），
同一回合的其他调用照常执行，不算无效回合。

## 交互模式和 MCP 开关不改 `tools`

Ask/Plan 模式不再从 `tools` 里隐藏工具，关掉的 MCP server 的占位也照常保留。
`AgentPromptManager.visible_schema_tools` 只按"有无技能"决定是否列出 `load_skill`，
所以切换模式、开关 MCP server 都不会让缓存失效。这两类限制通过两处生效：

- 每次请求末尾的临时提醒，不写入 transcript：Ask/Plan 下有 `<interaction-mode>`
  （`workspace/modes.py` 的 `mode_notice`），有 server 被关掉时有 `<mcp-servers-off>`。
  放在末尾，前面的缓存前缀不变。
- 执行层照旧拦截：分发层的 `tool_visible` 检查、MCP 开关检查，以及权限层的
  `operations_allowed`。被拦下的调用返回失败结果，说明当前模式或哪个 server 已关闭，
  以及替代做法。`tool_search` 不返回已关闭 server 的工具。

代价是模型仍能看到被禁止工具的名字，偶尔会去试一次；安全性不受影响。关掉 server
也不再省掉它的占位 token（MCP 页的 token 数照实计入）。于是 `tools` 在整个会话里
保持逐字节不变。

旧 checkpoint 里的 `active_deferred_tools` 字段在加载时忽略。

## system prompt 每次请求重新渲染

会话创建时把第一份 system prompt 写进 transcript，只作历史；每次请求都由
`agent/system_prompt.py` 的 `render_system_prompt` 按当前的工具列表、记忆说明和
角色说明重新生成，在请求副本里替换掉存下的那份，开着记忆时再加常驻记忆块。
恢复的会话因此跟着当前配置走：换了 MCP server、开了知识检索、关掉了
`memory.enabled`，提示里的 "Always available" 和记忆说明都会对上实际的工具。

渲染是确定的，同一个 Agent 的每次请求逐字节相同，不影响提示缓存；只有配置变了
提示才变，而那时 `tools` 也变了，缓存本来就要失效一次。

## Anthropic Messages 传输

`api.anthropic.com` 默认走 `messages` 传输：`infrastructure/llm/anthropic_client.py`
用官方 `anthropic` SDK 调 `POST /v1/messages`，编码和解码在
`domain/model/llm/anthropic_adapter.py`，不含 SDK 类型。约定与其他传输相同：

- `tools` 按会话固定的顺序编码为 `name`/`description`/`input_schema`，不发 `strict`；
  延迟工具照样只发占位（`input_schema` 为 `{"type": "object"}`），超出上限的经
  `call_tool` 调用，请求里的工具数组整个会话不变。交互模式和 MCP 开关的临时提醒
  排在历史断点之后，不影响缓存。
- 第一条 system 消息成为顶层 `system`；连续的 `role=tool` 合成一条带 `tool_result`
  的 user 消息，工具图片放在同一条消息里。
- 来自该 API 的 assistant 回合把原始内容块存进 `provider_state["anthropic_content"]`，
  下次请求原样回放（含 thinking 块和 `signature`）。来自其他传输的回合按正文和
  `tool_calls` 重建，所以 Chat 与 Anthropic 端点之间可以互相续接。
- 停止原因映射：`end_turn`/`stop_sequence` → `stop`，`tool_use` → `tool_calls`，
  `max_tokens` → `length`，`refusal` → `refusal`；后两者不会触发工具执行。

缓存断点只放两个：最后一个 system 块（工具定义渲染在它之前，一起缓存），以及
持久历史的最后一个可缓存块（`ModelRequest.cache_prefix_messages`，由 `ContextView`
给出），本轮临时提醒在断点之后。thinking 块不能带断点，会向前找文本或工具块。
命中情况看 `usage.cache_read_input_tokens`，`provider_usage` 已把它计入
`cache_read_tokens`。

Claude 4.6 及以后的模型发 `thinking: {"type": "adaptive", "display": "summarized"}`，
推理摘要照常流式显示；更早的模型不发 thinking。会检查 thinking 块会话前缀的模型
（Fable/Mythos 5.1+，Opus/Sonnet/Haiku 5.5+）额外带
`block_binding.prefix_mismatch_behavior: "drop_block"`（beta
`thinking-binding-controls-2026-08-01`）：上下文压缩折叠旧工具结果属于改写历史，
这样只会丢掉受影响的 thinking 块，不会返回 400。折叠位置变化时，断点之后的消息
缓存会失效，这是已知代价。

## 会话兼容与失败处理

新版只运行原生协议。checkpoint 版本从 1 升到 2；旧版会话明确拒绝加载，
提示使用 `legacy-json-react` 打开。迁移不转换、覆盖或删除已有会话，新版从新会话开始。

被截断、被过滤或缺少结束标记的响应不能触发工具执行。
无效参数不会通过补括号或抽取正文来修复；无效回合被记录并进行有限重试。
工具执行前保存 pending checkpoint；恢复时补齐缺失的结果消息，
未知结果标记为“需要先检查现场”，不自动重放可能已经完成的写操作。

JSON mode 仍用于记忆选择、记忆提取等明确需要结构化数据的查询，
不会约束普通 Agent 的整轮回答。完成门闩默认是结构化检查
（计划、未完成工具、缺失写入产物），不额外调用评审模型。

## 验证

```bash
uv run pytest -q
uv run python scripts/smoke_agent.py
uv run python scripts/smoke_subagent.py
```

原生协议测试覆盖实际 Python SDK 的流式/非流式解码、交错工具参数、
多调用 ID 配对、文本与调用并存、共享客户端隔离、无效/截断响应、
取消后的会话续用、MCP 别名和崩溃恢复。

接口依据：[OpenAI function calling 文档](https://developers.openai.com/api/docs/guides/function-calling)。
