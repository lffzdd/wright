# Skills 与知识检索

Agent 有两样按需能力：磁盘上的领域流程（Skill），以及可选的只读检索
（`knowledge_search`）。Skill 不是系统指令，正文不写进冻结的 system prompt。

下面几件事不是同一个状态：

| 问题 | 所有者 |
|---|---|
| 工具是否已注册、是否允许执行 | 组合根注册的工具 + `PermissionResolver` |
| 工具 schema 当前是否向模型暴露 | 当轮 `encode_tools` 快照 |
| MCP 是否已连接、工具是否已发现 | `McpManager.start()`（启动时完成） |
| 技能正文是否留在历史里 | transcript 里的 `load_skill` 结果 |
| 技能正文是否在当前模型请求里 | `ContextBuilder` 投影；`retention=instruction` 不被折叠 |

`defer_to_model` 只推迟 **schema 暴露**。它不推迟注册、权限或 MCP 连接。
MCP 工具在 `McpManager.start()` 时已经连接并发现；随后才用
`defer_to_model=True` 把 schema 留到 `tool_search` 激活之后。

## 调用链

```text
磁盘 SKILL.md
  storage/skills.py          解析、路径校验、单技能故障隔离
  SkillRegistry              缓存、指纹失效、项目目录优先、扫描诊断打日志
        │
根 Agent 组合
  resident_skill_tools       注册 load_skill（不 defer）
  tools_for_role             子 Agent / durable run 去掉 load_skill
        │
每一轮模型请求
  AgentPromptManager         有技能才暴露 load_skill，并附上当前目录
  encode_tools               本轮 schema / 名称快照
  ContextBuilder             丢掉历史里的旧目录消息，保留指令类工具结果
        │
load_skill
  读当前文件，把正文快照写入 tool_result
  结果带 retention=instruction 与 retention_key
  相对路径以 skill_root 为基准；不预读资源，不执行脚本
```

`tool_search` 只发现被 `defer_to_model` 标出的工具。技能不走这条路：
目录直接给出 id 和用途，`load_skill` 在有技能时属于当轮 schema。

## 目录契约：可更新的请求投影

目录不写入 transcript，也不进 checkpoint。每一轮从 `SkillRegistry` 现读，
作为 ephemeral user 消息放进这次请求。因此：

- 启动、恢复、继续运行看到的都是当时磁盘上的目录；
- 技能文件增删或描述更新后，下一次模型请求换成新目录；
- 不会每轮往历史里再追加一份目录；
- 旧 checkpoint 里已经写入的 `<skill-catalog>` 消息会从请求投影中省略，
  避免和当前目录叠成两份；
- 没有技能时不注入空目录，也不把 `load_skill` 放进当轮 schema。
  根 Agent 仍注册这个加载器，所以会话中途出现第一个技能后，下一轮就能看见它；
- 超 2500 字符时只截描述，不丢掉 id。

目录不写入 checkpoint。

已加载正文是调用当时的快照，存在那条 tool result 里。目录更新不会改写
已经加载的正文；文件变了要再调一次 `load_skill`。

## 正文保留

`load_skill` 的结果带 `retention=instruction` 和 `retention_key=skill:<id>`。
压缩器只认这个标记，不按工具名开特例。

- 同一个 key 只保留最新一份完整正文；更早的副本可以标成 superseded。
- 最新正文不会被换成 “folded” 占位。
- 折叠只发生在请求投影上，不改 transcript，也不清空
  `active_deferred_tools`。
- 把可折叠结果都收掉之后仍然超过预算时，请求失败，并说明必需的指令内容
  放不进预算、没有被占位符替换。不会假装正文还在上下文里。
- 重复加载产生新的 tool result。恢复会话时，正文随 transcript 回来，
  目录按当前磁盘重建，已激活的延迟工具按 checkpoint 恢复后再按当前工具过滤。

Checkpoint 不另存一份技能正文。没有 `retention` 的工具结果按普通结果折叠，
不会补成技能指令。

## 格式

解析使用只接受安全类型的 YAML，并拒绝重复键。

| 字段 | 行为 |
|---|---|
| `name` | 必填，必须与目录名相同：1–64 字符，小写字母、数字和单个连字符 |
| `description` | 必填，最多 1024 字符，进入目录 |
| `allowed-tools` | 唯一的建议字段。空格分隔字符串或 YAML 字符串序列，归一成同一个内部列表 |
| `license` / `compatibility` / `metadata` | 只作为元数据返回，不参与权限或工具暴露。`metadata` 可以嵌套 |
| 其他未知键 | 保留为 extra 元数据，没有行为 |

`allowed-tools` 不会绕过 `PermissionResolver`，也不会增删工具清单。
正文里的 `scripts/`、`references/`、`assets/` 相对 `skill_root`（SKILL.md
所在目录的绝对路径）。模型用现有文件或命令工具按需读取；加载器不预读、
不执行脚本。

资源上限：文件 256KB，正文 100000 字符，描述 1024 字符。超出时错误里带
当前长度和上限。一个技能编码错误、YAML 损坏、读失败或扫描时消失，只变成
该技能的诊断并写入日志；其余技能继续可用。

项目目录 `{project}/.wright/skills/` 优先于 `~/.wright/skills/`。同名技能
加载到的是胜出那一份自己的 `skill_root`。

## 所有权

