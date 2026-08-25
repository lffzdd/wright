# Unified Task Runtime（阶段 3.5）

这次重构的目标不是把 Agent、Shell 和未来的持久化任务塞进一个巨型状态机，而是让
它们拥有同一个控制入口，同时保留各自正确的执行语义。

## 核心结构

```text
Model / REPL
    │
    ▼
TaskService  ── get / list / wait / cancel
    │
    ├── AgentTaskBackend ──► AgentControlPlane（Agent 状态唯一 owner）
    │
    └── ShellTaskBackend ──► SessionState.background_tasks（Shell 状态唯一 owner）
```

`RuntimeTask` 是只读投影视图，不保存任务状态。它统一了 `id`、`kind`、`status`、
时间、结果/输出、错误和取消信息；backend-specific 数据仅放在 `details` 中。

这种边界避免了最危险的做法：控制面、Shell registry 和“统一任务表”各写一份 status，
最终出现三份互相矛盾的真相。

## 模型工具面

- `get_task(task_id)`：读取任意 Agent/Shell 任务；
- `list_tasks(...)`：默认列当前 user turn，可按 kind/status 过滤；
- `wait_task(task_id, timeout)`：等待终态，超时只返回观察结果，不取消任务；
- `cancel_task(task_id, reason)`：Agent 走协作取消并传播到后代，Shell 终止整个进程组。

`get_task_output`、`get_agent_task`、`cancel_agent_task` 仍注册在 executor 中，旧 transcript
或测试可以继续调用；但它们不再进入新会话的系统提示，避免模型同时学习两套 API。

## 完成通知

Agent worker 和 Shell reader 都只向 REPL 投递 `TASK_DONE(task_id)`。REPL 是 root session
唯一写入者，它根据 task id 从真实 owner 读取 `RuntimeTask`，生成统一的
`task_notification` runtime event，再让 Agent 处理结果。后台线程不会直接改 root
transcript。

## 第四阶段扩展（已实现）

定时规则、条件触发和外部事件监听由独立 Scheduler 管理；调度定义不是一次运行，
不会伪装成当前的进程内任务。具体执行通过 `DurableTaskBackend` 接入 `TaskService`，
Agent/Shell 的执行内核没有被改写。完整状态机与恢复规则见
[`long-running-autonomy.md`](long-running-autonomy.md)。
