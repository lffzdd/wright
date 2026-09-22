# Execution / Permission 第二轮修复计划

## 1. 目标与约束

在当前 Luna 已完成的改动上，修复复查确认的六项问题：

1. 审批改写参数后没有重新判断并发安全性。
2. 工具运行时仍暴露原始 ExecutionBackend，可以绕过 AuthorizedExecution。
3. 新目录授权未同步到 Shell 和子 Agent 使用的范围快照。
4. 子 Agent 接收到错误类型的 cwd，继承失败。
5. 子 Agent 的永久授权没有真正保存。
6. grep 改用 Python 搜索后出现字面量、忽略规则等行为回归。

**本轮是修复，不重新设计已确定的权限策略。** 保留四种模式、规则优先级、目录授权的含义，以及本次/会话/永久审批选择。

实施约束：

- 保留当前工作区改动，不回滚第一轮实现。
- 不添加旧接口兼容别名，不引入第二条权限执行路径。
- 不通过扩大授权、默认 bypass、吞掉异常或跳过测试解决问题。
- 不增加 SSH、容器后端或操作系统沙箱。
- 不改动模型可见工具名称、参数 schema、结果结构。
- 每项先增加能复现当前错误的测试，确认失败，再修复并确认通过。
- 集成测试必须经过生产 `ToolExecutor` / `Agent` 调用链。不能只使用 `tool_runtime_for_session()` 的宽授权测试适配器证明权限正确。

当前已确认的基线：Python `510 passed, 2 skipped`，Web `17 passed`，E2E `1 passed`，Ruff 和差异检查通过。测试通过不代表本轮六项问题不存在。

## 2. 执行入口与调度修复

### 2.1 工具只能取得 AuthorizedExecution

**现状：** Executor 创建 `runtime.execution`，但同时保留 `runtime.capabilities.execution` 中的原始后端。后者可以直接操作未授权文件。

按以下方式修复：

- 从工具可见的 `ToolCapabilities` 删除原始 `execution` 字段。
- 原始 backend 由装配层创建，由 ToolExecutor 私有持有，交给 Resolver 做资源解析、交给 AuthorizedExecution 做实际执行。
- 将装配函数返回值改成明确的装配结果，分别包含 `capabilities`、`runtime_resources` 和 `backend`。整个装配结果不能放进 ToolRuntime。
- 工具访问环境的唯一公开入口是 `runtime.execution`。
- 保留 `required_capabilities` 中的 `"execution"` 声明。只有声明该能力、且权限允许的工具，才得到 AuthorizedExecution；其他工具的 `runtime.execution` 为 `None`。
- AuthorizedExecution 的原始后端改为私有成员，不再提供公开 `.backend` 属性。
- 文件、Shell、子 Agent 工具，以及测试适配器全部更新，清除对 `runtime.capabilities.execution` 的依赖。
- 子 Agent 获取本次 cwd 使用 `runtime.execution.cwd()`；不要为它重新暴露原始 backend。
- TaskService 和 ProcessRegistry 继续使用既有 ProcessHandle，不需要原始 backend。

这里建立的是明确的模块/API 边界，不承诺防御同进程 Python 代码通过反射访问私有对象。

**必须新增测试：**

- 经真实 Executor 调用的工具，声明只读文件 A。
- 确认 runtime 的公开 capabilities 不包含原始 backend。
- 通过 AuthorizedExecution 读取 A 成功；读取兄弟文件 B、范围外文件、写入 A、启动 Shell 均失败。
- 使用没有 `"execution"` 声明的内部工具，确认它拿不到执行 wrapper。
- 调用结束后保留下来的 wrapper 不再允许访问环境。

### 2.2 审批完成后再决定最终并发

**现状：** `execute()` 先分批并启动工作线程，线程内部才审批。审批把只读参数改成写入参数后，调用仍在原并发批中。

引入内部 `PreparedInvocation` 数据结构，至少包含：

- 原始调用及最终调用参数。
- 工具、内部调用身份。
- 最终 PermissionResolution。
- 本次固定 cwd 和 AccessScope 快照。
- 最终并发安全性。
- 审批耗时和取消状态。

将权限准备和工具执行拆开：

