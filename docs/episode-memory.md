# Episode Memory

Episode 记录可供以后任务检索的执行经历。入库只保留事实，召回时再判断这条经历是否适用。

不新增入库 LLM 调用，不使用 embedding、向量数据库或新的外部服务。Core Memory 的更新和系统提示投影保持原有职责。自动提炼不会把助手推断写成已验证事实，也不会把历史结论提升进 Core Memory。

## 链路

回合结束 → 保存门槛 → 项目内 episode。需要时再走独立的提炼门槛 → 一次模型调用 → 来源校验 → 语义记忆。

当前任务 → 项目过滤 → 本地候选检索 → 有预算的候选摘要 → 一次 selector → 文本预算 → 上下文投影。

Episode 保存和语义提炼是两次决策。没有值得提炼的知识，仍然可以留下执行记录。

## 入库

- 有工具执行或子 Agent 活动时，成功、失败、取消和步数耗尽都保留。步数耗尽写成 `failed`，终止原因是 `max_steps`。旧记录里的 `max_steps` 状态仍可读取。
- 没有执行活动时，只在真实用户输入不是空白或纯寒暄、并且产生了非空交付回答时保存。
- 只读取真实用户输入。召回块、hook 文本和 runtime event 不参与判断。
- `outcome` 是实际交付回答。没有交付回答时留空，并保留终止原因，不编造总结。
- `usage` 只表示历史执行消耗，不参与召回文本预算，也不写进候选摘要。验证记录和执行状态分开保存。`completed` 不表示测试已经通过。工具 `ok` 也不表示目标达成或测试通过。

项目归属来自 Session 的稳定 `project_root`，使用已有的 `project_id(project_root)`。同一 `project_root` 下的工作树共享归属，不从 shell cwd、任务文本或工作树路径推断。

新记录版本号为 3，放在 `episodes/projects/<project_id>/`。每个项目按记录时间保留最近 500 条，相同时间再按 ID 稳定排序。文件 mtime 不参与排序。

版本 3 增加 `evidence`。每条引用包含来源类型、`session_id`、`root_run_id`、`run_id`，以及消息、工具调用或 step 的 ID。正文只保留有界摘要：命令、路径、退出码、错误摘录。参数里的凭据和工具 stdout 不复制进来。来源类型、执行状态、验证记录彼此独立。

旧版记录仍可读取。没有 `evidence` 表示当时没有登记来源，不会补写、不会猜测项目，也不会批量迁移。平铺的旧文件仍是项目未知，不进入自动召回。按已知 ID 仍可读取和删除。本轮不提供修改历史经历的接口。相同 episode ID 再次保存仍是幂等插入，不会改写已有记录。

有后台 Agent 时，先在 checkpoint 保存按原回合绑定的待收口快照，相关 Agent 都进入终态后再落成正式 episode。同一回合的 runtime continuation 可以补充结果。新用户回合开始后，旧任务完成只能更新旧快照。存储失败保留待处理项并记录失败类型，不影响任务交付。

## 证据读取

`get_episode` 默认不读 Session。只有 `include_evidence=true` 才按该 episode 已登记的引用读取。没有第二条证据工具。

读取会核对引用属于对应的 Session 和 Run。未登记的 ID、别的 Run、别的用户回合、非法 session id 都不会返回正文。一次请求里同一个 Session 只加载一次。正文有单条和总计上限，超出部分带 `…(已截断)`。

读取不执行工具，不恢复任务，不启动 Agent。原始 Session 缺失、损坏或来源对不上时，该条为 `unavailable` 或 `rejected`，episode 本身仍可读取。不会用模糊搜索找一条“看起来差不多”的消息代替。

## 提炼门槛

提炼门槛是纯规则，自身不调用模型。结果包含 `should_extract`、`reason_codes` 和可供引用的 `source_refs`。这些信号只表示“值得让模型看一眼”，模型可以返回空列表。

会尝试提炼的情况：

- 真实用户输入里有明确的长期说法，例如“记住”“以后只用”“from now on”“I prefer”。中英都覆盖。
- 同一次执行里先失败、随后有成功的工具结果或验证通过。

