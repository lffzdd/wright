# Windows 启动与文件锁

原生 Windows 的 CLI 帮助命令和 Web 服务可以启动：

```powershell
uv sync --extra web
uv run wright --help
uv run wright --ui web --no-open
```

配置、项目注册表、核心记忆、语义记忆、episode 和 ApplicationHost
统一使用 `wright.infrastructure.file_lock.FileLock`，通过 `portalocker`
获取操作系统级排他锁。项目代码不直接导入 `fcntl`。

- 存储写入等待其他进程释放锁，没有固定的等待超时。
- ApplicationHost 非阻塞获取锁，同一执行目录的第二个实例立即报错。
- 正常退出、异常退出和进程被终止后，操作系统释放锁。
- 获取失败或等待被中断时关闭文件句柄；释放后保留锁文件。
- 原有线程锁和临时文件加 `os.replace` 的写入方式继续使用。

Windows 不导入 iTerm 专用的 POSIX 终端驱动；macOS 的 `libproc`
推迟到进程管理功能实际使用时加载。

Windows 11 的 Shell 改用原生 PowerShell、低权限账户、受限令牌、WFP 和
Job Object，初始化前明确拒绝执行。首次运行 `uv run wright --sandbox-setup`
或在权限页初始化；`--sandbox-status` 查看状态，`--sandbox-cleanup` 清理。
日常命令不会自动请求管理员权限，也不会降级到无隔离进程。

三平台原生隔离 CI 全部通过之前，Shell 支持仍处于待验收状态。
完整权限语义、存储格式和验收入口见 [权限说明](permissions.md)。

`tests/test_file_lock.py` 验证真实进程互斥、等待释放、崩溃释放和异常清理。
`tests/test_startup.py` 在独立解释器中验证 CLI 帮助命令及 Web 首页响应，
使用临时 `WRIGHT_HOME`，不发起模型请求。