| 状态 | 所有者 |
|---|---|
| 文件、解析、扫描错误 | `infrastructure/storage/skills.py` |
| 发现、优先级、缓存失效 | `SkillRegistry` |
| 目录是否出现在本轮请求 | `AgentPromptManager` + 当轮 schema |
| 已加载正文 | transcript 中的 tool result |
| 请求里是否仍有全文 | `ContextBuilder` / `ContextCompactor` |
| 已激活的延迟工具 | `Session.active_deferred_tools` |
| 计划 | `Session.plan_manager` |
| 跨会话事实 | Memory |

## knowledge_search

默认关闭。未设置 `WRIGHT_KNOWLEDGE_ENABLED=1` 时，工具根本不进工具集，避免
每个新会话都被一个不可用的检索工具占位。

启用后走 `KnowledgeProvider` 协议。Wright 的类型是 `KnowledgeHit`，不
依赖 RAG 的 `SearchResult`。`RagKnowledgeProvider` 在第一次 `search()` 时才
把 RAG 目录插入 `sys.path`、导入 `RAGChain` 并 `load_index()`。导入
Wright 不会触发 RAG 导入、模型加载或网络请求。

初始化失败（缺索引、缺 `SILICONFLOW_API_KEY`、RAG 导入失败）返回
`ToolResult.fail`，说明缺什么、怎么补；同一 provider 实例会缓存失败原因，
不再反复重初始化。查询期的瞬时网络错误不缓存。

权限声明 `accesses_network`，与 `http_request` 一样走 `ask`，避免默认放行。

环境变量：

| 变量 | 含义 | 默认 |
|---|---|---|
| `WRIGHT_KNOWLEDGE_ENABLED` | `1`/`true`/`yes`/`on` 才把工具加入工具集 | 关闭 |
| `WRIGHT_KNOWLEDGE_INDEX` | 索引文件路径 | 若设置了 `WRIGHT_RAG_DIR` 则为 `$WRIGHT_RAG_DIR/simple_index.json` |
| `WRIGHT_RAG_DIR` | 含 `rag_chain.py` 的外部 RAG 目录 | 无（不导入 RAG） |
| `WRIGHT_KNOWLEDGE_RETRIEVER` | `dense` 或 `hybrid` | `dense` |
| `WRIGHT_KNOWLEDGE_RERANKER` | 是否启用 reranker | 关闭 |
| `SILICONFLOW_API_KEY` | embedding API；缺省时可回退 `LLM_API_KEY` | 无 |

凭据按 `SILICONFLOW_API_KEY` 优先、`LLM_API_KEY` 后备解析；每个名称都先看进程
环境，再只读 `$WRIGHT_RAG_DIR/.env`。显式传给 `RagKnowledgeProvider` 的
`api_key` 会继续注入 `RAGChain`，不会只做存在性检查。

检索结果带来源，正文包在 `<untrusted-knowledge>` 里，并有“未经验证”的警告。
`top_k` 限制 1..10，单条 content 截断 2000 字符，总输出 8000 字符。

普通测试使用真实 `RAGChain` 和 `SimpleVectorStore` 加载临时索引，但替换掉会联网
的 query embedder。需要验证现有索引和真实 embedding API 时显式运行：

```bash
WRIGHT_KNOWLEDGE_LIVE_TEST=1 pytest -q \
  tests/knowledge/test_rag_provider.py \
  -k live_rag_provider
```

该测试默认跳过，避免常规回归静默消耗 API 额度。

## 子 Agent 与 durable run

- **knowledge_search 给子 Agent，不给 durable run。** 它是只读检索，没有跨会话
  副作用，子任务常常需要查资料；但无人值守运行会消耗 embedding 额度、依赖
  网络，且权限是 `ask`——fail-closed 下调用必被拒，放进工具集只会误导模型。
- **`load_skill` 不给子 Agent，也不给 durable run。** 子 Agent 的契约是自包含任务；
  父 Agent 应在 spawn 描述里写清流程。durable run 把步骤写进调度 prompt，避免
  用步数去发现和展开 skill。两者都没有技能目录。

排除名单在 `CHILD_EXCLUDED_TOOLS` 和 `DURABLE_EXCLUDED_TOOLS`，由
`tools_for_role` 执行。漏掉 `load_skill` 会让隔离上下文或无人值守任务拿到
技能加载器。

## 有意没做的事

- **不给模型 `create_skill` / `update_skill`。** Skill 由人维护。让模型自动沉淀
  流程会和 Memory 抢职责：Memory 存“跨会话为真的事实”，Skill 存“完成某类
  任务的做法”。自动写入会把一次性对话习惯写进仓库级流程。
- **`allowed-tools` 只是提示，不动态增删工具集，也不授权。** 文件里的 `allowed_tools` 会拒绝加载。
- **不把 Skills 塞进 Memory。** 召回的是事实，展开的是流程；两者的失效策略、
  注入时机和所有权都不同。
- **不用 `RAGChain.query()`。** 那条路径会再调 LLM 生成答案。这里只要检索。
- **不把 skill 正文复制进 checkpoint 的独立字段。** 正文在 transcript 的
  tool result 里；请求投影从那里保留最新全文。
- **不做按任务检索技能。** 当前规模下，目录给出全部 id；超预算只截描述。
- **不把技能发现塞进 `tool_search`。** 目录和常驻 `load_skill` 是同一条路径。
