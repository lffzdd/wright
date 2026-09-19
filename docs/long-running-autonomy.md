# Long-running Autonomy（第五阶段）

第四阶段在统一 Task Runtime 上增加了可持久化调度，但没有把 Scheduler、Agent 和 Shell
合并成一个巨型状态机。SQLite 文件位于
`~/.wright/projects/<project-id>/tasks.sqlite3`。

## 两层持久化模型

`AutomationRecord` 是“以后何时做什么”的定义：

```text
active ──pause──► paused ──resume──► active
   │
   ├── once trigger consumed ──► completed
   └── cancel ────────────────► cancelled
```

`DurableRunRecord` 是某次具体执行，也是 `TaskService` 中 `kind=durable` 的任务：

```text
queued → dispatched → running → completed
   ▲          │           ├── failed
   │          │           ├── cancelled
   │          │           ├── waiting_retry ──► dispatched
   │          │           └── unknown
   └──────────┘ process stopped before Agent started: safe requeue
```

定义和运行必须分开：周期定义可以产生很多次运行；暂停定义不应篡改已经完成的历史。

## Scheduler 与单写者边界

Scheduler 线程只做四件事：检查触发条件、写 durable run、原子 claim、投递已领取的
`run_id`。它从不调用 `Agent.run`，也不修改 root transcript。

`ApplicationHost` 是一个执行目录的应用级 owner：拥有 Scheduler、后台 worker、SQLite 连接、
独立 MCP 连接和控制面。CLI、TUI、Web runtime 在装配时创建或复用它，不借用来源聊天的
Renderer、队列或 MCP 连接。

CLI/TUI 退出时关闭当前进程的宿主。Web 的 `RuntimeManager` 按执行目录保留宿主；同目录
关闭后新建会话、恢复旧会话都复用它。每个已打开的来源 Session 保留自己的 AutonomyStore
视图和 Scheduler，记录不会混入另一个会话。关闭会话不删除 Automation，也不停止已接收的后台任务。

同一执行目录通过 OS 自动释放的 advisory file lock 保证只有一个 Host；另一进程不能把
仍在运行的 owner 当作崩溃现场恢复。各来源 Scheduler 共享宿主的原子领取容量检查，合计
最多运行一个 durable run，不能靠新建会话绕过同目录并发限制。不同 worktree 使用不同 Host。

宿主接收领取结果后构造独立 Session 并提交给后台 worker。Durable session 不继承 root 的
transcript、cwd、plan、status 或 memory。完成事件由宿主线程消费并转交可选回调，不注入 root 上下文。

关闭 Host 时先停止所有来源 Scheduler，等待 worker 退出后再释放 MCP、数据库和目录锁。
归档 worktree 前也必须确认无活跃任务或定义，并停止其宿主。进程重启只恢复显式打开的来源 scope；
数据库中存在定义本身不等于有一个存活的执行宿主。

## Headless 宿主

使用 `wright --ui headless --workspace DIR --automation-session SESSION_ID` 显式启动本地常驻宿主。
`SESSION_ID` 是创建 Automation 时显示的来源 session id；它只用于查找已持久化定义，并不恢复、
启动或借用那个聊天 Session。它不会安装
LaunchAgent/systemd、不会 daemonize、不会开放端口；进程仍在前台，Ctrl-C 即关闭。
数据库中有 Automation 只表示任务已持久化，不表示没有一个存活宿主时它仍在执行。

## 触发器

- `once`：epoch `run_at` 或相对 `delay_seconds`；
- `interval`：固定秒数；上次运行仍活跃时合并 tick，不无限堆积；
- `file_change`：监控 workspace 内文件/目录的存在性、mtime 和大小；
- `web_change`：在数据库事务外探测公共 HTTP(S) 页面，比较状态、ETag、
  Last-Modified 和正文 SHA-256；拒绝私有/回环地址，限制 5 秒和 1 MB；
- `event`：匹配持久化的命名外部事件。

终端可用 `/event <name> [JSON object]` 注入事件。其他本地进程也可以打开同一数据库，
使用 `AutonomyStore(...).emit_event(name, payload)`；Scheduler 最迟在下一 polling tick 发现。

