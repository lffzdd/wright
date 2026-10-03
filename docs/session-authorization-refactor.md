# 会话授权封装与三层权限管理重构

实施日期：2026-10-03。对照基线：Git HEAD `247cc8c`。

## 实际改动

- `Session.authorization` 成为会话权限状态唯一入口，持有 `SessionAuthorization`。规则为私有、不可变的 `PermissionRule` 元组；领域模型不读取仓储，也不转换持久化映射。授权对象独立加锁，集中管理两种模式、父级绑定、授权归属和有效执行限制，拒绝循环及重复会话身份。
- 父级关系只引用授权对象，不引用整个 Session。交互子 Agent 的批准归属根用户会话，父级撤销和硬限制实时生效，一次性许可不记忆。工具可见性也使用有效父级限制。
- 应用层 `application/permission/service.py` 统一会话、项目和用户授权的读取、版本、保存、撤销、模式变更和恢复。项目及用户授权独立保存，不复制进会话对象；模式更新与撤销失败恢复原状态。运行重新组装时复用注入的权限服务。
- 旧 Session 权限字段、增删方法及 `application/tool_execution/permissions.py` 已删除，没有兼容属性或旧模块转发。CLI/TUI/Web、文件引用、交互子 Agent、调度执行、工具执行及相关脚本和测试已迁移。Web 请求与事件保持现行字段。
- 检查点只接受版本 8，使用嵌套 `authorization`；独立授权仓储保持版本 2。旧检查点报错且不改写、不删除。编解码及历史预览不查询权限仓储；应用层在执行前刷新授权，以仓储撤销优先，并通过后端验证 cwd。执行根不可用时拒绝恢复。
- 保留用户已有前端样式及生产资源改动。浏览器验证生成的原本干净的截图已恢复，没有构建生产前端资源。

## 验证证据

- 实现前：权限、检查点和架构基线测试 **143 passed**。
- 最终相关回归：**254 passed**，命令：
  `uv run pytest tests/permission tests/test_checkpoint.py tests/test_architecture.py tests/test_architecture_boundaries.py tests/test_session_service.py tests/test_session_coordination.py tests/test_web_runtime_manager.py tests/test_tui.py tests/test_cross_surface.py tests/workspace -q --tb=short`。
- 前端：`npm test -- --reporter=dot` → **70 passed**；`npx tsc -b` 通过。
- 浏览器：`npm run test:e2e -- permissions.spec.ts two-sessions.spec.ts` → **7 passed**，覆盖权限期限、窄屏及中英文、Shell 设置与联网选择、多会话隔离和切换。这些流程使用结构化后端测试数据及模拟接口，不代替真实 OS Shell 验收。
- `uv run ruff check src tests scripts/smoke_subagent.py`、`uv run python -m compileall -q src/wright`、`git diff --check` 通过。

## 完整检查的限制

最终 `uv run pytest tests -q --tb=short` → **12 failed, 910 passed, 10 skipped in 61.35s (0:01:01)**。全部 12 项失败均位于 Shell 执行相关路径，且均包含在独立未修改 HEAD 对照的失败集合中。最终失败用例：

- `tests/test_access.py::test_command_cwd_snaps_back_when_leaving_granted_roots`
- `tests/test_access.py::test_command_cwd_stays_inside_granted_extra_root`
- `tests/test_capabilities.py::test_command_tool_starts_and_waits_through_execution_backend`
- `tests/test_execution_control.py::test_command_tools_follow_background_and_timeout_handoff`
- `tests/test_execution_control.py::test_wait_timeout_does_not_terminate_and_list_stays_in_session`
- `tests/test_execution_control.py::test_terminate_command_kills_the_process_tree`
- `tests/test_execution_regressions.py::test_parent_exit_does_not_leave_owned_children`
- `tests/test_execution_regressions.py::test_ignored_sigterm_is_escalated_on_the_owned_group`
- `tests/test_execution_regressions.py::test_output_pages_keep_a_prefix_and_mark_truncation`
- `tests/test_plan2_execution.py::test_one_executor_refreshes_scope_for_shell_and_child`
- `tests/test_task_service.py::test_service_projects_agent_and_shell_without_copying_ownership`
- `tests/test_tool_orchestration.py::test_command_cwd_is_isolated_per_session`

以下基线问题已通过独立导出的未修改 HEAD 确认：

- Shell 相关测试在当前 macOS 环境中存在进程退出码 `-6` 及其后续状态断言失败。对照运行七个涉及执行的测试文件，得到 **13 failed, 49 passed**；失败覆盖命令 cwd、执行后端、后台等待、终止进程树、输出分页、子 Agent Shell、任务投影和会话 cwd。没有通过绕过隔离、更改超时、修改断言或跳过用例使其变绿。
- 完整 `uv run ruff check src tests scripts` 存在三个原有问题：`scripts/check_cycles.py:13` 的 FURB188、`:35` 的 RUF005，以及 `scripts/eval_episode_memory.py:55` 的 I001。HEAD 对照结果一致，未修改这些无关脚本。
- 模块检查脚本报告四个既有环，HEAD 和修改后完全一致；领域/应用层边界测试通过，未新增环。
- 单独按 `tests/workspace tests/test_tui.py` 的顺序运行，会因 locale 状态残留而出现三个 TUI 英文文案断言失败。HEAD 对照同样得到 **3 failed, 49 passed**。标准完整集合及上述相关回归顺序没有该失败；本次未修改 locale 功能。

## 是否解决

会话权限状态的封装、三层统一管理入口、恢复职责分层及旧接口移除已完成，相关回归通过。完整验收仍受上述基线问题限制，不能将本次结果描述为全量测试及全量 lint 全部通过。
