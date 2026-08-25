# Multi-Agent 控制面

## 为什么原来的 `spawn_agent` 还不算控制面

原实现已经具备两个重要性质：子 Agent 上下文隔离，以及同一轮多个
`spawn_agent` 的线程并发。但每次调用只是临时创建一个 `SessionState`，执行结束后
只返回字符串。系统无法回答这些问题：

- 当前一共有多少子任务，谁是谁的孩子？
- 某个孩子是在运行、失败、取消，还是超时？
- 全树已经消耗多少 step 和 token？
- 父任务取消后，已经递归创建的后代是否都会停止？
- 进程在子 Agent 运行中崩溃，恢复后如何表示未知结果？

第三阶段新增的 `AgentControlPlane` 是这些状态的唯一真值来源；`subagent.py` 只负责
把真正的 Agent 执行接到控制面上。

## 结构

```text
Root SessionState
└── AgentControlPlane（全树共享）
    ├── AgentTask a0001...（depth 1）
    │   └── AgentTask a0002...（depth 2）
    └── AgentTask a0003...（depth 1）

每个 AgentTask
├── 稳定 task_id / parent_id / tool_call_id
├── root_turn_id / depth / child_session_id
├── lifecycle timestamps + status
├── step reservation + actual steps
├── token usage
└── bounded result / error
```

Root 和所有后代共享同一个控制面，但每个子 Agent 仍拥有独立 `SessionState` 和消息
历史。控制面保存任务摘要，不保存子 Agent transcript，因此不会破坏上下文隔离。

## 生命周期

```text
begin_task
    ↓
running ──成功──→ completed
    ├────失败──→ failed
    ├────取消──→ cancelled
    └────超时──→ timed_out
```

并发容量不足时采用 fail-fast，而不是等待 semaphore。原因是运行中的父 Agent 可能都
在等待自己的孩子；阻塞等待新槽位会形成“父占槽、子等槽”的树形死锁。

## 共享预算

### Step

创建任务时先从当前 `root_turn_id` 的共享池预留 step。并发兄弟无法分别看到同一份
“剩余预算”，所以总量不会超卖。任务结束后按实际 `steps_used` 结算，未使用的预留量
可供后续任务使用。

### Token

每个子 Agent 的真实 `UsageEvent` 会立即累计进控制面。当全树达到 token 上限时，
当前 root turn 的所有运行任务都会收到取消信号。单次 LLM 请求可能略微越过边界，
因为服务端只有在请求完成后才返回准确 usage；下一项工具副作用或下一轮 LLM 调用不会
再开始。

## 取消与超时

- 控制面对父任务发出的取消会递归设置所有后代的 cancellation event。
- `ToolRuntime` 同时观察 executor deadline、父 Agent 取消和控制面取消。
- `spawn_agent` 使用独立的 300 秒 deadline，不再错误复用普通工具的 30 秒期限。
- Python 线程不能安全强杀，因此取消是协作式的：Agent 会在每轮开始、usage 入账后、
  工具调用前检查；长工具应通过 `ToolRuntime.raise_if_cancelled()` 定期检查。
- 子 Agent 禁止启动或自动遗留后台 shell。否则子 Session 结束后父 Agent 无法管理进程。

## Checkpoint / crash recovery

控制面每次关键状态变化都会触发 root checkpoint：任务建立、绑定 child session、usage
更新和终态都会保存。恢复时：

- 已完成任务保留原状态与结果摘要；
- checkpoint 中仍为 pending/running 的任务改为 failed；
- error 明确标记“进程重启、结果未知”，要求重新核实 workspace；
- 外层 pending `spawn_agent` 工具调用仍走原有 interrupted-tool recovery，不会静默重放
  可能产生副作用的子任务。

## 结果与可观测性

- 同一模型回合内多个 `spawn_agent` 可并发运行，`ToolExecutor` 按原 tool-call 顺序聚合
  结果，不按完成顺序打乱。
- Renderer 接收结构化 `agent_task` 事件，可观察 running/terminal 转换。
- Root 可调用 `get_agent_tree` 查看 Agent 树；单任务读取、等待与取消统一走
  `get_task`、`wait_task`、`cancel_task`。旧 Agent 专用工具只作为隐藏兼容别名保留。
- 树视图是有硬截断的摘要，避免把整棵树的完整输出重新灌入上下文。
- 完整单任务结果只通过对应 `spawn_agent` 返回，并受 `max_result_chars` 限制。
- Episodic Memory 会保存紧凑的子 Agent 执行摘要，供以后回忆执行经验。

## 后台 Agent

- Root 可以在 `spawn_agent` 中设置 `run_in_background=true`，调用会立即返回
  `task_id`，不占住当前 ReAct 轮。
- 后台 worker 只运行隔离的子 Session；只有 REPL 主线程能重新调用 root
  `Agent.run`，因此不会并发改写 root transcript。
- 任务进入终态后，worker 只投递 `TASK_DONE(task_id)`；REPL 主线程通过
  `TaskService` 读取统一 `RuntimeTask`，再生成有界 runtime event。Agent 与 Shell
  后台任务共用这条通知链路。
- 通知走 `Agent.run_runtime_event`，不会伪装成新用户输入：原 user goal、Plan、Verifier
  证据边界和 root turn id 都保持不变，也不会额外召回记忆。
- 若 root 收口时仍有后台 Agent，Episodic/Semantic Memory 会延迟到当前 turn 的最后一个
  后台任务完成并被吸收后再写入，避免把 `running` 状态固化成永久 episode。
- 主会话退出时会向所有在途任务传播取消，等待短暂宽限期后关闭 executor。

## 当前有意保留的边界

- 所有 Agent 共享 workspace。文件工具有进程内路径锁，但控制面无法理解任意 shell 的
  语义冲突；只有互相独立的任务才应并发委派。
- 子 Agent 不持有长期 Memory 和 `ask_user`。需要人的信息必须回报父 Agent，由 root
  决定是否询问。
- Python 线程无法强制终止正卡在不可中断第三方调用中的 worker；取消依赖
  Agent/工具在边界点协作检查。
