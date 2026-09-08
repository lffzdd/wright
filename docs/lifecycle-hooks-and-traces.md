# 第四阶段：Lifecycle Hooks 与结构化 Trace

## 为什么先做这一层

前三阶段已经让 Agent 能规划、询问、验证、恢复、记忆和委派，但运行过程仍主要只能从
终端输出倒推。第四阶段把“发生了什么”与“发生时允许外部做什么”分开：

- `TraceRecorder` 是只追加的事实日志，失败也不改变 Agent 决策；
- `LifecycleManager` 把事实分发给可选 Hook；
- Hook 只有返回合法的 `decision: deny` 才能阻断，异常会写入 `hook_error` 后 fail-open；
- `pre_tool_use` 改写的参数必须重新通过 JSON Schema 与 PermissionResolver，Hook 不是后门。

这个边界借鉴了 Claude Code 的 Session / Tool / Stop / Subagent / Compact 事件划分，但没有
照搬它的大型插件系统。当前实现只有一个小型同步协议，便于逐层读懂。

## 事件

| 事件 | 时机 | 可阻断 |
|---|---|---|
| `session_start` | root session 装配完成 | 否 |
| `runtime_event` | 后台 Agent 完成等内部事件进入 root | 否 |
| `user_prompt_submit` | root 用户输入落 transcript 前 | 是 |
| `agent_start` / `agent_stop` | root Agent 开始 / 候选完成或终止 | `agent_stop` 可拒绝候选完成 |
| `llm_start` / `llm_end` / `llm_error` | 主模型请求、耗时、usage 或异常 | 否 |
| `pre_tool_use` | Schema 初检后、权限检查前 | 是，可改写参数 |
| `permission_decision` | 改写后参数的权限判定结果 | 否 |
| `post_tool_use` / `tool_failure` | 工具进入成功 / 失败终态 | 否 |
| `subagent_start` / `subagent_stop` | 隔离子 Session 开始 / 结束 | 否 |
| `pre_compact` / `post_compact` | 上下文折叠前 / 后 | 否 |
| `hook_result` / `hook_error` | Hook 自身结果 / 异常的审计记录 | 内部事件，不再触发 Hook |

`agent_stop` 被拒绝后，原因会作为反馈回到正常 ReAct 循环，仍受完成验证重试上限约束。
后台通知使用独立 `runtime_event` 路径，不触发 `user_prompt_submit` Hook，也不被当成新的
用户目标或新的 Episodic Memory 回合。

## Trace 格式

每个 session 写入 `~/.wright/projects/<project-id>/traces/<session_id>.jsonl`，
不写进项目源码树。`WRIGHT_HOME` 可改根目录。

每行包含 `schema_version`、稳定 `event_id`、单调 `sequence`、时间、session / root turn /
subagent 身份和有界 `payload`。记录器线程安全；恢复旧 session 时从已有 sequence 继续；若
进程崩溃留下半行，后续合法记录仍可读取。常见 secret/password/token/cookie 字段会脱敏，
长字符串、深层对象和大数组会截断。

Trace 是审计与后续 eval 的输入，不是完整 transcript，也不承诺能重放外部副作用。

## 配置命令 Hook

命令 Hook 不会从仓库自动加载。复制或修改 `docs/lifecycle-hooks.example.json` 后，必须由
用户显式启动：

```bash
python -m wright.main --hooks-config path/to/hooks.json
```

这一步就是执行本地 Hook 程序的授权边界；Hook 命令本身不再经过 Agent 的
PermissionResolver。`command` 必须是 argv 数组，系统以 `shell=False` 执行，并把事件
JSON 写到 stdin。

Hook 可不输出内容，或在 stdout 输出一个 JSON 对象：

```json
{
  "decision": "allow",
  "reason": "optional explanation",
  "updated_input": {"path": "safe/path.txt"},
  "additional_context": "context visible to the next agent call"
}
```

- 退出码 `0`：正常；stdout 为空等同 allow。
- 退出码 `2`：明确 deny，stderr 作为原因。
- 其他退出码、超时、非法 JSON：记入 `hook_error`，当前执行继续。
- `matcher` 使用大小写敏感 glob，匹配工具名；省略时匹配全部。
- `updated_input` 目前只在 `pre_tool_use` 生效；`additional_context` 目前在
  `user_prompt_submit` 生效。

## 与 Verifier 的区别

Verifier 默认只做确定性收口检查：计划是否完成、本轮工具是否结束、声称写入的
文件是否还在磁盘上。它在模型已经流式打出候选回答之后运行，只决定 agent
循环能不能停，不负责「审一遍文案再显示」。`agent_stop` Hook 是项目扩展点，
例如跑固定测试或组织规则。二者都能拒绝候选完成。