1. 保留现有 schema 校验、pre-tool Hook 和 Hook 改写后的校验。
2. 按 Hook 处理后的调用形成“原批次”，保留原有顺序屏障。
3. 每个原批次开始时，按输入顺序逐个准备调用。
4. 每个调用读取当前会话范围和 cwd，完成 Resolver 裁决及审批改写。
5. 使用最终参数重新调用 `is_concurrency_safe()`；异常视为不安全。
6. 最终允许且参数确定后，提交授权变更；失败则该调用终止，不进入执行。
7. 保存准备结果，不再在工作线程中重复审批。
8. 对当前原批次内的准备结果按最终安全性重新分批。
9. 最终安全调用可以并发；不安全调用独占一个批次。
10. 当前原批次全部结束后，才准备下一个原批次。

补充约束：

- 不能跨原批次合并调用。
- 审批拒绝、校验失败和保存失败仍返回对应位置的失败结果，不能丢失调用。
- 准备好的安全调用也必须等到该原批次的权限准备完成后才开始执行。
- 授权变更提交后，后续准备的调用读取新的会话范围。
- 执行前重新检查取消状态。已明确批准并成功保存的会话/永久授权不因之后取消工具调用而自动回滚。
- 工具执行 timeout 从实际执行开始计算，审批等待不消耗该 timeout。
- intent、started、result 继续由执行阶段记录；记录最终参数和本次固定 cwd。
- 不修改模型原始调用记录。
- 返回顺序仍与输入顺序一致；生命周期事件及 on_result 不得重复。
- 每个 wrapper 只在本次实际执行期间有效，并在 finally 中关闭。

**必须新增测试：**

- 两个初始只读调用均被审批改成写操作。断言最终写操作没有重叠。
- 使用 Event/Barrier 稳定控制调度，不只依赖短暂 sleep。
- 两个最终仍安全的调用必须能够重叠，防止用“全部串行化”冒充修复。
- `[read, read, write, read]` 的屏障保持不变。
- 某调用改写后被 deny，不执行、不提交授权，不影响其他结果返回。
- 审批只发生在准备阶段，工作线程不会再次调用审批器。

## 3. 会话快照、子 Agent 与授权保存

### 3.1 统一使用调用准备阶段的 AccessScope

**现状：** Resolver 使用最新 Session，但工具拿到的 `capabilities.access_scope` 是 Executor 装配时的旧值。

修复方式：

- 从长期存在的 ToolCapabilities 删除 `access_scope`。
- 在 ToolRuntime 增加本次调用专用、不可变的 `access_scope`。
- Executor 每次准备调用时，从 Session 取得新快照；该快照用于本次 Resolver 裁决。
- 本次目录授权成功提交后，再取一次快照，作为本次工具执行上下文。这使工具能看到本次已经成功加入会话的目录。
- 准备结果保存该快照，执行时不再从旧 capabilities 取范围。
- Shell 完成后的 cwd 校验、子 Agent 创建时的目录继承统一使用本次 runtime 快照。
- Session 仍是额外目录的唯一可写来源；不要通过同时修改多个缓存来同步授权。
- 已准备或已运行的调用不动态更换快照；之后准备的调用读取新范围。

**必须新增测试：**

使用同一个 Executor 连续执行：

1. 越界读取，用户选择本会话目录授权。
2. Shell `cd` 到该目录。
3. 创建子 Agent。

断言：

- Session 中包含该目录。
- Shell 不出现 `cwd_reset`，会话 cwd 保持在新目录。
- 子 Agent 继承该目录。
- 原始工作区外、未授权的另一目录仍会触发审批或 cwd 重置。

测试中路径先规范化，避免 macOS `/var` 与 `/private/var` 的路径别名造成错误断言。

### 3.2 修复子 Agent cwd 的类型转换

**现状：** `child_session.set_cwd(capabilities.execution.cwd())` 将 ExecutionPath 传给需要 Path 的 Session，异常被捕获后子 Agent 留在工作区根目录。

修复方式：

- 从父调用的 AuthorizedExecution 获取固定 cwd。
- 在 engine 的子会话装配边界，显式将本地 ExecutionPath 转换为 Session 使用的 Path。
- 转换前检查环境身份。当前子会话装配仅支持本地环境；不支持的环境明确失败，不能悄悄创建本地子会话。
- 保留 Session.set_cwd 的现有类型契约，不让它同时接受 Path、ExecutionPath、字符串并猜测含义。
- 删除吞掉该类型错误后继续运行的行为；子会话上下文初始化失败应结束这次 spawn，并记录失败。
- 子 Agent 使用自己的 Session、cwd 和目录快照；后续修改不能回写父会话。

**必须新增测试：**

- 父 Agent 位于 `workspace/nested`。
- 根目录与 nested 中放置同名文件、不同内容。
- 子 Agent 使用相对路径读取该文件，必须得到 nested 文件的内容。
- 子 Agent 改变自己的 cwd 后，父会话 cwd 不变。
- 子 Agent 创建后，父会话新增目录不自动改变已创建子会话的范围。

