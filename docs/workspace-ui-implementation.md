# 工作台实现记录

第一阶段交付能力和 Web 接口。第二阶段用选定设计替换默认 Web UI，并接上这些接口。设计稿只作产品参照，其中的演示脚本、SIMULATE、固定耗时、示例 PID 和示例 PR 编号没有被实现，也没有执行。

基线是当前工作区。没有 reset、clean、commit 或 push。设计文件没有修改。

## 能力清单

状态：A 已有完整能力；B 能力已有，本阶段补了 Web 投影或数据口径；C 本阶段新补的应用能力。结论按调用链，不按按钮是否存在。

| 设计区域 | 状态 | 代码入口与调用链 |
| --- | --- | --- |
| 顶栏项目登记、选择、切换 | C | `WorkspaceCatalog` → `infrastructure/persistence/workspace/registry.py`（`~/.wright/workspaces.json`）。`GET/POST /api/v1/workspaces`、`POST /workspaces/select`、`DELETE /workspaces/{id}`。选择只写登记项。`RuntimeManager.project_root` 和已打开会话的 `workspace_dir` 不变。注销不删除目录或任务库。 |
| 任务新建、恢复、关闭、归档 | A，多项目为 C | 原有 `POST /sessions`、close、archive 仍绑定启动项目的 `SessionDirectory`。其他项目走 `POST /workspaces/{id}/sessions`，句柄记录 `owner`，close/archive 用该目录。状态来自 `SessionService.summary`：`lifecycle`、`execution`、`agent_status` 分开。 |
| local / worktree、分支、变更摘要 | A + B | `SessionDirectory.open` 仍用 `ProjectContext` / `WorktreeManager`。`GET /sessions/{id}/workspace` 返回执行根、项目根、环境、`base_commit`、`branch_name`，以及 `summarize_git` 的分支和未提交数量。摘要不改进程 cwd。 |
| 文件浏览、引用、内容 | C | `files.resolve_inside` 拒绝绝对路径、`..` 和解析后越出根的符号链接。工作区树用登记根；会话树用该会话的 `workspace_dir`。 |
| 全局搜索 | C | `search_files`、`search_content`（`kind=content`）、`search_symbols`（`kind=symbol`）、`search_tasks`。符号来自 Python `ast` 或 js/ts/go/rs 的定义正则，不是正文检索。 |
| 执行时间线 | B | `timeline.project_timeline` 读 `message_records` 和 `tool_executions`。文本、工具、`execute_command`、`edit_file`/`write_file`、待处理交互分别投影。身份是消息 id、call id、request id。快照带 `timeline`，并保留原有 `stream_id` / `last_seq`。 |
| 计划、已访问文件、子 Agent | A + B | 计划仍是 `plan_manager.snapshot()`。`accessed_files` 来自工具参数里的路径。`subagents` 来自 `AgentControlPlane.tree()`，不用正文推断完成。 |
| Memory & Rules | A + B | 记忆走 `MemoryService`，项目 id 取会话自己的 `project_root`。核心画像只读；`human_profile` / `project_anchor` 可改。语义记忆和 episode 删除要 `confirm`。规则是 skill 文件，走 `write_skill` / `scan_skills`。`allowed-tools` 只是建议，不授予权限。覆盖和删除都要确认。 |
| 定时任务 | A + C | 创建、修改、暂停、恢复、删除走 `SchedulingService` 和会话上的共享 `AutonomyStore`。项目级 `GET /workspaces/{id}/schedules` 以及 `POST .../schedules/{id}/{pause\|resume\|update\|delete}` 直接读写项目任务库，会话关闭后状态仍在。有执行记录的删除保留 `cancelled` 行。 |
| 模型、Agent/Plan/Ask、权限策略 | A + C | 模型仍是 `POST /sessions/{id}/model`。`POST /sessions/{id}/policy` 在会话空闲且没有待回答交互时写入 `interaction_mode` 和 `permission_mode`，下一次权限决议生效，不改已经发出的调用授权。Ask 的操作集是读、内部读、网络读、用户交互；Plan 再加 `plan_update`。两者同时收紧工具名单。`bypass` 不能放宽这两层，拒绝规则和受保护路径仍然优先。 |
| 输入、图片、文件、@、/ | A + C | 图片仍走附件接口。非图片走 `POST /sessions/{id}/documents`，文件存在会话状态目录，提交时把文本内联进已接受的命令载荷。`GET /commands` 只列出 `dispatch_slash` 真正执行的命令。`@` 选择使用文件搜索和文件读取接口，组合输入框留给下一阶段。 |
| 权限请求、问答、授权管理 | A + C | 请求和回答仍是 websocket `interaction.respond`，带 `command_id`。`GET /sessions/{id}/grants` 列出会话规则、持久规则和额外目录，并写明剩余边界。撤销和新增都要 `confirm`，且会话必须空闲。单次授权是当次决议，不另存一条永久规则。 |
| Context | C | `classify_context` 按实际组装的消息、工具 schema 和输出预留分桶，各类相加等于估计总量。`kind=tokenizer_estimate`，`exact=false`。快照里的 `usage` 仍是供应方计费，并标了 `kind=provider_billing`。系统提示里的核心记忆只作标记，不重复计数。 |
| Changes、diff、Accept、Revert | A + C | 工具写入仍然立刻落盘。`GET /sessions/{id}/changes` 仍是只读 git diff。任务审阅是 `SessionChangeJournal`：Accept 只把 `review` 记为 `accepted`，不重写字节；Revert 用任务第一次改动前的字节恢复。外部修改造成哈希不一致时整批拒绝。失败时已恢复的文件写回 after 字节。不用 `git reset --hard` 或 `git clean`。 |
| 设置、语言、主题、检查器 | A + C | `GET/PUT /preferences` 保留界面语言。`theme`（`dark`/`light`）和 `inspector_open` 会写入并在当前客户端应用。空请求拒绝。不读写凭据。 |