会跳过、且不调用模型的情况：

- `blank`、`trivial_user_text`：空输入或寒暄。
- `ephemeral_activity`：pwd、ls、天气这类没有持久信息的查看。多个同类调用也不会因为数量多就提炼。
- `no_durable_signal`：普通文件修改，或看不出持久信息。
- `failure_without_followup`：失败经历仍然保存，但失败本身不自动变成可复用教训。
- `deferred_background`：后台 Agent 尚未结束。稍后只用当时的快照提炼，不读取新回合 transcript。
- `already_recorded`：同一次 root run 已经有非 deferred 的提炼收据。
- `snapshot_missing_sources`：快照里没有可引用的来源 ID。旧快照不补造来源。

规则的边界：启发式会漏掉没有这些说法的隐含偏好，也可能因为“记住”出现在并不长期的句子里而多调用一次模型。它不把这种句子解释成已经理解了重要性。助手自己说“测试通过”不是提炼信号，也不是已验证事实。

收据写在 Session 的 `semantic_extract_receipts`，键是 `root_run_id`。已经记下的跳过、空结果、无效输出、成功或失败，重复收口不会再调用模型。进程在 checkpoint 之前崩溃时，同一次提取可能再跑一遍，这是 at-least-once，不是 exactly-once。失败状态不会在重复 finalize 时自动重试。

召回块、hook 和 runtime event 不能当作新的用户偏好。若后台结果迟到时无法安全使用旧快照，episode 仍保留，提炼记为跳过。

## 自动提炼的来源

送给模型的内容来自该回合快照，而不是当前 Session 的新 transcript。内容包括真实用户陈述、工具观察摘要、标成助手陈述的最终回答，以及已有语义记忆清单。总长度有上限，超出带截断标记。不回放整段原始 trace。

模型返回的 `source_refs` 必须是这次提供的 ID。应用层只检查格式和是否属于这组来源。引用存在只说明可追溯，不说明正文为真。编造的 ID 会被拒绝。

只有助手陈述支撑的结论不保存。`user` / `feedback` 必须引用用户陈述。`project` / `reference` 可以引用工具观察或验证记录。当前结构表达不了的不确定结论直接跳过，不写成确定事实。

语义记忆增加可选的 `origin` 和 `source_refs`。旧文件没有这两项时读成空，仍然可读。显式记忆工具不要求引用。自动路径必须带看来源。更新正文时如果没有一并给出新来源，旧来源会被清掉，避免正文和引用错位。本轮没有有效期、supersedes 图或知识版本历史。

写入走 `MemoryService.record_extracted`。显式工具仍走原来的存储接口。

提炼使用已有的 `OPENAI_MEMORY_MODEL`。未配置时复用主模型。这里不另加一套模型配置。

## 召回

自动召回只使用当前项目的 episode。扫描该项目全部保留记录，用无外部依赖的 BM25 排序。固定初值 `k1=1.2`、`b=0.75`。分词做 Unicode 归一化、英文大小写折叠、标识符拆分，以及中文单字和相邻双字。不要求查询里的每个词都命中。

BM25 索引和 selector 摘要使用同一组事实：错误、操作对象、验证问题，以及目标、结果、工具名。索引仍包含完整 `outcome`，所以结果中部的词仍可能被检索到。selector 看到的是有预算的摘要，中部句子可能不在摘要里。

字段权重：目标 3，完整结果 2，错误、验证问题和操作对象 2，工具名与子任务描述 1。排除 token usage。

候选是相关性前 40 条加上最近 10 条，去重后最多 50 条。分数字段是 `lexical_score`，只用于排序，不是相似度概率。相关性相同时，先看是否有结果、错误或验证信息，再按时间排序。失败经历不自动降权。

送给 selector 的 episode 清单还有 4096 个估算 token 的上限。这只限制这次 side request 的清单，不提高注入上限。50 条候选大约每条 320 个字符，够放下 ID、短目标、首尾结果和一条错误。超出预算的候选从已排序列表末尾丢掉。

