# Unified Task Runtime（阶段 3.5）

这次重构的目标不是把 Agent、Shell 和未来的持久化任务塞进一个巨型状态机，而是让
它们拥有同一个控制入口，同时保留各自正确的执行语义。

## 核心结构

```text
Model
    ├── agent_task_id  → get_agent / wait_agent / cancel_agent
    ├── command_id     → get_command / wait_command / list_commands / terminate_command
    └── schedule_id / run_id → schedule tools
              │
              ▼
        TaskService（内部路由，不暴露给模型）
              │
              ├── AgentTaskBackend ──► AgentControlPlane
              ├── ShellTaskBackend ──► Session.background_tasks + ProcessRegistry
              └── DurableTaskBackend ──► AutonomyStore（共享库；调度经 JobDispatch）
```

`RuntimeTask` 仍是 UI、事件和诊断用的只读投影，不是另一套状态机。模型看到的是
对象类型明确的标识：`agent_task_id`、`command_id`、`schedule_id`、`run_id`。

## 模型工具面

子 Agent 委派：

- `spawn_agent` / `get_agent_tree`
- `get_agent` / `wait_agent` / `cancel_agent`，参数是 `agent_task_id`

命令执行：

- `execute_command` 在后台启动或超时转后台时返回 `command_id`
- `get_command` / `wait_command` / `terminate_command`
- `list_commands` 默认只列当前 user turn 的后台命令，可按状态过滤；
  `include_all_turns=true` 只扩展到当前 session

持久化调度见 [`long-running-autonomy.md`](long-running-autonomy.md)。

等待超时只结束这次等待。`unknown` / `outcome=unconfirmed` 表示无法确认，不是成功完成。
把别的家族的 id 传进来会在控制动作之前失败。

## 完成通知

Agent worker 和 Shell reader 仍向 REPL 投递内部 `TASK_DONE`。REPL 读取真实 owner 后，
交给模型的事件带有 `agent_task_id` 或 `command_id`，以及对应的后续工具名。

## 第四阶段扩展（已实现）

定时规则、条件触发和外部事件监听由独立 Scheduler 管理；调度定义不是一次运行，
不会伪装成当前的进程内任务。具体执行通过 `DurableTaskBackend` 接入 `TaskService`，
Agent/Shell 的执行内核没有被改写。完整状态机与恢复规则见
[`long-running-autonomy.md`](long-running-autonomy.md)。
