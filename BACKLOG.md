# Backlog

当时没做到最好、或以后想升级的点。每条写清现状、为什么没做、什么时候该做。
做完就删掉这一条，历史看 git。要分派或讨论的事改开 issue。

## 工具加载

### 占位 schema 的提示句可以挪进 system prompt
- 现状：每个延迟工具的占位都带一句 "Parameters are omitted here; call tool_search with this name before first use."，实测每个占位约 58 token，这句占了不少
- 为什么没做：占位在缓存前缀里，实际花费很小
- 什么时候做：延迟工具接近占位上限，或接入不做前缀缓存的服务商时
- 相关：`src/wright/domain/protocol.py`（`_STUB_HINT`）、`src/wright/domain/prompt.py`

### 占位上限 64 是写死的
- 现状：`DEFERRED_STUB_LIMIT = 64`，不看服务商的函数个数上限，也不看基础工具有多少
- 为什么没做：目前工具数远低于 OpenAI 每次请求 128 个函数的上限
- 什么时候做：MCP 工具多到出现溢出，或接入函数上限更低的服务商时
- 相关：`src/wright/domain/protocol.py`（`call_tool_only`）

### `tool_search` 可以接受 `name` 作为 `names` 的别名
- 现状：真实模型测试里 21 次有 1 次传了 `{"name": ...}`，校验失败后模型自己改对
- 为什么没做：只多一个来回，频率低
- 什么时候做：换模型后这类错误变多时
- 相关：`src/wright/application/tool_execution/tool_search.py`

### MCP 开关可以加"关掉时连占位也移除"的选项
- 现状：关掉 server 后占位保留（保缓存），模型仍能看到工具名，token 也照算
- 为什么没做：选的是保缓存，目前没人需要用开关省上下文
- 什么时候做：有用户关 server 是为了省上下文、或某个 server 工具特别多时
- 相关：`src/wright/application/agent/prompt.py`（`visible_schema_tools`）、`docs/native-tool-calling.md`

### `AgentProfile.allowed_tools` 和 `Tool.roles` 功能重叠
- 现状：两者都能限制某个 Agent 拿到哪些工具，同时存在
- 为什么没做：各自有调用方，合并要动子 Agent 和 durable run 的组装
- 什么时候做：再加一种执行角色，或两处规则出现不一致时
- 相关：`src/wright/domain/model/agent/profile.py`、`src/wright/application/execution/roles.py`

### durable run 仍能用 MCP 工具
- 现状：MCP 工具声明 `ALL_ROLES`，定时任务等无人值守的运行也能调用
- 为什么没做：用户可能为这些工具配了 allow 规则，直接拿掉会破坏已有用法
- 什么时候做：重新梳理无人值守运行的权限边界时
- 相关：`src/wright/infrastructure/tools/mcp_client.py`、`docs/permissions.md`

## 验证

### 用其他模型跑一遍延迟工具和模式的真实测试
- 现状：只用 deepseek-v4-flash 跑过 7 个场景 × 3 次，结果全部符合预期
- 为什么没做：当时只有这一个模型的配置
- 什么时候做：换默认模型或接入新服务商时
- 相关：测试脚本当时放在会话临时目录，没进仓库；需要时可按同样思路写进 `scripts/`

### 原生 Anthropic Messages 传输没有真实 API 测试
- 现状：只有假 HTTP 的单元测试（`tests/test_anthropic_messages.py`），缓存断点和 thinking 回放没在真实 API 上验证
- 为什么没做：`.env` 里没有 Anthropic API Key
- 什么时候做：配好 Key 后，看 `cache_read_input_tokens` 是否随轮次增长
- 相关：`src/wright/infrastructure/llm/anthropic_client.py`、`src/wright/domain/model/llm/anthropic_adapter.py`

### 并行跑测试时有偶发失败
- 现状：做 Anthropic 适配器的会话报告并行跑 pytest 时有 4–7 个测试偶发失败，单独跑能过；合并后在 main 上跑一次没复现，具体是哪些测试没有记录
- 为什么没做：没能稳定复现
- 什么时候做：再次出现时记下测试名，查共享状态或时序问题

## Web UI

### `call_tool` 调用的显示没检查
- 现状：事件和 trace 用真实工具名，transcript 记录的是 `call_tool`，界面上的工具卡片显示哪一个没有看过
- 为什么没做：当时只改了后端
- 什么时候做：下次动工具卡片，或 MCP 工具多到出现溢出时
- 相关：`web/src/components/cards/`
