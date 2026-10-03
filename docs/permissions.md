# 权限与原生 Shell 隔离

权限判断只在领域层实现；应用层 PermissionService 统一生成版本快照、保存授权和撤销；基础设施仓储及系统沙箱在组合入口注入。界面只选择服务端提供的选项 ID。

## 授权

一次批准仅授权当前调用的实际目标，不产生记忆。文件记忆默认精确到文件。目录授权必须明确选择，递归覆盖目录及子目录，并标明只读或可读写。会话授权在恢复同一会话后仍有效；长期默认保存到当前项目；所有项目长期授权放在高级选项。

界面保存的文件和目录授权按资源与操作匹配，不绑定某一个文件工具；例如记住一个文件的读写授权后，写入和编辑工具可以在同一个文件上复用授权，相邻文件仍需单独批准。显式配置的工具限制继续约束实际调用，权限页会标明该限制。策略投影显示真实的会话、项目和用户期限，以及覆盖默认策略的只读边界。

审批卡将命令或文件路径与内容预览分开显示，截断预览明确说明批准作用于完整内容。风险提示只描述已识别的删除、移动、Git 修改等特征，不代替领域层授权和系统隔离。保存或撤销遇到版本冲突时，界面展示最新状态并保留错误提示，由用户查看后重试；不自动扩大授权或重新提交。

Shell 审批先检查系统隔离状态，未就绪时按钮和快捷键都不能放行，拒绝仍可用。初始化完成后仍需明确批准当前调用。选中联网会把主按钮改为“允许此次联网”，同时禁用保存离线命令授权，避免记忆授权静默丢失联网选择。

Web 权限文案和审批测试数据由 `uv run python scripts/generate_permission_fixtures.py` 从后端生成；后端测试验证生成结果，浏览器测试使用相同的结构化数据覆盖中英文、窄屏、授权期限、初始化和联网交互。

项目空间位于用户运行状态目录 `projects/<现有项目身份>/permissions.json`，不读取仓库里的授权文件。worktree 共用项目授权；工作区规则相对当前执行根解析。外部资源使用基础设施解析出的绝对规范路径。主 Agent、交互子 Agent 共用用户会话授权；父会话的硬模式限制会约束子 Agent，模式变化使旧许可失效。会话授权独立存储在 `permissions-sessions/<session_id>.json`，checkpoint 只记录该会话授权快照。恢复时仓储里的撤销结果优先。

固定优先级：保护资源、模式硬限制和只读边界 → deny → ask → 明确授权 → 模式默认。bypass 只减少当前边界内的确认。HTTP 授权包含协议、主机、端口和方法；HTTP 拒绝规则不影响 HTTPS。Shell 记忆只支持会话内的精确命令、工作目录及沙箱能力。副作用不明的工具调用只支持一次批准。

权限文件独立使用版本 2，不改变会话 checkpoint 版本。旧授权格式、additionalDirectories 和含糊规则直接报错，不迁移、不扩大授权。新记录携带稳定 ID、lifetime、project_id、资源、操作和来源。会话、项目、用户授权各自保存，不把长期记录复制成会话记录。

管理接口添加授权传入 resource、lifetime、expected_version；撤销传入稳定 ID、source、expected_version。旧版本更新返回 409 及最新状态。审批完成、保存之后及实际执行之前都检查版本；跨进程修改会被重新读取。撤销取消待审批请求，令待执行调用失效，并停止依赖该授权的后台 Shell；已完成副作用不回滚。

## Shell 后端

Shell 默认离线；单次联网是独立能力，仍保留文件隔离。隔离启动失败返回错误，不重试为普通进程。

* Linux：系统安装的 Bubblewrap，私有 PID、IPC、UTS、用户和离线网络命名空间，最小挂载视图、只读运行时、私有临时目录。离线 seccomp 同时阻止新 socket 和 io_uring，避免已挂载 Unix socket 绕过网络隔离。需要允许用户命名空间及 seccomp 的内核配置。
* macOS 12+：系统 `/usr/bin/sandbox-exec`，按许可生成 Seatbelt 文件和网络规则；可用性通过真实探针检查。
* Windows 11：`wright --sandbox-setup` 或权限页请求管理员初始化两个独立低权限账户、系统 WFP 离线网络规则、只读运行时 ACL 和 DPAPI 凭据。每次执行使用限制 SID、临时资源 ACL、原生 PowerShell 和禁止子进程逃逸的 Job Object。ACL 和账户修改记录归属，清理仅撤销自身 SID；存在活动命令时不能删除网络隔离。`--sandbox-status` 查看状态；`--sandbox-cleanup` 清理。失败时聊天和文件工具仍可用，Shell 拒绝执行。

管理员初始化和网络规则属于一次设置。日常执行不更改系统目录 ACL，不请求管理员权限。无法取得资源能力 ACL 时执行失败，不扩大到整个用户目录。

## 验收与支持声明

`.github/workflows/ci.yml` 的 native-sandbox 矩阵在 Linux、macOS、Windows 上运行真实进程；Windows job 明确完成管理员初始化及最终清理。启用 `WRIGHT_SANDBOX_INTEGRATION=1` 后，任何缺失的依赖或隔离能力都会导致验收失败，不跳过测试。Windows 以 junction 验证路径绕过；普通账户的符号链接限制只影响对应符号链接用例。

当前实现不能仅凭单元测试声明三平台 Shell 支持完成。必须看到三平台原生隔离 CI 全部通过，才能改变支持状态。本地 Linux WSL 已通过真实隔离验收；当前本地 Windows 尚未初始化隔离账户，macOS 无本地验收环境，Windows 与 macOS 验收仍待对应 CI 完成。

2026-10-03 本地验证记录：Linux 完整后端测试 881 项通过、10 项跳过；单独启用真实隔离验收后，Linux 8 项全部通过。前端 63 项单元测试、2 项 Playwright 流程通过，覆盖审批、撤销冲突刷新、窄屏和中英文交互。后端 lint、前端生产构建及两个离线 Agent 回归脚本通过。生产资源输出到实际服务目录 `src/wright/interfaces/web/static`。这些结果不替代 Windows 和 macOS 的真实进程验收。

2026-10-03 前端权限对齐后验证：权限相关后端 208 项通过、1 项跳过；前端 70 项单元测试及 7 项 Playwright 流程通过，lint、TypeScript 和生产构建通过。完整 WSL 后端运行 902 项通过、10 项跳过、3 项失败；进程恢复与检查点文件单独复测 22 项全部通过，包含原失败用例，未修改超时或恢复逻辑。没有将该次完整运行描述为全部通过。详情及截图见 `web/qa/permission-alignment-20261003/report.md`。

参考：[Bubblewrap](https://github.com/containers/bubblewrap)、[Seatbelt 实现](https://github.com/openai/codex/blob/main/codex-rs/sandboxing/src/seatbelt.rs)、[Windows 隔离工程](https://openai.com/index/building-codex-windows-sandbox/)。
