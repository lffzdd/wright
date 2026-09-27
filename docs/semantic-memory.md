# Semantic Memory

Semantic Memory 是带元数据的 Markdown 文档集合，用来保存可以跨回合复用的说明。一条记忆对应一个明确主题，正文可以是自然语言，包括理由和适用条件。它不是事实数据库，也不做 subject/predicate/object 拆分。

继续保存的内容：

- 用户信息和长期偏好。
- 用户反馈。
- 项目约束、决定，以及无法轻易从当前代码恢复的背景。
- 外部参考资料的位置和用途。

不在这里做 embedding、向量库、SQL、知识图谱、实体归一化、批量反思、定时摘要、完整历史版本，或自动判断自然语言是否矛盾。也不会把语义记忆自动提升进 Core Memory。

默认目录是 `~/.wright/memory`，可被 `WRIGHT_HOME` 或 `WRIGHT_MEMORY_DIR` 改写。每条记忆是一个 Markdown 文件。`MEMORY.md` 只是给人看的全库导航，不是 Agent 召回的真源。

## type 和 scope

`type` 说明这条笔记在说什么。`scope` 说明它适用于哪里。两者不能互相代替。

`type`：

- `user`：用户信息或偏好。
- `feedback`：用户反馈。
- `project`：项目约束、决定或背景。
- `reference`：外部资料的位置和用途。

`scope`：

- `global`：所有项目都可以自动读取。
- `project`：只属于一个项目，必须带 `project_id`。

`type=user` 不表示全局。一条用户偏好可以只适用于当前项目。`type=project` 也只是分类，不表示它已经绑定了某个项目。

`project_id` 来自 Session 的稳定 `project_root`，使用已有的 `project_id(project_root)`。不从 shell cwd、标题、正文或模型返回的项目名推断。

## 默认读取和写入

有当前项目时，自动召回、selector 清单和注入索引只包含：

- `status=active` 且 `scope=global` 的记录。
- `status=active` 且属于当前 `project_id` 的记录。

没有当前项目时，只读取 active global。`inactive` 不进入自动召回。

过滤发生在排序和候选上限之前。其他项目的大量记录不会先占满名额，再把当前项目挤掉。候选上限是 200 条。selector 输入清单另有 2048 个估算 token 的上限，超出的记录不会被送进 selector，也不能靠返回 ID 重新进入。注入正文和索引合计 1200 个估算 token，最多选 5 条正文。预算不够时，上下文里给出带范围的 `search_memory` / `get_memory` 路径，不把整库正文塞进去。

新写入默认是当前项目。全局写入必须显式选择 `scope=global`，依据是用户明确表达或工具参数，不是每次弹确认。没有项目上下文时，项目写入返回“缺少项目上下文”，不会退回 global。

自动提炼固定写入快照所属项目。模型不能把 `scope` 填成 global 来扩大范围。可读到的全局记忆只作为只读清单，不在可更新 ID 里。后台旧快照使用快照自己的 `project_id`，不使用 Manager 此刻绑定的新项目。

显式读取可以换范围：

- `applicable`：全局加上当前项目。这是 get、update、delete、search 的默认范围。
- `current_project`：只看当前项目。
- `global`：只看全局。
- `all_projects`：所有带项目的记录，不含全局。

知道另一项目的 ID 不能绕过默认范围。跨项目读取或修改必须带上对应 scope。读过不等于可以修改。这是应用层的归属边界，不能阻止直接打开文件的人。

同名不是冲突。两个项目可以各有一条同名记忆。同一项目里新建同名记录也不会覆盖旧文件。项目约定可以作为当前项目更具体的上下文，但系统不会把任意同名记录当成覆盖关系。

## 身份和操作

记录 ID 是 `mem-` 加 16 位十六进制，文件名必须是 `{id}.md`。改标题不改 ID。文件名不会被当成缺失 ID 的替代。

| 操作 | 含义 |
| --- | --- |
| create | 新建。不隐式变成 update。同名不覆盖。 |
| update | 必须提供稳定 ID 和读到的 `expected_revision`。保持 `created_at`，更新 `updated_at`。未知 ID 不会变成新建。 |
| deactivate | `update` 把 `status` 设为 `inactive`。文件还在。 |
| reactivate | `update` 把 `status` 设为 `active`。普通正文修改不会顺便恢复。 |
| delete | 删除文件。这是明确遗忘，和停用不同。 |
| search | 只在请求的范围内搜索。没有命中就返回空，不退回“最新几条”。 |
| get | 范围内可以读到 inactive。范围外返回说明，不说成文件不存在。 |

显式工具和自动提炼都经过 `MemoryService`，再进入同一套策略和 Markdown store。工具只解析参数、说明权限、调用服务、生成 `ToolResult`。