设计稿里的窗口交通灯、SIMULATE 状态下拉、虚构用户、34ms 延迟、PR #342 和 PID 38291 没有对应的生产接口。没有伪造外部账号或 PR 连接。

## 业务语义

- 项目、会话、执行根、worktree 分开。UI 选中的项目 id 存在登记文件。并发任务各自持有打开时的执行根。
- 会话打开时，若 `permission_mode` 为空，冻结当时的权限设置模式。之后修改用户默认值，不改这个已打开会话。
- Agent 使用会话自己的权限模式。Plan 把模式强制为 `plan`。Ask 使用操作上限和工具名单。Full access 就是现有 `bypass`，仍然受拒绝规则、受保护路径，以及 Ask/Plan 上限约束。
- 策略、规则和目录授权的变更在下一次权限决议生效。正在执行的调用保留已经发出的授权。
- 变更归属：基线是会话打开时的 git 脏文件。任务写入进入 journal head。基线之后、没有 head 的变化是外部修改。二进制或超过 1MB 的内容标记为不可安全回退。
- 批量 Accept/Revert 先预检。任一条 blocked 则全部不落盘。回退中途失败时，把已经恢复的路径写回 after 字节，并返回 `applied: false`。
- 定时任务删除：先取消；没有 `durable_runs` 才删行；有历史则保留 `cancelled`。
- 记忆、规则、定时任务、授权的删除或覆盖都要求明确目标和 `confirm: true`。

## 接口契约

前缀都是 `/api/v1`，沿用现有 cookie。原有会话、模型、附件、git changes、websocket `turn.submit` / `turn.cancel` / `interaction.respond`、`stream_id` 和 `last_seq` 保持可用。`turn.submit` 增加可选 `document_ids`，缺省为空数组。

快照在原字段之外增加 `context_breakdown`、`timeline`、`subagents`、`accessed_files`。未知字段可以被旧客户端忽略。

审阅响应包含 `applied` 和逐条 `results`。`applied: false` 表示磁盘没有留下这批操作的结果。Accept 的文案是“已落盘编辑的审阅接受”。

偏好：`interface_language`、`theme`、`inspector_open`。至少要有一个字段。

## 测试

`tests/workspace/test_workspace_phase1.py` 覆盖：跨项目文件隔离和注销不删文件、符号搜索不同于正文搜索、路径和符号链接越界、Ask/Plan 在 bypass 下的上限、会话与持久授权撤销、目录授权、记忆和规则的项目隔离、定时任务重启后的暂停以及有历史时的保留删除、时间线身份和子 Agent 真实状态、Context 分类求和、变更冲突与批量失败回滚、文档不写入工作区、切换选中项目不改变执行根。

`tests/test_architecture_boundaries.py` 与上述用例一起通过。修改过的 Python 文件已用 Ruff 检查。前端 `npm test` 为 32 项通过，`npm run build` 通过。最终 `uv run pytest -q` 为 750 passed、2 skipped。

