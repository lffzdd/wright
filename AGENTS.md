# Wright

Wright 是一个终端编程 Agent，提供 CLI、TUI、本地 Web 控制台和 headless 四种界面。
用法见 [README.md](README.md)。前端改动另见 [web/AGENTS.md](web/AGENTS.md)。

## 常用命令

```bash
uv sync --group dev --extra web   # 安装依赖；Web 相关测试需要 web extra
uv run pytest -q                  # Python 测试
uv run ruff check src tests       # Lint
uv run pytest -q tests/test_tui.py  # 只跑一个测试文件
uv run lint-imports --no-cache    # 分层检查
uv run python scripts/smoke_agent.py     # 离线运行时测试
uv run python scripts/smoke_subagent.py
```

以下测试默认跳过，改到对应部分时再跑：

```bash
WRIGHT_SANDBOX_INTEGRATION=1 uv run pytest -q tests/runtime/test_native_sandbox.py
```

## 设计文档

改动以下部分前，先读对应文档：

- 权限、审批、沙箱：[docs/permissions.md](docs/permissions.md)
- 工具调用协议：[docs/native-tool-calling.md](docs/native-tool-calling.md)
- 子 Agent 和多 Agent：[docs/multiagent-control-plane.md](docs/multiagent-control-plane.md)
- Agent 和 Shell 任务的统一控制入口：[docs/unified-task-runtime.md](docs/unified-task-runtime.md)
- 定时任务和调度：[docs/long-running-autonomy.md](docs/long-running-autonomy.md)
- 长期记忆：[docs/core-memory.md](docs/core-memory.md)、[docs/episode-memory.md](docs/episode-memory.md)、[docs/semantic-memory.md](docs/semantic-memory.md)
- Skills 和知识检索：[docs/skills-and-knowledge.md](docs/skills-and-knowledge.md)
- 生命周期钩子和 trace：[docs/lifecycle-hooks-and-traces.md](docs/lifecycle-hooks-and-traces.md)
- Windows 兼容和文件锁：[docs/windows.md](docs/windows.md)

## 分层规则

依赖方向由 `import-linter` 在 CI 中强制检查，规则在 `pyproject.toml`。

| 层 | 职责 | 不能依赖 |
|---|---|---|
| `interfaces` | 用户输入和输出的转换 | `infrastructure` |
| `application` | 编排业务流程 | `infrastructure`、`interfaces` |
| `domain` | 模型、规则、接口 | `application`、`infrastructure`、`interfaces` |
| `infrastructure` | LLM、存储、工具的具体实现 | `application`、`interfaces` |

- `core`、`base`、`common`、`utils` 放不含业务逻辑的通用代码，各层都可以用。
- `application/composition` 是例外：它负责把接口和实现接起来，可以引用 `infrastructure`。
- `application` 的子包之间不能有运行时循环，由 `tests/test_import_graph.py` 检查。

代码放哪里：

- 换一种界面就要改的，放 `interfaces`。
- 换一家 LLM 或存储就要改的，放 `infrastructure`。
- 其余的，流程放 `application`，规则和数据模型放 `domain`。

容易混淆的子包：

- `application/session_runtime`：各层共用的事件、发布器和运行时资源；`session`：高层编排。不要把高层代码放进 `session_runtime`。
- `application/agent`：单个 Agent 的回合循环和上下文组装；`session`：会话级的队列、分发和命令。
- `application/tool_execution`：一次工具调用（分发、审批）；`command`：shell 命令的执行和输出；`execution`：执行 ID、同一目录下工作的排队、进程内共享的运行时服务。
- `domain/model` 放数据模型，`domain/policy` 放规则（权限、上下文预算、护栏），`domain/gateway` 放给 `infrastructure` 实现的接口。
- `memory` 是长期记忆（core、episode、semantic）；`knowledge` 是文档 RAG 检索。
- `infrastructure/persistence` 存会话、记忆、任务运行记录等持久状态；`storage` 存附件、产物和 skills 文件。
- `infrastructure/runtime` 是命令执行后端和沙箱；`infrastructure/tools` 是各工具的具体实现。

## 必须遵守

- **不要往 `pyproject.toml` 的 `ignore_imports` 白名单里加新条目。** 它只能变短。遇到依赖问题，改为依赖 `domain/gateway` 的接口，或把共用定义移到 `domain`。
- **不要用函数内 import 绕开循环。** 先查清依赖方向为什么不对。可选依赖（如 `fastapi`）可以延迟 import，`TYPE_CHECKING` 只用于类型标注。
- **权限和沙箱的默认值不要顺手改。** `application/tool_execution` 里选择权限存储和执行后端的代码，以及 `domain/policy/permission`，改动前要先说明影响并等用户确认。失败时必须保持拒绝，不能放行。
- **不要提交或输出密钥。** API Key 只放在被 Git 忽略的 `.env` 里，新增配置项时同步更新 `.env.example`。
- **不要提交过程产物。** `docs/` 只放长期有效的设计说明，不放带基线和测试结果的实施记录。

## 代码风格

- 代码需兼容 Python 3.10，格式和规则以 `ruff` 为准。
- 注释跟随所在文件的语言和密度。

## 测试

- 已有对应子系统目录的（如 `tests/permission/`、`tests/memory/`），测试放进去；否则放在 `tests/` 根目录。
- 写测试前先看 `tests/conftest.py`、`tests/runtime_helpers.py` 和 `tests/permission_helpers.py` 里有没有现成的夹具。
- `tests/test_architecture*.py` 和 `tests/test_import_graph.py` 是架构约束，失败时改代码，不改测试。

## 完成任务前

1. 按改动范围运行上面的检查。Python 改动至少跑 `pytest`、`ruff` 和 `lint-imports`。
2. 行为改动要有测试，修 bug 时先写能复现问题的失败测试。
3. 如实汇报：说明跑了什么、结果如何、哪些没有验证。

测试失败时，先判断是测试过时还是代码有问题，不要为了通过而改断言。

## 协作方式

- 普通功能先给简短方案（目标、改动范围、完成标准），确认后再动手。不改变对外行为、不新增模块、改动集中在少数文件里的小修改，可以直接做。
- 不要做任务范围之外的重构或清理，发现问题就单独说明。
- 同一个问题修了两轮还没好，就停下来查根因，不要继续试。
- 只有用户要求时才提交或推送。提交信息用 Conventional Commits，例如 `refactor(agent): ...`。
