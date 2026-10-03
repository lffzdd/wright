# 前端权限策略与交互审查

审查日期：2026-10-02（用户时区）。对象：当前工作区代码及实际服务使用的静态资源。

结论：审批执行主链路接入了统一后端策略，授权边界有明确约束；权限展示、文件授权的一致性和操作反馈仍有缺口。开发者可以使用，但还不适合称为普通用户容易理解的完整体验。本次没有修改业务代码，也没有初始化系统隔离账户。

采用命令检索、Python 策略探针、Vitest 和 Playwright CLI。页面操作全部由脚本完成。重新构建到临时目录后，index.html、JS、CSS 与服务目录中的文件逐字节一致，因此问题不是前端旧包导致的。

已经对齐的部分：前端只提交服务端选项 ID；后端校验允许选项；单次审批不保存；文件范围和目录递归范围需要明确选择；项目长期授权为记忆默认，所有项目范围放在高级选项；Shell 只支持精确命令、cwd、隔离能力绑定的会话记忆，联网需要单次单独授权；审批及实际执行检查权限版本；撤销使用稳定 ID、来源和 expected_version，冲突返回 409，前端刷新后可以重试；硬模式、保护路径、拒绝和只读边界优先于明确授权。

| 优先级 | 问题及用户影响 | 证据与建议 |
| --- | --- | --- |
| P1 | 有效策略漏掉手动添加的文件／目录授权。授权真实生效，但该策略分组不列出对应规则；已保存授权列表仍有记录。 | resolver.py:226 只接受固定工具名，漏掉 tool_name=*。backend-probe.json 复现 file_write=allow，三个策略分组规则数全为 0。投影应识别通用资源规则并按操作分类。 |
| P1 | 项目授权被策略分组标成用户范围，造成“是否跨项目生效”的误解。 | resolver.py:207 将 settings 规则全部标为 persistent；widgets.tsx:653 将非 session 范围全部显示成用户范围。应保留真实 lifetime/source，并分别显示会话、项目、所有项目。 |
| P1 | 审批记忆与手动添加的文件权限含义不同。write_file 审批显示“仅此文件 · 读、写”，但保存规则绑定 write_file，后续 edit_file 仍要求审批；手动添加的同类授权使用 *。 | resolver.py:787、settings.py:462、grants.py:63；backend-probe.json 复现 write_file=allow、edit_file=ask。应统一资源授权语义，或在审批和授权卡上明确显示工具限制。工作区 read_file 默认放行，不能把其 allow 归因于该记忆。 |
| P2 | 写文件审批套用了命令样式，内容预览显示在“Agent 想运行”与 $ 后面，具体文件路径藏在范围列。 | cards.tsx:258 优先使用 preview，截图 07-real-write。按文件写入、文件编辑、Shell、HTTP 分别显示动作、目标和内容预览；大内容应有明显截断提示。 |
| P2 | 简单 echo hello 也显示 Medium，并列出可能写文件、修改 Git、删除文件，削弱风险信息的辨识度。 | 命令 descriptor 对所有 Shell 添加通用能力风险；cards.tsx:277 因 may_delete_files 包含 delete 而选择 Medium。截图 07-real-shell。通用能力说明与实际识别的危险行为应分开，风险等级由明确分类提供。 |
| P2 | 这台 Windows 的隔离状态为 setup_required，批准不代表命令可以运行；初始化入口只在权限管理页。 | wright --sandbox-status 返回 available=false。本次没有进行管理员初始化或真实隔离验收。Shell 审批卡应同步呈现隔离就绪状态，并提供明确的初始化入口与后续恢复提示。 |
| P2 | 联网勾选与记忆批准组合容易误解。勾选联网后点“保存并允许”，代码清空联网并发送会话离线记忆选项。 | cards.tsx:376。当前隔离边界保持保守，但没有明确解释该组合的执行结果。需要清楚区分本次联网与后续离线记忆，避免悄悄忽略已勾选能力。此项为代码确认，未单独动态执行网络组合。 |
| P3 | 冲突提示位于整张添加授权表单之后；中文页面仍显示 default、agent，风险等级硬编码英文，Windows 界面保留 Mac 快捷键符号。 | 截图 04-conflict、06-narrow-zh；permissions.tsx:25/末尾、widgets.tsx:645、cards.tsx:277。错误应靠近触发操作，明确“已刷新，请重试”；模式、等级和平台快捷键应本地化。 |

