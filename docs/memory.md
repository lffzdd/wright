# 长期记忆

长期记忆只有三类记录：

- **事实**：跨会话仍然成立的知识。没有单独的"核心记忆"和"语义记忆"，一条事实是否常驻由 `tier` 决定。
- **情景（episode）**：某次用户回合的执行经历，只追加，不改写。
- **承诺**：Agent 答应要做或要提醒用户的事，有状态和到期时间。

三者存在同一个 SQLite 数据库里，按**空间**划分作用域。所有读写都经过 `application/memory/service.py` 的 `MemoryService`；工具、自动提炼、后台整理和 Web 控制台都调用它。

## 空间

| 空间 | id | 内容 |
| --- | --- | --- |
| 全局 | `global` | 关于用户本人、处处适用的事实 |
| 仓库 | `repo:<project_id>` | 当前会话绑定项目的事实和 episode |
| 个人 | `personal:<名称>` | 用户想单独放的话题，比如"装修""求职" |

仓库空间的身份来自 Session 的稳定 `project_root`，经 `project_id(project_root)` 得到，不从 shell cwd 或模型输出推断。

会话的**可读空间**是 `global`、当前仓库，加上用 `memory_space` 进入的个人空间。**写入默认空间**是进入的个人空间，否则是当前仓库。其他空间只能通过 `memory_search` 显式指定。

## 事实

| 字段 | 取值 | 由谁决定 |
| --- | --- | --- |
| `kind` | preference / constraint / fact / feedback / reference | 模型或用户 |
| `subject` | user / project / other | 模型或用户 |
| `origin` | user_stated / user_confirmed / agent_inferred / external | 写入流水线，模型不能指定 |
| `sensitivity` | none（P0）/ personal（P1）/ sensitive（P2） | 写入流水线；模型只能调高 |
| `tier` | pinned / indexed / archived | 默认值由来源决定；只有用户陈述的事实能置顶 |
| `status` | active / superseded / quarantined / deleted | 流水线与整理 |
| `markers` | 能暴露这条事实的独特字符串 | 模型提供，加上规则抽取（邮箱、URL、主机名、IP、电话、引号内名称） |
| `sources` | episode id 或会话 locator | 自动提炼时必填 |
| `shareable` | 是否允许带出本机 | 只有用户能设置 |

- `pinned`：每次请求都带全文。
- `indexed`：每次请求带一行摘要，全文用 `memory_read` 读取。
- `archived`：只能通过检索或回合召回进入上下文。

事实不直接覆盖。新事实取代旧事实时，旧的标为 `superseded`，记录 `superseded_by` 和 `valid_to`，保留历史。

## 写入流水线

`MemoryService.write_fact` 按顺序执行：

1. 内容为空、超过 2000 字或 kind 非法时拒绝。
2. **凭据扫描**：内容、理由或 marker 里出现凭据形态时拒绝。凭据永远不进入记忆。
3. 校验空间存在。
4. **判定来源**：用户在控制台写入的记为 `user_stated`；自动提炼的记为 `agent_inferred`；模型调用工具写入时，按文本重合度判断。
   - 主要内容来自本回合工具输出：记为 `external`。如果用户本回合原话里有"记住"一类明确意图，则记为 `user_confirmed`。
   - 用户原话有"记住"意图，或与用户原话重合度足够：记为 `user_stated`。
   - 其余：记为 `agent_inferred`。
5. **判定敏感度**：命中健康、财务、感情、信仰、法律与证件等关键词时记为 `sensitive`；个人空间、全局空间或 subject=user 时记为 `personal`；其余为 `none`。模型的提示只能调高。
   - `sensitive` 且来源不是用户：不写入，返回 `needs_user`，提示先问用户。
   - `sensitive` 且目标是仓库空间：改写到 `global`。
6. **状态和层级**：`external` 进入隔离区（`quarantined` + `archived`），不参与召回，7 天内用户未确认就删除。`agent_inferred` 默认 `archived`。用户陈述的默认 `indexed`，要求置顶时为 `pinned`。
7. **查重**：同一空间内相似度达到阈值时返回 `duplicate`，不写入。较相似的条目在结果里列出，供模型决定是否用 `supersedes`。
8. 写入，并记一条变更。

