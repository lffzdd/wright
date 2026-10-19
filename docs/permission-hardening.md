# 权限流程收束与执行边界加固

2026-10-03，当前 macOS checkout。本次保留三层授权语义、父子继承、一次性许可与既有 UI 契约。检查点只接受 v8，权限仓储保持 v2；未添加旧接口转发或数据迁移。前端源文件、样式及生产资源未修改。Playwright 产生的截图已保存到 `/tmp/wright-hardening-playwright-images`，仓库中的既有截图已还原。

## 原问题 → 实际改动

| 原问题 | 实际改动与执行边界 |
| --- | --- |
| 保护边界解析失败被忽略 | Resolver 解析失败即拒绝；缺失但可解析的保护路径保留在许可中。文件操作和 Shell 启动前重验保护路径；后台校验也检测保护边界变化。事务目录加入保护范围。 |
| 授权编排分散 | `PermissionService.authorize` 协调决策、审批、改写重判、版本验证、提交和执行能力，返回 `AuthorizedInvocation`。Dispatcher 只准备和调度；文件引用共用该流程；界面通过 `grant_resource` 提交。删除提交回调模块与测试专用的生产许可构造入口。 |
| Resolver 包含交互与展示 | 领域 Resolver 仅解析、决策和描述许可事实。应用 `AuthorizationWorkflow` 使用审批适配器；工具提供详情描述器，应用层生成审批输出与策略摘要。生产注入参数改名 `permission_workflow`，没有旧名称别名。 |
| 快照及变更可变 | `PermissionConfiguration`、规则、范围、继承状态、模式、版本构成不可变快照。`AuthorizationChange` 只接受类型化不可变规则并验证期限；ID 与项目归属由应用提交赋值。继承拓扑及规则锁保证一致捕获与提交。 |
| 重复装载与策略通道 | 删除 `inline_settings` 及后台静态配置副本，生产使用仓储，测试显式注入内存仓储。缓存依赖轻量文件修订、模式、继承和边界；捕获期间修订变化重试。Ask/Plan 操作上限来自同一份领域定义。 |
| 网络许可仅在工具调用前检查 | 内置 HTTP 与搜索使用受限网络执行接口；许可绑定规范 URL、查询参数、HTTP 方法、语义操作和调用身份。发送前检查版本与目标，不自动重试或跟随重定向。搜索 POST 仍为 `network_read`。供应商凭据在基础设施获取，未加入规则或投影。Knowledge/MCP 保持原边界。 |
| 跨文件补偿缺乏崩溃一致性 | 专用工作单元及显式检查点参与者协调 Session/Project/User 文件和检查点。v1 日志持久化 PREPARED、文件替换、COMMITTED、清理，同步文件和新目录；恢复旧状态或完成新状态。所有补偿逐项尝试并保留失败；未恢复完禁止许可。普通保存、重命名和检查点读取共享恢复锁。 |

会话授权继续由 `SessionAuthorization` 持有；Project/User 规则组合进快照，不复制成会话状态。交互子 Agent 共享根会话服务和检查点参与者；后台调度使用同一权威仓储的独立服务，不持有用户 Session 的检查点生命周期。历史解码与投影不装载授权或创建执行能力；基础设施读取协调只恢复文件事务。

## 验证证据

执行前基线：`uv run pytest tests` 为 **910 passed / 12 failed / 10 skipped**；完整 Ruff 存在 3 个既有脚本问题。基线日志 `/tmp/wright-permission-hardening-baseline-tests.log`、`/tmp/wright-permission-hardening-baseline-ruff.log`。

真实 Dispatcher 的普通 `read_file`：原完整 Settings/Session 装载分别 **14/14 次**；当前冷缓存 **1/1 次**，热缓存 **0/0 次**。目录遍历继续逐资源校验，后台监控继续检查撤销；未引入 TTL。

新增 24 项权限加固测试覆盖：保护解析失败与执行前变更、不可变上下文与类型化变更、读取次数、跨服务和进程撤销、捕获期间外部修订、HTTP 目标/方法/查询参数、搜索 POST 和供应商凭据、多个事务持久化阶段进程退出、重复恢复、补偿失败、未登记路径、校验和损坏、检查点保存与恢复竞争，以及授权和检查点共同回滚。架构断言同时约束领域交互依赖、旧回调/静态合并、生产许可构造和仓储事务访问。

| 验证 | 结果 |
| --- | --- |
| 权限、检查点、Session、架构、跨界面及后端边界专项 | 296 passed |
| 完整 `uv run pytest tests` | 935 passed / 12 基线失败 / 10 skipped |
| `uv run ruff check src tests scripts` | 仅 3 个基线问题；变更的源代码、测试和夹具生成脚本检查通过 |
| 前端 `npm test` | 70 passed，9 个测试文件 |
| `npx tsc -b` | 通过 |
| 既有权限与多会话 Playwright | 7 passed |
| `uv run python scripts/smoke_subagent.py` | 5 passed |
| `git diff --check` | 通过 |

完整后端日志 `/tmp/wright-hardening-full-verified.log`；专项日志 `/tmp/wright-hardening-focused-final.log`；Ruff `/tmp/wright-hardening-ruff-final.log`；前端 `/tmp/wright-hardening-frontend-tests.log`、`/tmp/wright-hardening-tsc-final.log`、`/tmp/wright-hardening-playwright.log`；子 Agent `/tmp/wright-hardening-subagent.log`。

Playwright 通过现有后端契约夹具覆盖 UI 审批与多会话交互；真实后端入口由 Python 测试验证。这次没有生成前端生产资源。

## 基线失败与未解决问题

完整后端保留基线中的 12 项原生 Shell 相关失败，失败用例集合相同：

- `test_access.py`：2 项命令 cwd 测试。
- `test_capabilities.py`：命令后端启动与等待。
- `test_execution_control.py`：后台/超时切换、等待、进程树终止 3 项。
- `test_execution_regressions.py`：父进程退出、SIGTERM 升级、输出分页 3 项。
- `test_plan2_execution.py`：同一执行器刷新 Shell 与子 Agent 范围。
- `test_task_service.py`：Agent 与 Shell 投影。
- `test_tool_orchestration.py`：跨会话 cwd 隔离。

本机原生 Shell 子进程退出码 `-6`，导致这些执行和后台行为断言失败；该环境问题本次未解决，没有调整测试条件或放宽执行边界来使其通过。完整测试并非全绿，三平台原生隔离验收不由本次结果替代。

Ruff 的既有问题为 `scripts/check_cycles.py:13` FURB188、`:35` RUF005，以及 `scripts/eval_episode_memory.py:55` I001，本次未修改这两个无关脚本。