## 第一阶段结束时的限制

下面这条在第二阶段已经过时：当时 React 客户端还是旧布局。现在默认入口就是新工作台。
- 符号搜索是语法定义定位，不是语言服务器或全仓库语义索引。
- Context 分类是本地字符估算，不是账单。
- 任务回退只覆盖本任务 journal 里可逆的文本变更。任务开始前就脏的文件、外部改过的文件、二进制和超大文件会返回 blocked，而不是禁用功能。
- git changes 接口仍然是只读 diff。审阅状态在 journal，不在 git index。
- 输入框的 `@` 和 `/` 菜单已经接上文件搜索和 `GET /commands`。
- 没有外部账号时不显示 PR 或连接状态。
- 主题值已保存。第二阶段的客户端会读取并应用 `theme` 和 `inspector_open`。

## 第二阶段：默认界面

打开 Wright Web 看到的就是这套三栏工作台，没有单独的 demo 路由，也没有把旧界面留作默认入口。

组件职责：

- `web/src/App.tsx` 保留连接、快照、命令确认、断线重同步、每会话草稿和快捷键。⌘K 新建任务，⌘. 停止，Esc 关闭弹层和窄屏侧栏，不把 Esc 当成停止或拒绝。
- `web/src/workspace/widgets.tsx` 是任务栏、时间线、审批卡、输入框、检查器和新建会话对话框。测试仍从 `App.tsx` 引入这些组件。
- `web/src/workspace/panels.tsx` 是搜索、记忆、规则、定时任务和设置。它们使用同一套面板变量，不另做一套后台页。
- `web/src/styles.css` 提取设计里的颜色、边框、圆角和栏宽。没有引入 Tailwind CDN。Markdown 和 diff 继续用原来的变量别名。
- Vite 仍输出到 `src/wright/web/static`。没有手改打包产物。

时间线优先使用快照里的 `timeline`。工具调用插在发出它的那条助手消息后面，不再把全部工具堆到最终回答之后。快照没有时间线时，仍按原来的 history 加 active turn 渲染，保证旧事件流测试可读。实时里尚未进入时间线的工具和文本追加在后面。用户离开底部时不强制滚动，并提供回到底部。

审批按钮使用服务端返回的 `choices`。Accept 只提交审阅；Revert 先确认目标，并显示服务端的 `applied: false` 和冲突，不会再发一次覆盖。

窄于 1024px 时左右栏离开文档流。检查器默认不盖住中间栏，由打开按钮或 ⌘J 再展开。

## 第二阶段测试

- `web` 下 `npm test`：35 passed。
- `npm run build` 通过，产物写入 `src/wright/web/static`。
- `npm run test:e2e`：Playwright 2 passed。这组测试使用页面内的 fetch 和 WebSocket 替身，只证明前端协议和会话隔离，不证明后端接入。
- 修改过的 `timeline.py` 和 `workspace_routes.py` 已用 Ruff 检查。`tests/workspace/test_workspace_phase1.py` 与 `tests/test_web_server.py` 通过。
- 真实联调：在临时 `WRIGHT_HOME` 上启动当前 FastAPI，静态文件是这次构建。模型是本地替身，第一次补全请求 `write_file`，之后返回文本，不访问网络模型。浏览器在 `http://127.0.0.1:8765` 完成新建会话，并收到真实的 `waiting_for_input` 审批，选项来自后端。1440×960、1280×800、1024×768，以及浅色中文，都对照过同一条等待审批的任务。

## 仍未完成

- 符号搜索仍是语法定义定位，不是语言服务器。
- Context 仍是 `tokenizer_estimate`，不是账单。
- 任务回退只覆盖 journal 中可逆的文本。冲突由服务端拒绝。
- git changes 仍是只读 diff。非 git 目录会返回错误，而不是伪装成干净工作区。
- 侧栏里的记忆、规则、定时任务和设置是同一视觉语言的对话框，设计稿没有这些页面的逐像素稿。
- 审批按钮的长授权说明会换行。选项文本来自后端，没有改写成三个固定短按钮。
- 预览服务器使用模型替身。它证明了会话、权限、文件和 websocket 是真实服务，不证明某个外部模型供应商的回复质量。

## 第三阶段验收

每一项都按「功能 → 实现入口 → 验证方式 → 实际结果 → 剩余问题」记录。没有把前两阶段的“已完成”直接当成验收结论。

