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
- 2026-11-20 复现一次：`tests/knowledge/test_service.py::test_api_embedder_missing_key_raises` 在 `-n auto` 下失败，单独跑能过
- 同日又一次：`tests/test_capabilities.py::test_command_tool_starts_and_waits_through_execution_backend`，同样单独跑和整体重跑都能过
- 为什么没做：没能稳定复现
- 什么时候做：再次出现时记下测试名，查共享状态或时序问题

## Context 面板

### 还没有 `/compact` 命令
- 现状：面板的"压缩上下文"按钮只在服务端注册了 `/compact` 时出现，后端没有这个命令，所以按钮不显示。压缩只有自动折叠：请求超过水位线后折叠较早的工具结果
- 为什么没做：手动压缩要先定义语义（立即折叠到哪里、折叠是否跨请求保持、是否做摘要），还会改变提示缓存前缀，需要单独设计
- 什么时候做：用户需要在到达水位线之前主动腾出空间时
- 相关：`src/wright/application/agent/context.py`（`ContextCompactor`）、`web/src/components/inspector/ContextPanel.tsx`（`PANEL_COMMANDS`）

### 面板上不能折叠或丢弃单条工具结果
- 现状：条目只能跳到对话里对应的调用
- 为什么没做：和 `/compact` 一样，需要一个跨请求保持的折叠标记
- 什么时候做：和 `/compact` 一起做

### 回复预留是固定上限，不是模型真实的最大输出
- 现状：预留取窗口的 1/8，最多 32K（`MAX_OUTPUT_RESERVE`）。目录里没有各模型的最大输出；Anthropic 传输实际请求 64K
- 为什么没做：要逐个服务商核对官方文档，目录目前只记上下文窗口
- 什么时候做：给目录补模型元数据时，或预留明显偏离实际回复长度时
- 相关：`src/wright/domain/model/llm/catalog.py`、`src/wright/infrastructure/llm/anthropic_client.py`

### 分类数字是粗略估算
- 现状：总数已按上一次请求的 provider 计数校准，各分类和上一次请求之后新增的内容仍是粗估：UTF-8 字节数 ÷ 4 向上取整，每条消息另加 3（`utils/token_counter.py`）。中文因此略偏高（DeepSeek 参考约 0.6 token/字，估算 0.75）
- 为什么没做：本地分词器要按服务商各配一套（OpenAI 的 tiktoken、DeepSeek/Qwen 在 Hugging Face 上的分词器），会增加依赖、拖慢计数；Anthropic 和 Gemini 只有在线 count_tokens 接口
- 什么时候做：新增内容的估算误差影响到折叠或压缩的时机时；或 Anthropic 传输稳定后，只对 Anthropic 模型用 count_tokens 精确计分类

### 系统提示词里没有项目指令
- 现状：Wright 不读取项目里的 AGENTS.md 这类说明文件，所以面板也没有"项目指令"这一类
- 为什么没做：这是独立的功能，和面板无关
- 什么时候做：要支持项目级说明文件时，同时在 `context_usage.py` 加一个分类

### 只有工具调用能跳回对话
- 现状：详情面板里只有带 `call_id` 的条目（工具调用和结果）有"在对话中查看"。轮次、用户消息、模型正文和推理不能跳，因为时间线的消息没有可定位的 DOM 锚点
- 为什么没做：要给时间线每条消息加锚点和滚动逻辑，属于时间线的改动
- 什么时候做：时间线支持按消息 id 定位时，用条目的 `id` 和轮次的 `record_id` 接上
- 相关：`web/src/components/inspector/ContextDetail.tsx`、`src/wright/application/workspace/timeline.py`

### 分类详情总是按下一次请求组装
- 现状：`/context/detail` 和完整请求弹窗（`/context/request`）每次按需组装下一次请求（同 Context 页空闲时的预览），不含本轮的记忆召回；请求进行中时，概览显示的是在途请求，详情可能多出或少几项。弹窗顶部总数和圆环一样按上次服务商计数校准，各分区仍是粗估
- 为什么没做：让详情跟随在途请求要改快照的组装方式
- 什么时候做：用户反映概览和详情对不上时
- 相关：`src/wright/application/agent/model_turn.py`（`preview_context`、`request_context`）

### 看不到某一次调用实际发了什么
- 现状：Context 面板只显示当前上下文（下一次请求）。之前弹窗里的"上一次请求"只存在内存里、重启就没了，而且空闲时和下一次几乎一样，已经去掉
- 为什么没做：按调用记录原始请求属于调试和追踪（类似 LangSmith、Langfuse 的 trace），要决定存哪里、存多久、占多少空间，和上下文面板是两件事
- 什么时候做：需要排查"模型为什么这么回答"、对比两次请求的差异时；做法是每次调用把请求（或相对上一次的增量）写进 trace，在时间线的每个模型回合上点开查看
- 相关：`src/wright/application/agent/model_turn.py`、`docs/lifecycle-hooks-and-traces.md`