## 重启与副作用安全

- `dispatched` 表示事件已入 REPL 队列但独立 session 尚未 start，重启后安全回到 `queued`；
- 进程崩溃时后台线程里的 `running` 行由 `store.recover_interrupted` 按 `recovery_policy`
  处理：`manual`（默认）→ 标 `unknown` 不重放。只有没有任何已启动工具意图的
  `retry` 运行才会按 `max_retries` 和 delay 重排一次；已有 `started` 工具的运行
  无论策略如何都标为 `unknown`。
  不再沿原 transcript 恢复；durable session 本身不写 checkpoint。这是有意的简化。
- 只有显式设置 `recovery_policy=retry`、且尚无工具执行过的失败运行才会按预算和 delay 重试。
  工具已执行但结果提交失败时，终态事务将工具日志和 Run 一并标为 `unknown`，清空成功结果，
  禁止外层调度器将其降级为普通失败再次执行。即使工具已有确认结果，也不能在缺少幂等契约时
  因后续模型失败而重跑整个任务。领取队列时同样检查工具日志，旧版本遗留的
  不安全 queued/waiting_retry 记录会被终结，不会继续执行。
- `cancel_task(run_id)` 对 queued/dispatched 立即终止，对 running 设置取消信号，Agent 和工具
  在正常协作取消边界观察它。

默认 `manual` 是故意的：任意 Agent prompt 可能写文件、调用网络或触发外部副作用，不能
仅凭“进程断了”就假定可安全重放。

对 durable 工具调用，SQLite 记录顺序为“意图 → started → 结果”，并在意图写入失败时
拒绝执行。若宿主在 `started` 后、结果提交前中断，恢复会把该工具和 Run 标为 `unknown`；
不会因为内部重领或恢复而再次执行 shell、写文件或远端副作用。该机制不宣称对任意外部
服务提供 exactly-once。

`command_id` 以 `session:<id>` 作用域持久化。相同 id、相同内容重放返回已有接受状态；
相同 id、不同内容被拒绝。交互回复只保存回答摘要的 SHA-256，不把可能敏感的回答复制进
命令去重记录。

## 后台 Run 历史与子 Agent 追溯

自动任务的 SQLite 记录是后台 Run 状态和结果的权威事实来源；交互 Session 的 checkpoint
只负责交互会话恢复，不替代后台记录。每个 durable Run 在同一个 `tasks.sqlite3` 中写入有序的
`durable_run_history` 事实，包括用户输入、`ModelStep`、工具意图/启动/结果、子 Agent 的
绑定关系和最终完成事件。事件带稳定的 `event_id` 与幂等键，内容有界且不写入活跃线程、进程
句柄或未脱敏凭据。

子 Agent 使用父 Run 提供的窄 journal 工厂：工具执行记录保留 `run_id`、`step_id`、
`call_id`、`agent_task_id`、`parent_agent_task_id` 和来源 Session Run。来源聊天 Session
关闭后，宿主仍可用 Run ID 查询完整投影；Web 通过认证的
`GET /api/v1/runs/<run_id>` 查询，宿主停止后也可从显式打开项目的数据库读取，查询不会
重新启动任务或重新执行工具。

当前 schema 版本为 6；从旧版本打开数据库会增量创建历史表、补齐工具执行归属列并保留
provider 原始 call ID，不重写
既有运行，也不会因为迁移重新执行已完成或未知的副作用。历史缺失只表示旧记录没有该事实，
不会被推断成成功。

## 模型工具

调度定义：

- `schedule_task`
- `get_schedule` / `list_schedules`
- `pause_schedule` / `resume_schedule` / `cancel_schedule`
- `list_task_runs`

具体运行继续使用统一接口：

- `get_task`
- `list_tasks`
- `wait_task`
- `cancel_task`

创建或修改持久化调度会进入 permission `ask`，风险标识为
`persistent_automation`。无人值守环境不会因此绕过既有工具权限：没有持久化 allow 规则
的写入、网络或外部工具仍然 fail-closed。