### 3.3 为每个子会话绑定独立的授权保存回调

**现状：** 主 Agent 有 `authorization_commit`，子 Agent 没有。Executor 的备用分支仅修改会话内存，却把永久授权当作成功。

引入装配层 `authorization_commit_factory(session)`：

- 输入目标 Session，返回仅绑定该 Session 的授权提交回调。
- 工厂捕获现有 checkpoint store、是否启用会话保存，以及配置写入函数。
- 主 Agent 和每个子 Agent 都通过同一工厂获得自己的回调。
- 工厂沿 `build_agent_tools → make_spawn_agent_tool → 子 Agent` 传递，嵌套子 Agent 也必须接通。
- 不把绑定父 Session 的回调直接传给子 Agent。
- 不把 checkpoint store 或配置存储对象暴露给普通工具。
- 子会话授权保存时使用子会话 ID，不覆盖父会话 checkpoint。
- 不借此增加子 Agent 自动恢复或重放能力；恢复后不存在的进程仍按既有 unknown 语义处理。

提交语义：

- 本次批准：不修改任何授权存储。
- 会话目录：更新目标 Session；启用持久化时立即保存该 Session。
- 永久目录：完成上述会话更新，并写入用户权限配置。
- 永久规则：写入配置，并保持当前已有的规则生效行为。
- `no_session_persistence` 下，会话授权仅存内存；永久配置写入仍按用户选择执行。审批说明必须与此一致。
- 没有持久化回调的独立 Executor，遇到永久目录或永久规则变更必须明确失败，不能忽略。
- 保存异常时不执行目标工具，报告失败及已成功保存的部分；不宣称跨文件保存具有事务性。
- 配置写入继续使用当前带锁的读取、合并、原子替换逻辑，保留无关配置。
- 本轮不调整既有 session rule 的主/子共享策略；只修复目录隔离与承诺保存却未保存的问题。

**必须新增生产链集成测试：**

使用临时 WRIGHT_HOME、临时权限配置和 checkpoint 目录，不接触真实用户配置。

- 通过主 Agent 真正创建子 Agent。
- 子 Agent 请求外部文件，审批选择永久目录。
- 断言工具执行成功、配置包含目录、子会话 checkpoint 包含目录。
- 父会话及已存在的兄弟会话不因这次授权增加会话目录。
- 重新加载配置后，新装配会话能够取得永久目录。
- 注入配置或 checkpoint 保存失败，断言子工具未执行，结果明确失败。
- 同时批准两个不同子会话的永久目录，最终配置不能丢失任意一个。
- 独立 Executor 没有保存回调时，永久授权请求必须失败，而不是降级成会话授权。

## 4. 恢复 grep 行为，同时保留授权过滤

### 4.1 保留 ripgrep 作为本地搜索实现

删除本地 backend 中替代 ripgrep 的 Python 正则扫描逻辑。

恢复既有语义：

- `fixed_string=True` 使用 ripgrep 的字面量模式，不编译 Python 正则。
- 保留大小写选项、glob、行号、列号及同一行多个匹配。
- 目录搜索默认遵循 ripgrep 的隐藏文件、忽略文件和二进制处理行为。
- 显式文件目标与目录目标按照原实现分别处理，不能把显式文件一律套用目录扫描过滤规则。
- 保留最大结果数和既有 truncated 返回行为。
- 找不到 rg 时保留明确错误。
- 搜索总超时恢复为 20 秒，不能为每个文件重新获得 20 秒。

### 4.2 先确定候选文件，再批准内容读取

不能直接让 rg 搜索整个目录，再仅过滤返回结果。采用两阶段后端接口：

1. `iter_search_candidates(...)`：根据根路径、glob 和 ripgrep 忽略规则列出候选文件，不读取文件内容。
2. `search_files(...)`：只对传入的、已通过授权检查的文件执行内容搜索。

职责：

