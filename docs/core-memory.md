# Core Memory

Core Memory 是每次主模型请求都会重新读取的少量常驻背景。它不是语义记忆，也不会从 episode 或语义记忆自动提升。这里的内容是可修正的长期背景，不能压过用户当前这一次的明确指令。`persona` 不能通过工具修改，这只是写入限制，不是完整的提示注入防护。

## 归属

| 部分 | 含义 | 存放 |
| --- | --- | --- |
| `persona` | 固定的助手角色。不写入未经确认的用户职业、操作系统或具体项目身份。 | 全局文件，工具不能改 |
| `human_profile` | 当前用户跨项目的信息和偏好。 | 全局文件 |
| `project_anchor` | 当前项目的少量长期约束。 | `core/projects/<project_id>.json` |

项目身份使用 Session 的稳定 `project_root`，再经已有的 `project_id(project_root)`。不从 shell cwd、工作树临时路径、任务正文或模型返回的名称推断。同一个 `project_root` 的工作树沿用这条身份。没有进程级“当前项目”变量；每个 `MemoryManager` 只记住自己绑定的根。

没有项目上下文时，可以读取 `persona` 和全局 `human_profile`，不注入任何项目 anchor。此时更新 `project_anchor` 返回“缺少项目上下文”，不会退回一条全局 anchor。

## 调用链

读取和更新：

`get_core_memory` / `update_core_memory`
→ `MemoryService`
→ `CoreMemoryPolicy`
→ `ICoreMemoryStore`
→ `FileCoreMemoryStore`

工具只调用 `MemoryService`。项目 id 由 Manager 在调用时提供，工具不接受模型指定的文件路径，也不读取 service 内部的 store。

请求投影：

Agent 每次主模型调用
→ `MemoryManager.project_system_prompt`
→ `MemoryService.get_core_memory(当前 project_id)`
→ 应用层把全局记录和当前项目 anchor 合成视图
→ `project_core_memory` 替换请求副本里旧的 `<CORE_MEMORY>` 前缀

原始 transcript 不改。每次请求最多一份有效 `<CORE_MEMORY>`。Core Memory 不经过 semantic selector，也不做回合级缓存。更新后，同一次会话的下一次模型调用会读到新值。

合成规则：

- 项目 A：`persona` + 全局 `human_profile` + A 的 anchor。
- 项目 B：同一 `persona` 和全局 `human_profile` + B 的 anchor。
- A 的 anchor 不会出现在 B 的请求里。
- 重新绑定项目后，已构造的工具和提示使用新的 `project_id`。旧项目的 anchor 不会继续被写入。

项目 anchor 读取失败时，不回退到其他项目或旧的全局 anchor。仍能读到的全局内容会保留，并记录失败类型。全局文件本身读失败时，这次请求可以不带 Core Memory，主任务继续。

## 存储布局

默认目录是 `~/.wright/memory`，可用 `WRIGHT_HOME` 或 `WRIGHT_MEMORY_DIR` 改写。

- 全局：`core_memory.json`
- 项目：`core/projects/<project_id>.json`
- 锁：全局 `.core_memory.lock`，项目 `core/projects/.<project_id>.lock`

锁文件在替换 JSON 之后仍然有效，释放锁时不删除。进程内锁和跨进程锁盖住同一次读、校验、修改和保存。保存使用同目录临时文件、`fsync` 和 `os.replace`。失败的写入留下原来的文件。

更新只写所属作用域。改 `human_profile` 不写项目文件。改 `project_anchor` 不改写全局内容。两个作用域各自原子，不宣称跨文件事务；现有工具一次只更新一个 section。

全局更新即使回调试图改掉 `persona`，保存时也会恢复加载到的角色设定。全局文件里如果还有旧的 `project_anchor` 字段，读取时忽略，下次写入 profile 时也不会保留。

## 默认值、空值和清空

新安装的 `human_profile` 为空，新项目的 `project_anchor` 为空。不会自动写入职业、macOS、Wright、uv、pytest 或 DDD 项目事实。`persona` 仍使用固定的通用角色设定。

空的 profile 或 anchor 不渲染成对应行。`append` 和 `replace` 拒绝空白内容。清空必须使用 `mode=clear`。`persona` 不能通过这个路径修改或清空。清空后的文字不会在下一次读取时重新出现。

最终长度限制同时检查追加后的整段，上限 1500 个字符。并发 `replace` 在锁内整段替换，后写入的完整值保留，不合并两段，也不保留历史版本。

## 旧文件

不兼容把三个字段放在一起的旧 `core_memory.json`。全局文件里的 `project_anchor` 不会被读取、导入或继续保存。`persona` 和 `human_profile` 这两个字段名如果还在，会按现在的全局记录读取。

## 已知限制

- 不把语义记忆或 episode 自动提升为 Core Memory，也不调用模型来猜测画像或初始化 anchor。
- 不提供跨文件事务或历史版本。并发 `replace` 是后写覆盖。
- 直接打开记忆目录的人不受应用层归属检查约束。
- `project_id` 必须符合现有的 `PROJECT_ID_RE`，与语义记忆和 episode 相同。