长 `outcome` 在摘要里保留开头和结尾，中间标 `…(首尾截断)`。这是按长度切开，不是判断哪一句最重要。没有最终回答的失败 episode 仍会带上错误摘录。

Episode 与 Semantic Memory 共用一次 selector 请求。Episode 最多选 3 条，可以是 0 条。只接受候选集合内的字符串 ID。selector 报错或响应无效时，本轮不注入 episode，也不改用关键词结果。显式搜索仍然可用。Core Memory 和可读取的语义索引照常提供。

只读取最终 `ContentDone`。流式增量不和最终全文拼接。同一用户回合只选择一次。新用户回合重新选择。

## 预算与投影

默认总预算 800 个估算 token，单条最多 400 个估算 token，包含标题、来源和历史提示。应用层用现有 `estimate_tokens`（约 4 个字符一个 token）给渲染后的文本计价。按精选顺序渲染，优先缩减工具轨迹和结果正文。截断会标明，完整内容用 `get_episode` 读取。第一条也不能突破预算。预算为零时不注入。

请求投影只包含当前 user turn 的召回块。历史 transcript 保留原始记录。同一回合的后续模型调用和 runtime continuation 复用该块。新用户回合重新召回。模型总上下文不足时，先移除可选的 episode 注入，再沿用原有的上下文超限处理。

`search_episodes` 的 `scope` 可以是 `current_project`、`all_projects` 或 `legacy`，默认当前项目。跨项目和旧记录只能通过显式范围进入结果。

## 成本

计量沿用原来的 usage observer。流式 usage 只记最后一次累计快照。请求失败但已经收到 usage 时，仍保留这一次。没有 usage 是未知，不是 0。

Memory 侧另外记下三类事件，不把它们再加进总用量：

- extraction：提炼调用、跳过原因、耗时、模型名、usage、写入条数。gate 跳过是 0 次调用。模型返回空列表、请求失败、输出无效或来源校验失败，彼此分开。
- selection：召回精选的同一种记录。同一回合复用召回时不再记一次 selection。
- injection：这一次主模型请求实际带上的 episode。selector 选中、渲染出来、以及真正注入，是三组 ID。ContextBuilder 丢掉 episode 块时，注入 ID 为空，episode 估算 token 记 0。语义块不是可选块，仍按当次请求记录估算值。

注入 token 只是主请求输入的估算组成部分。主请求的 usage 已经包含它们，这里不再相加。同一回合多次发送可能多次计入输入长度。这里不估算缓存折扣或金额，也不写供应商价格。

计量失败只记日志，不打断回答，也不打断记忆写入。日志不记录原始提示、凭据或大段工具输出。

## 评测

固定标注集在 `src/wright/application/memory/eval_baseline.py`。离线命令：

```
uv run python scripts/eval_episode_memory.py
```

它不联网，不调用模型。输出包括 BM25 候选召回率、已知词面漏召回、没有相关候选时的词法结果和近期补充、gate 的调用与跳过、selector 清单和单条注入文本的估算大小。`model_quality` 在未加 `--live` 时固定是“未验证”。

已知漏召回保持为漏召回，用来决定以后要不要引入 embedding。词面不同、机制不同的经历，BM25 找不到时 selector 也看不到。近期补充可能把一条词法未命中的记录放进 selector 池，这不算候选召回命中。

真实模型入口：

```
uv run python scripts/eval_episode_memory.py --live
```

它使用现有模型配置，不打印密钥。普通 pytest 不走这条路径。只有实际跑过之后，输出里的 `model_quality.status` 才是 `ran`。脚本化 selector 的测试只证明协议和边界，不代表模型选择质量。

## 初值

40 条相关性候选、10 条近期补充、最多 3 条、800／400 估算 token、selector 清单 4096 估算 token，以及 BM25 的 `k1=1.2`、`b=0.75`，都是明确的初值，不是最优参数。

本轮没有做 embedding、向量库、lazy summarization、周期反思、跨 episode 因果归纳，或完整的知识版本系统。