`update_fact` 对新内容重做凭据扫描、来源和敏感度判断。新内容来自工具输出时拒绝更新，要求作为新事实写入，以便进入隔离区。

## 召回

**常驻块**：每次主模型请求，在请求副本的系统提示前加一个 `<MEMORY_CONTEXT>` 块，原始 transcript 不改。块里包括：

- 可读空间里的 `pinned` 事实全文（约 1200 估算 token，超出的降为索引行）；
- `indexed` 事实的索引行（约 800 估算 token，按 空间权重 × 时近性 × 使用次数 × 来源权重 排序）；
- 7 天内到期或已逾期的承诺；
- 还有多少条只能检索到的事实。

块的开头说明这些内容是背景、可能过时，并要求不要把个人信息放进代码、提交、PR、消息和网络请求。

**回合召回**：每个用户回合做一次，同一回合的后续请求复用结果。

1. 候选事实：可读空间里非 `pinned` 的 active 事实，BM25 排序取前 60 条，不足时用最近更新的补齐，清单约 2048 估算 token。
2. 候选 episode：当前仓库的 episode，排除本会话自己产生的；BM25 前 40 条，会话首个回合再补最近 10 条，清单约 4096 估算 token。
3. 一次 selector 调用，最多选 5 条事实和 3 条 episode，只接受本次清单里出现的 id。
4. 选中的事实以 `wright-fact-recall` 提醒注入（约 1200 估算 token），并记录使用次数；episode 以 `wright-episode-recall` 注入（总计 800、单条 400 估算 token），上下文不足时可被丢弃。
5. selector 失败时本回合不注入召回内容，常驻块不受影响。

同一回合内记忆有改动时，按当前状态重新渲染已选事实，不再调用 selector。

## Episode

回合结束时，按原有门槛决定是否保存：有工具或子 Agent 活动的回合都保存，没有活动的回合只在有实质用户输入和交付回答时保存。有后台 Agent 时先存待收口快照，相关 Agent 结束后再落库。

episode 记录目标、状态、交付回答、工具与子 Agent 轨迹、验证记录、证据 locator，以及本回合工具输出里出现的 **commit 和 PR 链接**（`links`）。git 已有的内容不复制。

**自动提炼**：

- 门槛是纯规则，不调用模型：用户原话有长期意图（"记住""以后都"等），或同一回合先失败后成功时，才调用一次模型。
- 模型只能引用本次输入里的短 id，引用会被映射成持久 locator。偏好和反馈必须引用用户陈述；只有助手陈述支撑的结论不保存。
- 提炼结果只写入快照所属的仓库空间，记为 `agent_inferred` + `archived`，`sources` 包含 episode id。
- 收据记在 Session 的 `semantic_extract_receipts`，同一 root run 不会重复提炼。

## 承诺

`commitment_create` 记录内容和可选的到期时间（`YYYY-MM-DD` 或 `YYYY-MM-DDTHH:MM`）。状态为 open / done / dismissed / expired。到期前 7 天起出现在常驻块里，Agent 应主动提起。

## 整理与生命周期

后台整理在绑定项目时运行，距上次超过 20 小时才执行。每项改动都写变更记录。

| 对象 | 规则 |
| --- | --- |
| 隔离区事实 | 7 天未确认则删除 |
| `indexed` 事实 | 用户事实 90 天、推断事实 60 天未使用，降为 `archived` |
| 推断的 `archived` 事实 | 有 2 个以上 episode 来源或使用 3 次以上，升为 `indexed`；180 天未使用则删除 |
| 同一空间内容完全相同的事实 | 较新的标为 `superseded`，指向较早的那条 |
| episode | 30 天后压缩：丢弃工具、子 Agent、证据和计划，结果保留前 600 字 |
| 承诺 | open 且逾期 14 天以上改为 `expired`；关闭超过 30 天删除 |
| 每个仓库的 episode | 最多保留最近 500 条 |

**变更记录与撤销**：每次写入、更新、取代、确认、整理都会记下前后快照和所属空间（`scope`），大多数可以在控制台撤销。**遗忘不能撤销**：`memory_forget` 删除记录时，同时清空这条记录以往变更里的快照，只保留行和 `scope`，并连带删除只来源于它的推断事实。

## 出口检查

