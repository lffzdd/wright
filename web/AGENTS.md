# Wright Web 前端

本目录是本地 Web 控制台的前端。通用规则见仓库根目录的 [AGENTS.md](../AGENTS.md)。

## 常用命令

在 `web/` 目录下运行：

```bash
npm ci
npm run lint       # ESLint
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
- **`eslint-suppressions.json` 只能变短。** 它记录了旧代码的存量违规。不要用 `--suppress-all`、`--suppress-rule` 或 `eslint-disable` 绕过新报错，改代码。修掉旧违规后运行 `npm run lint -- --prune-suppressions` 收紧基线。

## 代码结构

Lint 会检查：单行不超过 120 字符、不写嵌套三元、单文件不超过 400 行、hooks 依赖完整。下面几条 lint 查不出来，靠自觉：

- **一行只做一件事。** JSX 里超过两个分支，用提前 return、查表或拆小组件，不要堆 `&&` 和三元。
- **组件超过 300 行，或 `useState` 超过 6 个，先拆再加功能。** 相关状态合并成 `useReducer` 或自定义 hook。
- **数据请求和派生状态放 hook，组件只管渲染。** 不要在事件处理里拼一长串 `api.*` 调用和状态更新。
- **先找现成的再新写。** 下拉、菜单、分段控件、确认框、重命名用 `components/ui/` 里已有的。相似的 JSX 出现第三次就抽出来。
- **按钮只用 `.button`（可加 `primary`、`ghost`）、`.icon-button` 和 `.text-button`。** `.btn` 和 `.icon-btn` 是旧写法，新代码不要再用。
- **样式写进 `styles/` 下对应区域的 CSS 文件**，颜色、间距、字号用 `base.css` 里已有的变量，不新增硬编码颜色。静态样式不写内联 `style`，动态值通过 CSS 变量传入。不要在 `overrides.css` 或别的文件里给同一个元素叠覆盖样式。
- **所有界面文案走 `tr()`**，同时补齐 `i18n.ts` 里的中英文。

## 完成任务前

前端改动至少跑 `npm run lint`、`npm run test` 和 `npm run build`。