自动提炼必须带上这次输入里实际出现、并且校验通过的来源。用户明确要求保存时，`origin` 记为 `explicit`，不伪造工具观察，也不标成已验证。

正文变化且没有同时给出新来源时，旧来源会被清掉。只改状态或标题时保留原来源。

同一回合里，显式更新、停用或删除会增加服务上的 generation。后续模型步骤复用已选 ID，按当前适用记录重新投影，不再调用一次 selector。已停用或不在范围内的 ID 会从注入里消失。`bind_project` 清掉旧项目的召回缓存。

selector 成功、失败或没有 selector 时，注入的索引都只来自这次过滤后的记录。过期或被改过的 `MEMORY.md` 不会被当作上下文。提示也不再让模型直接去读未过滤的全库索引来补漏。

## 来源

自动提炼时，模型继续引用本次输入里的短 ID，例如 `ev-u-msg_1`。应用层把它映射成持久 locator。模型不能自己编 Session 或 Run 路径。

落盘后的引用是一行可解析字段：

`sess=<session>;root=<root run>;run=<run>;kind=<kind>;msg=<message>;tool=<tool call>;step=<step>;ep=<episode>;local=<短 ID>`

空字段省略。它能确定 Session、root run、来源类型，以及消息、工具调用或验证步骤，可选 episode。两个 Session 里都有 `msg_1` 时，仍按 Session 和 run 区分。

输入先按完整来源条目装进 8000 字符预算，再装可更新的记忆行。一条过长的来源可以被截断并标 `…(已截断)`，ID 仍算本次输入。装不进去的来源整条剔除，模型引用它会被拒绝。伪造、越界或不在本次输入里的 ID 同样拒绝。

`get_memory` 默认只显示记忆和 locator，不加载 Session。`include_evidence=true` 时，按已有的只读证据解析去读对应记录。原始 Session 或 Episode 不存在时，记忆仍可读取，来源状态是 `unavailable`。不会到别的会话里模糊匹配同一个局部 ID。没有 session 的引用不会写入。

来源可追溯不等于正文为真。用户陈述、工具观察和助手推断仍然分开。只有助手陈述支撑的结论不保存。引用存在不会把整段正文标成已验证事实。

## 不读取旧版文件

`schema_version` 不是 1，或缺少 `id`、`scope`、`status`、`revision` 和时间的 Markdown 不是当前语义记忆。列表、检索、召回和索引都会跳过。按文件名读取会返回格式错误，不会归类，也不会改写成 global 或当前项目。文件留在目录里，这个功能不删除它们，也不提供迁移。

## 并发

写入在目录锁 `.semantic.lock` 上使用 `fcntl` 排他锁，不只靠进程内的线程锁。文件用临时文件写入后 `os.replace`，失败时删掉临时文件，不留半截正文。

更新必须带读到的 `revision`。锁内发现 revision 已变时返回冲突，当前 revision 写在错误里，不自动重试覆盖。调用方重新读取后再决定。停用之后，基于旧 revision 的更新不能把记录悄悄改回 active。

`revision` 只是并发检查用的整数，从新记录的 1 和旧记录的 0 开始递增。不保存完整历史。

## 调用链

显式工具：

`create_memory` / `get_memory` / `update_memory` / `delete_memory` / `search_memory`
→ `MemoryService`
→ scope 策略 + `ISemanticMemoryStore`
→ Markdown 文件，并在同一把锁里重写给人看的 `MEMORY.md`

自动提炼：

快照的 `project_id` → 按完整来源条目装包 → 只校验包内 ID
→ 短 ID 映射成 locator
→ `MemoryService.record_extracted`
→ 同一个 store，scope 固定为该项目

自动召回：

`MemoryManager.recall_for_turn`
→ `MemoryService.prepare_memory_context`
→ 先按 scope 和 active 过滤，再截断候选
→ 过滤后的索引和清单进入同一次 selector
→ 只接受本次提供的 ID
→ 在预算内投影索引和正文

selector 失败时仍投影当前范围的索引，不注入其他项目，也不读取原始 `MEMORY.md`。

## 已知限制

- 不判断两条自然语言记忆是否矛盾。同名记录各自保留。
- 自动提炼不会写入全局。全局记忆靠显式工具。
- 直接访问记忆目录的人不受应用层范围检查约束。
- 旧版语义记忆文件不会被读取、归类或迁移。
- `MEMORY.md` 可能落后于某次失败的索引重写；Agent 上下文不依赖它。
- 不保存每次修改前的正文，冲突后只能读到当前版本。
- Core Memory 仍是单独的常驻背景，不会从语义记忆自动提升。作用域见 `docs/core-memory.md`。