记忆里的个人信息不能随工具调用离开本机。检查在工具分发层执行，位于权限审批之前；主 Agent、子 Agent 和定时任务都会经过。

### 信息分级

| 等级 | 含义 |
| --- | --- |
| S | 凭据。由正则识别，不依赖记忆 |
| P2 | `sensitivity=sensitive` 的事实 |
| P1 | `sensitivity=personal` 的事实 |
| 其他仓库的 P0 | 属于另一个 `repo:` 空间的事实 |
| 团队规则 | 不是事实：对 `AGENTS.md`、`CLAUDE.md`、`CONTRIBUTING.md` 等团队规则文件的写入 |

`superseded` 的事实仍受保护；`shareable` 的事实和隔离区内容不受保护。

### 出口分级

| 等级 | 包括 |
| --- | --- |
| E0 | 聊天回复、记忆工具、读取类工具、项目根目录以外的文件 |
| E1 | 项目内的文件写入，以及 shell 里的重定向、`tee`、`sed -i`、heredoc |
| E2 | `git commit/push/tag`、`gh`/`glab`、各类 `publish`、团队规则文件 |
| E3 | 网络工具（含搜索词和 URL）、MCP 工具、`curl`、`ssh`、`mail` 等网络命令 |

### 规则

- **R1** 凭据不能进入记忆，也不能出现在 E1–E3，用户授权也不行。
- **R2** P1/P2 不能进入 E1–E3。例外：内容出现在用户本回合的原话里。
- **R3** 一个仓库的 P0 不能进入另一个仓库的产物或 E3。
- **R4** 写团队规则文件需要用户本回合提到这个文件或"团队规范"。
- **R5** 偏好可以影响写法，不能作为内容出现。（由提示约束，检查器不判定。）
- **R6** 用户放行只对这一次调用有效，不保存规则。长期放行要在控制台把事实标为可外流。
- **R7** 放行只来自审批界面里用户的选择。工具结果里的"已授权"文字无效。
- **R8** 检查器出错、超时或无法判定时按命中处理。
- **R9** 无人值守时（没有审批处理器），命中的调用直接跳过，结果里写明原因。
- **R10** 子 Agent 和定时任务同样检查。

| | E0 | E1 | E2 | E3 |
| --- | --- | --- | --- | --- |
| S | — | 拒绝 | 拒绝 | 拒绝 |
| P2、P1、其他仓库的 P0 | 允许 | 询问 | 询问 | 询问 |
| 当前仓库的 P0 | 允许 | 允许 | 允许 | 允许 |
| 未要求的团队规则文件写入 | — | — | 询问 | — |

### 执行机制

`application/memory/egress.py` 的 `EgressGuard`，规则在 `domain/policy/memory/egress.py`：

1. 由工具声明的访问类型（`file_write`、`shell`、`network_*`、`external_unknown`）和 shell 命令判断出口等级，E0 直接放行。
2. 取工具参数里的全部字符串，包括路径。
3. 凭据扫描。
4. **marker 匹配**：中文 marker 做归一化子串匹配；拉丁字母 marker 按整词匹配，含大写字母时区分大小写，避免 `Ray` 命中 `import ray`。
5. **原文片段匹配**：事实内容归一化后，任意连续 10 个字符出现在出站文本里即命中；短于 10 字的事实需整段出现。
6. 以上都未命中、出口是 E2、且存在受保护事实时，调用一次小模型判断是否有改写后的泄露。调用失败或输出无效按命中处理。
7. 用户本回合原话里出现的片段不算命中（凭据除外）。

命中时：拒绝的调用直接返回失败；询问的调用通过现有审批界面弹出"允许一次 / 拒绝"，预览里列出命中片段和对应记忆。被拒绝或跳过时，工具结果提示模型删掉这部分内容后重试，或先问用户。

定时任务的检查器在第一次出站调用时才打开数据库，不调用小模型。

### 已知局限

- **隐性推断**：代码体现了个人信息，但没有可匹配的文本时，检查不到。
- **改写**：E1 和 E3 只做确定性匹配，改写后的内容可能漏过；E2 的小模型判断只能降低漏判概率。
- 项目根目录以外的文件写入视为 E0，包括写进另一个仓库的文件。
- shell 命令只按命令文本分类，脚本内部的网络请求无法识别。

