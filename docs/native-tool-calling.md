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
上下文压缩只折叠旧工具结果的正文，保留角色和调用 ID；完整结果仍在执行记录中。

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