### 内置工具没有开关
- 现状：skill 有开关（`skills.disabled`），MCP server 有会话级开关，内置工具（shell、edit 等）没有
- 为什么没做：工具 schema 要整个会话保持字节不变才能命中缓存，关掉内置工具只能像 MCP 一样由调度器拒绝、schema 照发，省不了上下文；多数工程 Agent 也没有这项
- 什么时候做：有人需要在某个项目里禁用某个内置工具（例如不许联网搜索）时，按 MCP 开关的做法加
- 相关：`src/wright/application/agent/prompt.py`（`visible_schema_tools`）、`src/wright/application/tool_execution/dispatch.py`

## Web UI

### Files 面板的"本会话"每个工具只能跳到最近一次
- 现状：每个文件对每个工具只记最近一次调用（`last_calls`），标签里的 "read ×2" 只能跳到最后那次读
- 为什么没做：要逐次保存每个文件的调用 id，快照会变大；多数时候看最近一次就够，回看改动有 Changes 页
- 什么时候做：需要按时间回看某个文件被反复读写的过程时，把 `last_calls` 换成按工具的调用列表，界面上点同一个工具名依次往前跳
- 相关：`src/wright/application/workspace/timeline.py`、`web/src/components/inspector/FilesPanel.tsx`

### `call_tool` 调用的显示没检查
- 现状：事件和 trace 用真实工具名，transcript 记录的是 `call_tool`，界面上的工具卡片显示哪一个没有看过
- 为什么没做：当时只改了后端
- 什么时候做：下次动工具卡片，或 MCP 工具多到出现溢出时
- 相关：`web/src/components/cards/`

### 回合结束后把过程折叠成一行
- 现状：思考和工具调用一直平铺。每段思考已经折成一行预览，但一个回合里步骤多时，时间线仍然很长
- 为什么没做：试过按"连续步骤"折叠（`StepGroup` 里 3 步以上收成摘要），有三个问题，已经撤掉：
  1. 中间的旁白、编辑和提问会把一个回合切成好几段，爱写旁白的模型（Claude、GPT）会出现多行摘要夹着旁白，每段又常常不到门槛，结果不折叠
  2. 门槛按每段算，不按整个回合算
  3. 回合结束时马上收起，正在往上看过程的人会遇到内容消失、滚动位置跳动
- 什么时候做：再有人嫌时间线太长时。做法是按回合折叠：用户提问之后、最终回复之前的思考、工具、旁白和编辑都收进一行摘要（`summarizeSteps` 可以复用），提问单独显示，门槛按整个回合的总步数算，不在底部时不在结束那一刻收起；页脚的 "N tool calls" 和"定位到此调用"要能展开折叠。`Timeline.tsx` 已经到 400 行上限，要先把按回合分组的逻辑拆出去
- 相关：`web/src/components/timeline/Timeline.tsx`（`renderTurnGroups`）、`web/src/components/timeline/StepGroup.tsx`、`web/src/components/timeline/steps.ts`

### 思考没有用时
- 现状：时间线里的 reasoning 条目不带 `duration_ms`，所以折叠后的思考行和步骤摘要都显示不了"思考了 Xs"，前端已经按有值时显示做好了
- 为什么没做：要在后端记录每段思考的起止时间，属于后端改动，不在思考行样式那次范围内
- 什么时候做：下次动时间线投影或 `reasoning.delta` 事件时
- 相关：`src/wright/application/workspace/timeline.py`（reasoning 条目）、`web/src/components/cards/Messages.tsx`（`ReasoningRow`）

### 39 个前端单元测试在 main 上就是失败的
- 现状：`npm run test` 有 39 个失败，集中在工具卡片、计划卡片和面板的测试，断言的还是迁移到新布局之前的界面
- 为什么没做：整理前端结构时确认这些失败在 HEAD 上就存在，修它们要逐个判断是测试过时还是代码有问题，超出那次整理的范围
- 什么时候做：下次改这几个组件之前，先把对应测试修好，否则改动是否破坏行为没法靠测试判断
- 相关：`web/src/components/cards/`、`web/src/components/panels/`、`web/src/components/timeline/timeline.test.tsx`

## 设置

### 项目 `.env` 白名单和 settings 分层是两套机制
- 现状：项目 `.env` 能设置哪些变量由 `runtime.py` 的 `_PROJECT_ENV_KEYS` 决定；项目 `settings.json` 能设置哪些键由 `SettingSpec.project_safe` 决定。两张表要靠人工保持一致，`load_env` 也会把项目 `.env` 写进进程环境，影响之后同一进程里的其他项目
- 为什么没做：这次重做设置页只处理了配置项的收口和界面，改 `.env` 加载方式会影响 CLI 启动行为，需要单独评估
- 什么时候做：再给项目 `.env` 加变量时，或者 Web 控制台同时打开多个项目、出现一个项目的 `.env` 影响另一个项目时
- 相关：`src/wright/application/composition/runtime.py`（`load_env`、`_PROJECT_ENV_KEYS`）、`src/wright/domain/policy/settings.py`

### 设置页只能查看生命周期钩子，不能编辑
- 现状：Tools → Lifecycle hooks 能设置 `hooks.config` 路径，并只读列出文件里的钩子
- 为什么没做：钩子会以用户身份执行任意命令，在网页里编辑需要额外的确认和审计设计
- 什么时候做：有人需要频繁调整钩子，或者钩子配置需要按项目区分时
- 相关：`src/wright/application/composition/lifecycle_loader.py`（`read_hook_config`）、`web/src/components/panels/settings/ToolsSection.tsx`