- 默认界面 → `web/src/App.tsx` 与 `src/wright/web/static` → 打开 FastAPI `http://127.0.0.1:8765/`，响应里的脚本是当次构建的 `index-DDBYxpsI.js` → 三栏工作台，不是旧布局，也不是独立 demo 路由 → 搜索、记忆、规则、定时任务对话框的字段标签仍是英文。
- 两个项目、切换、刷新恢复 → `WorkspaceCatalog`、`POST /workspaces/select`、会话列表 → `tests/workspace/test_workspace_phase1.py` 的登记、注销和执行根隔离；浏览器里刷新 `/?v=10` 后同一条空闲任务仍在 → 选择写入登记文件，不改已打开任务的执行根 → 这次浏览器只在预览项目里操作，没有在用户仓库里登记第二个项目。
- local / worktree、发指令、换模型、停止与恢复 → `SessionDirectory.open`、`POST /sessions/{id}/model`、websocket `turn.submit` / `turn.cancel` → 预览会话是 local；模型下拉是 `deterministic-preview`；pytest 覆盖 worktree 打开路径 → 浏览器发出的指令停在真实 `waiting_for_input`，允许一次后任务回到空闲并写出 `preview-note.txt` → 没有在浏览器里再走一遍 worktree 创建。
- 草稿、附件、工具、审批不串线 → `App.tsx` 按会话保存草稿；附件删除调用 `DELETE` 附件接口 → Playwright `two-sessions.spec.ts` 2 passed；`isolation.test.tsx` 4 passed → 切换会话不发送 `turn.cancel`，迟到的 changes 响应被丢掉 → 文档芯片移除只影响下一次提交，没有删除已保存文件的接口。
- 文本、工具、编辑、计划、子 Agent → `timeline.project_timeline`，前端 `Timeline` → 浏览器时间线顺序是用户文本、助手文本、`edit · write_file`、最终 “Preview turn completed.”；计划为空时显示“这一轮没有进行中的计划”，子代理为空时显示“没有子代理” → 审批解决后，reducer 会从时间线去掉已解决的审批，并把工具阶段改成 succeeded（`reducer.test.ts`） → 这次替身没有发出 shell、计划工具或子 Agent，所以这三类的实时卡片没有在浏览器里出现。
- 权限、拒绝、不同范围、撤销 → websocket `interaction.respond`，`GET/POST /sessions/{id}/grants` → 浏览器上的四个选项文案和范围来自后端；点击 Allow once 后服务端快照 `pending_interactions` 为 0；pytest 覆盖撤销和目录授权 → 按钮纵向排开，不再互相遮挡 → 没有在这次浏览器里点 Deny 或撤销一条已保存规则。
- Agent / Plan / Ask → `POST /sessions/{id}/policy`，`PermissionPolicy` → 下拉受会话字段控制；pytest 断言 Ask 在 bypass 下仍受操作上限；浏览器把模式显示为 Agent / 默认 → 忙碌或有待回答交互时策略接口返回 409，输入框显示失败原因 → 没有在浏览器里把这条正在等待的任务改成 Ask 再执行工具。
- @、/、搜索和跳转 → `GET /commands`、`search`、`GET /file` → 浏览器搜索 README，返回 1 条，点击后检查器切到文件并显示 `# Wright preview` → 符号搜索的浏览器点击没有单独再做一次；pytest 已区分符号和正文。
- Memory & Rules → `memory_view.py`、`rules.py` → pytest 覆盖项目隔离、确认删除 → 浏览器打开了入口，没有在预览项目里改用户的真实记忆 → 对话框标签仍是英文。
- 定时任务 → `schedule_api.py`，面板的 Edit 调用 `update` → pytest 覆盖暂停、恢复、有历史时的保留删除 → 创建和编辑不再要求把“没有会话就不能改”套到已有任务上 → 浏览器没有提交一条新的定时任务。
- Task / Files / Changes / Permissions / Context → 检查器五个页签 → 变更页列出 `README.md` 为 preexisting，以及任务写出的 `preview-note.txt`；上下文显示 `32292 / 200000`，分类相加等于估计值，并分开写 Billing 0 → 非 git 目录的 changes 仍是错误提示，不会写成工作区干净。
- 接受审阅与回退 → `SessionChangeJournal`，`POST /review/accept|revert` → 新增测试 `test_rename_of_a_clean_file_reverts_without_touching_other_work`：已提交文件被重命名后可以成对回退；把任务前就改过的 `kept.txt` 放进同一批时整批拒绝，该文件内容不变 → 回退基于 before 字节和当前哈希。冲突或 preexisting 不会覆盖 → 没有在用户仓库或预览仓库里点击回退。
- 任务前修改、外部修改、并发修改 → `preflight` → 同一测试里任务前脏文件被标成 preexisting 并挡住整批；既有用例覆盖外部哈希冲突和批量失败回滚 → 通过 → 无。
- 断线、缺口、命令确认、重复提交 → websocket `last_seq`、`commandAfter`、`protocol.test.ts` → 协议测试 3 passed，e2e 覆盖命令 id 重试 → 通过 → 没有在浏览器里拔网线。
- 语言、主题、设置 → `PUT /preferences` → 浏览器把主题从浅色改成深色，状态变为 Saved，刷新后仍是深色中文 → 设置对话框已翻译 → 搜索、记忆、规则、定时任务对话框未翻译。
- 空计划、无变更、未知用量、失败、取消、长内容 → 时间线空态、changes 错误、`tokenizer_estimate`、失败/取消文案 → 这条预览任务的计划和子代理是真实空态；用量估计与账单分开；失败和取消文案有 reducer 与组件测试 → 长输出折叠没有在这次浏览器里用超长工具结果再看一次。

