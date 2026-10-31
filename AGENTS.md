# Wright

Wright 是一个终端编程 Agent，提供 CLI、TUI、本地 Web 控制台和 headless 四种界面。
用法见 [README.md](README.md)，各子系统的设计见 [docs/](docs/)。

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
WRIGHT_E2E_WEB=1 uv run --group e2e pytest tests/e2e_web
WRIGHT_SANDBOX_INTEGRATION=1 uv run pytest -q tests/runtime/test_native_sandbox.py
```

前端在 `web/` 目录下运行：

```bash
npm ci
npm run test       # 单元测试
npm run build      # 构建到 src/wright/interfaces/web/static
npm run test:e2e   # 浏览器交互测试
```

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
- `session_runtime` 放各层共用的事件、发布器和运行时资源；`session` 放高层编排。不要把高层代码放进 `session_runtime`。

代码放哪里：

- 换一种界面就要改的，放 `interfaces`。
- 换一家 LLM 或存储就要改的，放 `infrastructure`。
- 其余的，流程放 `application`，规则和数据模型放 `domain`。

## 必须遵守

- **不要往 `pyproject.toml` 的 `ignore_imports` 白名单里加新条目。** 它只能变短。遇到依赖问题，改为依赖 `domain/gateway` 的接口，或把共用定义移到 `domain`。
- **不要用函数内 import 绕开循环。** 先查清依赖方向为什么不对。可选依赖（如 `fastapi`）可以延迟 import，`TYPE_CHECKING` 只用于类型标注。
- **权限和沙箱的默认值不要顺手改。** `application/tool_execution` 里选择权限存储和执行后端的代码，以及 `domain/policy/permission`，改动前要先说明影响并等用户确认。失败时必须保持拒绝，不能放行。
- **不要手改 `src/wright/interfaces/web/static`。** 它由 `npm run build` 生成。改了前端就要重新构建并提交产物，CI 会检查。
- **不要提交或输出密钥。** API Key 只放在被 Git 忽略的 `.env` 里，新增配置项时同步更新 `.env.example`。
- **新增模块或函数前，先搜索有没有现成的可以复用。**

## 代码风格

- 代码需兼容 Python 3.10，格式和规则以 `ruff` 为准。
- 注释跟随所在文件的语言和密度。

## 完成任务前

1. 按改动范围运行上面的检查。Python 改动至少跑 `pytest`、`ruff` 和 `lint-imports`；前端改动跑 `npm run test` 和 `npm run build`。
2. 行为改动要有测试，修 bug 时先写能复现问题的失败测试。
3. 如实汇报：说明跑了什么、结果如何、哪些没有验证。

测试失败时，先判断是测试过时还是代码有问题，不要为了通过而改断言。

## 协作方式

- 普通功能先给简短方案（目标、改动范围、完成标准），确认后再动手。小修改可以直接做。
- 不要做任务范围之外的重构或清理，发现问题就单独说明。
- 同一个问题修了两轮还没好，就停下来查根因，不要继续试。
- 只有用户要求时才提交或推送。提交信息用 Conventional Commits，例如 `refactor(agent): ...`。
