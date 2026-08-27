# Skills 与知识检索（第五阶段）

第五阶段给 Agent 两样按需能力：磁盘上的领域流程（Skill），以及隔壁 RAG
项目的只读检索（`knowledge_search`）。两者都不改已经冻结的 system prompt。

## 为什么不能改 system prompt

`Agent` 只在会话第一条消息时构造 system prompt，之后永不重建。工具清单、
记忆静态指令都冻在那里。Skill 是领域流程，不是系统指令：写进 system prompt
会和既有规则抢优先级，恢复会话或子 Agent 也会把过期流程当成“系统规则”。

因此：

- **目录**作为一条 `user` 消息写入 transcript，每个会话只发一次；
- **正文**走 `skill` 工具的 `tool_result`，钉在调用点，和普通工具输出一样留在历史上；
- 都不进 system prompt，也不再每轮往 `wire_messages` 末尾临时贴。

计划提醒（`plan_reminder`）仍是每轮 ephemeral；Skill 已经不是那条路。

## Skill 生命周期

```text
磁盘  {project}/.wright/skills/<id>/SKILL.md
      ~/.wright/skills/<id>/SKILL.md
        │  SkillRegistry 扫描 / 缓存 / 失效（项目目录优先）
        ▼
会话开始  若至少有一个合法 skill
        ├── 工具：skill（写入冻结的工具清单）
        └── 目录：id + description 写入 transcript 一次
            （超 2500 字符只截描述，不丢掉 id）

skill(skill_id)
        │  当时从磁盘读完整正文
        ▼
tool_result  正文进入 transcript，之后随对话历史保留
```

`continue_run` / `run_runtime_event` 不是新会话：目录标记
`SessionState.skill_catalog_sent` 已为真则不再重发。后续 `Agent.run()`
也不清空这份目录——它已经在历史里。

没有 `list_skills` / `load_skill` / `unload_skill`，也没有激活表。
模型看见目录后调用 `skill`；对话里已经有过该正文就直接遵循，不必再调。

## 所有权边界

| 状态 | 所有者 | 不放在 |
|---|---|---|
| skill 文件 / 正文 | 磁盘 + `SkillRegistry`（进程级只读缓存） | Session、checkpoint、system prompt |
| 目录是否已写入 transcript | `SessionState.skill_catalog_sent` | 全局 registry、Memory |
| 计划 | `SessionState.plan_manager` | Skill |
| 跨会话事实 | Memory | Skill |

正文只在被调用时出现在 transcript 的 `tool_result` 里，不另建
`active_skill_ids`。主 Agent 和子 Agent 各有自己的 session，目录标记天然隔离。
子任务不该自己调 `skill`——委派时把需要的步骤写进任务描述，子 Agent 才能保持
“自包含、可独立完成”。

Checkpoint 只存 `skill_catalog_sent`，不存正文。旧 checkpoint 没有该字段、
或仍带着已废弃的 `active_skill_ids` 时，当成尚未发送目录。

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
  src/wright/tests/knowledge/test_rag_provider.py \
  -k live_rag_provider
```

该测试默认跳过，避免常规回归静默消耗 API 额度。

## 子 Agent 与 durable run

- **knowledge_search 给子 Agent，不给 durable run。** 它是只读检索，没有跨会话
  副作用，子任务常常需要查资料；但无人值守运行会消耗 embedding 额度、依赖
  网络，且权限是 `ask`——fail-closed 下调用必被拒，放进工具集只会误导模型。
- **`skill` 工具不给子 Agent，也不给 durable run。** 子 Agent 的契约是自包含任务；
  父 Agent 应在 spawn 描述里写清流程。durable run 把步骤写进调度 prompt，避免
  用步数去发现和展开 skill。

这两处分别写进 `_child_base_tools` 和 `_DURABLE_EXCLUDED_TOOLS`。漏一处就会
让隔离上下文或无人值守任务拿到不该有的能力。

## 有意没做的事

- **不给模型 `create_skill` / `update_skill`。** Skill 由人维护。让模型自动沉淀
  流程会和 Memory 抢职责：Memory 存“跨会话为真的事实”，Skill 存“完成某类
  任务的做法”。自动写入会把一次性对话习惯写进仓库级流程。
- **`allowed_tools` 只是提示，不动态增删工具集。** 模型看见的工具清单在会话
  开始时冻结。运行期改 executor 注册表会造成 prompt 与可执行集合不一致。
- **不把 Skills 塞进 Memory。** 召回的是事实，展开的是流程；两者的失效策略、
  注入时机和所有权都不同。
- **不用 `RAGChain.query()`。** 那条路径会再调 LLM 生成答案。这里只要检索。
- **不在会话中途把新出现的 skill 补进工具清单。** system prompt 已经冻结；
  启动时一个 skill 都没有，则本会话没有 `skill` 工具。新增文件后开新会话即可。
- **不把 skill 正文写入 checkpoint。** 正文只在被调用时出现在 transcript。
- **不做按任务检索的相关便签。** 目录一次性给出全部 id；超预算只截描述。
  清单涨到需要检索时再加，不让模型自己 `list_skills`。