- 本地候选枚举使用 `rg --files` 及与原行为一致的过滤选项。
- AuthorizedExecution 对每个候选文件重新解析并检查环境、读权限、目录范围、禁止路径和符号链接目标。
- 只有通过检查的候选文件能进入 `search_files`。
- 不允许在搜索结果阶段才执行首次授权过滤。
- 本地 `search_files` 使用 `rg --json`，正确解析每个 submatch。
- 参数通过 argv 传递，不构造 Shell 命令；查询使用 `-e`，路径放在参数终止标记后，避免把以 `-` 开头的输入当选项。
- 大量候选文件分批处理，保持整次搜索共同的截止时间。
- 候选枚举、内容搜索都响应取消；超时、取消或生成器提前关闭时清理搜索子进程。
- 工具达到结果上限后显式关闭搜索迭代器，不能依赖垃圾回收结束进程。
- 搜索子进程属于文件读取实现，不向 grep 工具授予 Shell 权限。
- 更新 ExecutionBackend 协议及独立内存后端，移除旧搜索接口，不保留并行兼容路径。

**必须新增测试：**

- 字面量搜索 `[`、`(`、反斜杠成功。
- 大小写模式和普通正则结果正确。
- 同一行出现多个匹配时，数量和列号与原 rg 行为一致。
- 临时 Git 仓库含普通文件、隐藏文件、`.gitignore` 排除文件：默认目录搜索只返回原 rg 会返回的文件。
- glob 包含与排除、嵌套路径、显式文件目标与实际 rg 的参考结果一致。
- 受保护文件和指向未授权位置的符号链接，不能进入后端内容搜索候选列表。
- 使用 spy 记录 `search_files` 输入，证明禁止文件没有被先读后过滤。
- 超时、取消、达到 max_results 后，没有遗留搜索子进程。
- 独立内存后端仍能通过同一 AuthorizedExecution 搜索接口运行；它不能继承本地实现。

## 5. 实施顺序与验收

### 实施顺序

1. **保存基线并增加复现测试。**  
   优先覆盖原始 backend 旁路、审批改写并发、目录快照、子 cwd、子永久保存和 grep 字面量六个当前失败场景。

2. **收拢执行入口与调用上下文。**  
   移除工具 capabilities 的原始 backend 和长期 AccessScope，更新装配结果及 runtime 字段。

3. **拆分权限准备与执行阶段。**  
   接入 PreparedInvocation、最终参数分批、最终上下文和授权提交。

4. **修复子 Agent 装配与持久化。**  
   接通 cwd 转换、快照继承和每个 Session 独立的保存回调，包括嵌套子 Agent。

5. **恢复本地搜索语义。**  
   接通候选枚举、授权过滤和 rg 内容搜索，补齐取消与超时清理。

6. **运行完整验证并更新文档。**  
   文档说明真正接通的调用流程、授权存储边界和搜索行为；不要只在报告中声称已完成。

### 验证要求

先运行各步骤的针对性测试，最后运行：

- `uv run pytest -q`
- `uv run ruff check src`
- Python compileall
- `git diff --check`
- Web 的 `npm test`
- Web 的 `npm run build`
- Web 的 `npm run test:e2e`

构建生成的静态资源按仓库现有方式保持与源码一致，不夹带无关产物。

静态检查必须确认：

- 普通工具无法从 capabilities 获取原始 backend。
- AuthorizedExecution 没有公开 backend 属性。
- Shell 和子 Agent 不读取长期缓存的 AccessScope。
- 子 Agent 初始化不再把 ExecutionPath 直接交给 Session.set_cwd。
- 所有生产子 Agent 构造路径均接入授权保存工厂。
- grep 本地实现不再用 Python 正则替代 ripgrep。
- 工作线程执行阶段不再发起权限审批。
- 没有旧接口兼容别名或新增旁路。

### 完成标准与交付报告

六个已复现场景必须各自具备回归测试，并经过生产调用链验证。以下做法不能作为完成证据：

- 仅创建新类或字段，实际入口未迁移。
- 仅给测试工具宽授权，绕开 Resolver。
- 仅断言 callback 被调用，不检查配置、checkpoint 和实际执行结果。
- 用全串行化代替最终参数重新分批。
- 用搜索结果过滤代替读取前授权检查。
- 忽略保存错误后仍返回成功。

最终报告逐项列出：

| 项目 | 必须报告的证据 |
|---|---|
| 审批改写并发 | 写调用不重叠，安全调用仍能并发 |
| 执行入口 | 原始 backend 不再进入工具公开上下文 |
| 目录快照 | 新授权后 cd 和后续子 Agent 均生效 |
| 子 cwd | 相对路径实际读取父 cwd 下的正确文件 |
| 子永久授权 | 临时配置和子会话 checkpoint 实际保存且可重载 |
| grep | 字面量、忽略规则、多个匹配及授权过滤测试通过 |

另外报告完整检查结果、未通过或未验证的部分，以及工作区最终状态。存在任何未修复项目时，不得宣布本计划全部完成。

