# Wright Web 前端

本目录是本地 Web 控制台的前端。通用规则见仓库根目录的 [AGENTS.md](../AGENTS.md)。

## 常用命令

在 `web/` 目录下运行：

```bash
npm ci
npm run test       # 单元测试
npm run build      # 构建到 src/wright/interfaces/web/static
npm run test:e2e   # 浏览器交互测试
```

Web 端到端测试默认跳过，改到前后端交互时在仓库根目录运行：

```bash
WRIGHT_E2E_WEB=1 uv run --group e2e pytest tests/e2e_web
```

## 设计文档

- Changes 审阅流程的行为和验收标准：[docs/changes-contract.md](../docs/changes-contract.md)

## 必须遵守

- **不要手改 `src/wright/interfaces/web/static`。** 它由 `npm run build` 生成。改了前端就要重新构建并提交产物，CI 会检查。
- **不要提交过程产物。** QA 截图、对比图和一次性验证报告放在被忽略的 `web/qa/` 或 `web/test-results/`。

## 完成任务前

前端改动至少跑 `npm run test` 和 `npm run build`。