架构：前端只提交后端给出的 choice id，不决定授权。路由仍调用既有服务。没有第二套记忆、调度、会话或权限存储。项目选择不改进程 cwd，也不改已打开任务的执行根。回退用记录的 before/after 哈希，冲突时 `applied: false`。Context 分类是估计，`usage` 是账单。CLI/TUI 代码没有为这个界面改行为；全量 pytest 包含原有 CLI 测试。

视觉：1440×960 深色中文下看过等待审批、变更列表和展开后的 `preview-note.txt` diff。1280×800 与 1024×768 看过同一条等待审批任务。1024 时左右栏离屏，停止和发送仍在中间栏。浅色中文也看过等待审批。审批长文案改为整行排列。展开 diff 改为盖住侧栏。对照的是选定设计的三栏密度、深色底、细边框和蓝紫强调色，没有改成另一套界面。

本阶段实际命令：

- `web` 下 `npm test`：36 passed。
- `npm run build`：通过。FastAPI 静态目录当前入口脚本是 `index-DDBYxpsI.js`，样式是 `index-Bas19Dq2.css`。
- `npm run test:e2e`：2 passed。这组仍是页面内替身，不代替上面的 FastAPI 浏览器验收。
- 仓库根目录 `uv run pytest -q`：751 passed，2 skipped。其中包含 `tests/test_architecture_boundaries.py` 和新增的重命名回退测试。
- `uv run ruff check src/wright/application/workspace/journal.py`：通过。

## 视觉移植

2026-09-30 更新：已删除独立的 `visual=fixture` / `visual=empty` 渲染分支及演示组件。下方旧记录仅作历史参考；当前验收以正常 `/` 入口、真实 FastAPI 会话与审批为准。生产构建统一写入 `src/wright/interfaces/web/static`，详见根目录 `design-qa.md` 和 `web/qa/production-*.png`。本次还修复了实时检查器数据不刷新、上下文条占比错误、回复重复与目录浏览失效。

呈现层按设计稿 DOM 重写，业务回调仍走原来的会话、审批和检查器接口。对照场景是 `?visual=fixture`，只替换视图，不写生产数据。

1440×960、`devicePixelRatio` 1、深色下量到的外框：顶栏 1440×38，左栏 x=0 宽 230，中间 x=230 宽 830，右栏 x=1060 宽 380。原稿同一组数字。审批卡片实测 768×216，原稿约 798×207。

本轮命令：

- `web` 下 `npm test`：36 passed。
- `npm run build`：通过。静态入口是 `index-DArJ1nrX.js`，样式是 `index-sohalY2x.css`。
- `npm run test:e2e`：2 passed。
- 没有改 Python，没有重跑 pytest。

真实联调：`http://127.0.0.1:8765/?v=14` 打开已有预览会话，中文界面。新建会话对话框能打开并关闭。变更页列出 `README.md` 和 `preview-note.txt`，接受与回退按钮仍在。1280×800 列为 200 / 780 / 300。1024×768 左右栏离屏，发送按钮仍在中间栏。浅色只改了页面 class，没有写入偏好。

对照图在 `docs/visual-compare/`。