执行结果：

- 前端现有单元测试：63/63 通过。
- 权限后端专项：82 通过、1 跳过（包含在下面的扩大集合中，不重复计数）。
- 扩大的后端集合：186 通过、3 失败、1 跳过。两个 cwd 用例返回 command not found: cd；一个符号链接用例因 WinError 1314 缺少系统权限而失败。未将这些结果解释成完整 Windows 原生隔离支持。
- 自动化界面流程：4/4 通过，其中 2 个原有流程、2 个使用真实领域层生成的文件和 Shell 审批数据的新增捕获流程。截图捕获重新执行时同样 4/4 通过。
- 临时生产构建成功，三个服务文件 SHA-256 与新构建一致，详见 build-check.json。

复现命令：

```powershell
uv run pytest tests/permission -q
uv run pytest tests/permission tests/test_access.py tests/workspace/test_workspace_phase1.py tests/test_backend_gaps.py -q
uv run wright --sandbox-status
cd web
npm.cmd test -- --reporter=dot
npm.cmd run test:e2e -- two-sessions.spec.ts
```

本次截图脚本保存在 audit-flow.spec.ts。临时放回 web/tests 后可用现有 Playwright 配置运行；审查结束时已移除临时测试入口。prompts.json 是领域层实际生成的审批数据，backend-probe.json 保存策略判定结果。

证据范围：界面流程使用 mock HTTP/WebSocket，验证前端渲染、交互、冲突刷新和会话切换；新增审批 payload 来自真实 Python 领域层，不代表实际系统 Shell 执行成功。没有调用模型、访问外部业务服务或改变系统账户。没有验证屏幕阅读器、完整键盘路径、颜色对比度或三平台原生隔离，不声明完整无障碍或安全验收。

审查步骤与截图：

1. 接收权限请求：流程正常；文件请求沿用了 Shell 表述，需要改善动作与目标识别。

![步骤 1：权限请求](C:/Project/Python/wright/web/qa/permission-audit-20261002/01-approval.png)

2. 展开记忆授权：默认精确文件、项目长期，所有项目隐藏在高级选项，方向正确；工具限制没有显示，展开后需要在聊天区继续滚动查看全部内容。

![步骤 2：记忆授权](C:/Project/Python/wright/web/qa/permission-audit-20261002/02-remember.png)

3. 管理保存的授权：列表能显示资源、读写能力和期限，并提供撤销；有效策略投影问题由 Python 探针确认，截图中的 mock effective_policy 为空，不作为该投影问题的动态证据。

![步骤 3：已保存授权](C:/Project/Python/wright/web/qa/permission-audit-20261002/03-grants.png)

4. 撤销发生版本冲突：自动刷新和再次撤销有效；错误离撤销按钮较远，缺少下一步说明。role=alert 提供错误语义，但可见位置仍应改善。

![步骤 4：撤销冲突](C:/Project/Python/wright/web/qa/permission-audit-20261002/04-conflict.png)

5. 窄屏与中文权限页：资源输入和授权选项可操作；模式名称保留原始枚举。仅检查权限管理页，未据此宣称整个窄屏审批体验通过。

![步骤 5：中文窄屏](C:/Project/Python/wright/web/qa/permission-audit-20261002/06-narrow-zh.png)

6. 渲染真实后端审批数据：文件 preview 被显示成运行内容；简单 Shell 具有 Medium 风险提示。外层时间线仍使用测试 fixture，图中的 FILE EDIT 标记不作为真实 Shell 时间线问题的证据。

![步骤 6a：真实文件审批 payload](C:/Project/Python/wright/web/qa/permission-audit-20261002/07-real-write.png)

![步骤 6b：真实 Shell 审批 payload](C:/Project/Python/wright/web/qa/permission-audit-20261002/07-real-shell.png)

同目录还保存 05-narrow-en、08-real-write-remember 和 08-real-shell-remember 三张补充截图，均在本次审查运行中捕获并检查。

建议先修复有效策略投影和文件记忆的工具语义，再改善 Shell 初始化提示、风险分类、预览及错误反馈。