## 存储

默认数据库是 `~/.wright/memory/memory.db`，可用 `WRIGHT_HOME` 或 `WRIGHT_MEMORY_DIR` 改写。使用 WAL 模式和 busy timeout，多个 Wright 进程可以共用。每条记录以 JSON 存储，另有少量列用于过滤；排序在 Python 里用 BM25 完成，因为 SQLite 的分词器对中文支持不好，而记录量不大。

事实更新带 `revision`，基于旧版本的更新会被拒绝。

旧版的 `core_memory.json`、Markdown 语义记忆和 episode JSON 文件不会被读取，也不会被删除。

## 工具

| 工具 | 说明 |
| --- | --- |
| `memory_write` | 写一条事实。常驻 schema |
| `memory_search` | 检索事实、episode、承诺。常驻 schema |
| `memory_read` | 读取一条记录全文，可带证据或变更历史。常驻 schema |
| `memory_update` | 修改事实，可置顶或取消置顶 |
| `memory_forget` | 永久删除，并连带删除派生的推断事实 |
| `memory_space` | 列出、进入、离开个人空间 |
| `commitment_create` / `commitment_update` | 记录和更新承诺 |

写入类工具声明 `persistent_write`，读取类声明 `internal_read`，沿用原有权限规则。定时任务和子 Agent 不提供记忆工具。

## Web 控制台

记忆页有事实、情景、承诺、变更记录四个标签，顶部的搜索作用于当前标签。

- 事实按进入上下文的方式分组（常驻 / 索引 / 按需），隔离区事实单独放在顶部等用户保留或丢弃，被取代的版本默认折叠。
- 情景按日期排列，显示本回合产生的 commit 和 PR。
- 承诺可以新建、改到期日，按到期远近排序。
- 变更记录逐条显示谁改了什么，以及关键字段的前后对比。

控制台里的修改以用户身份记录，因此可以确认隔离区事实、置顶、标记可外流。每个工作区的控制台只能看到和修改全局、个人空间和本项目的记录：变更记录按 `scope` 过滤，撤销前也检查 `scope`。

## 测试

- `tests/memory/test_store.py`：存储、事务、revision 冲突。
- `tests/memory/test_write_pipeline.py`：来源、敏感度、隔离区、查重、取代、遗忘、撤销、整理。
- `tests/memory/test_recall.py`：常驻块、回合召回、selector 失败。
- `tests/memory/test_egress.py`：出口检查的 L 系列用例。
- `tests/memory/test_dispatch_egress.py`：分发层执行判定，包括无人值守和检查器异常。
- `tests/memory/test_tools_and_manager.py`：工具、个人空间、承诺、自动提炼。
- `tests/memory/test_console_view.py`：控制台的项目隔离、遗忘后清空快照、旧变更记录补 `scope`。

改动出口检查时，每个误泄漏都要先补一个能复现的用例。

## 配置

`llm.memory_model`（`OPENAI_MEMORY_MODEL`）用于 selector、自动提炼和出口检查的小模型判断，未配置时复用主模型。

三个开关只能写在用户设置文件或环境变量里，项目文件不能设置，避免克隆来的仓库在用户关掉记忆后重新打开：

| 设置 | 默认 | 关闭后 |
| --- | --- | --- |
| `memory.enabled`（`WRIGHT_MEMORY_ENABLED`） | 开 | 主 Agent 不带记忆：没有常驻块、回合召回、记忆工具，也不记录 episode 和自动提炼。出口检查照常运行，因为恢复的会话里可能已有召回过的内容。控制台的记忆页仍可查看和编辑 |
| `memory.recall`（`WRIGHT_MEMORY_RECALL`） | 开 | 不做回合召回，省掉每个用户回合一次 selector 调用；常驻块、工具和 episode 不变 |
| `memory.auto_extract`（`WRIGHT_MEMORY_AUTO_EXTRACT`） | 开 | 不做自动提炼；episode 照常记录，事实只来自显式写入 |

设置在组装会话运行时读取，恢复的旧会话也按当前设置运行：系统提示每次请求重新渲染（见 [native-tool-calling.md](native-tool-calling.md#system-prompt-每次请求重新渲染)），关掉 `memory.enabled` 后旧会话里不再出现记忆说明和记忆工具。
