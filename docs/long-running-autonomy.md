# Long-running Autonomy（第四阶段）

第四阶段在统一 Task Runtime 上增加了可持久化调度，但没有把 Scheduler、Agent 和 Shell
合并成一个巨型状态机。SQLite 文件位于 `workspace/.wright_tasks/tasks.sqlite3`。

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

Scheduler 线程只做四件事：检查触发条件、写 durable run、原子 claim、向 REPL 投递
`DURABLE_RUN_DUE(run_id)`。它从不调用 `Agent.run`，也不修改 root transcript。

REPL 主线程收到事件后只构造独立 `SessionState` 并 `background_runtime.submit(...)`，
然后立刻回到事件循环。Durable session 不继承 root 的 transcript、cwd、plan、status
或 memory。同一 workspace 同时最多 dispatch 一个 durable run（`max_inflight`，默认 1）；
提高上限前需要 git worktree 级别的隔离，否则并发 shell 会互相踩。

完成后投递 `DURABLE_RUN_FINISHED`，主线程只渲染一行摘要，不把结果注入 root 上下文。

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
  处理：`manual`（默认）→ 标 `unknown` 不重放，`retry` → 按 `max_retries` 重排一次。
  不再沿原 transcript 恢复；durable session 本身不写 checkpoint。这是有意的简化。
- 只有显式设置 `recovery_policy=retry` 的任务才会按 `max_retries` 和 delay 重试；
- `cancel_task(run_id)` 对 queued/dispatched 立即终止，对 running 设置取消信号，Agent 和工具
  在正常协作取消边界观察它。

默认 `manual` 是故意的：任意 Agent prompt 可能写文件、调用网络或触发外部副作用，不能
仅凭“进程断了”就假定可安全重放。

## 模型工具

调度定义：

- `create_task`
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
