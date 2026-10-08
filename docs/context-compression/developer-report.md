# 后端开发交回记录（2026-10-07）

状态：DONE（仅代表分配的后端实现、自测及自查完成；整体集成与独立审查由主 agent 确认）。

## 范围与稳定版本

- MODULE_WORKTREE：`/Users/jk/Documents/ChatGPT/de/.worktree/deer-mini-source/.worktree/context-compression`。
- 分支：`context-compression`；BASE_SHA / 当前 HEAD：`3eed4d358c494093dd978c9acd655d989a719e6d`。
- 权威需求：本目录 `requirements.md`，SHA256 `dc752df10012800e7e9339a587932395ea9ccb59e41812775413170108be4c8e`，实现中未修改。
- 本开发者仅修改 `backend/**` 与本报告；保留并完善原有草稿，未修改 frontend、模型/provider/凭据配置，未暂存、提交、推送、合并或部署。
- 后端审查范围包含 26 个已修改/未跟踪文件。稳定内容摘要：`01889996dd0db4e9357a76c169752bf97ec92344079ebb99d406fdfbcbbc9e78`。算法：`git ls-files -m -o --exclude-standard backend` 去重排序后，对每个文件依次拼接 UTF-8 路径、NUL、文件内容、NUL，计算 SHA256。
- 本记录写入后停止后端与报告写入，等待主 agent 的独立审查反馈。

## 关键实现

- `app/context_compression/state.py` 提供独立、可恢复的主/子压缩元数据；通过状态副本持久化后才发布，取消等待在途保存完成，保存失败继续抛出 `StatePersistenceError`。
- `view.py` 按稳定 Message ID 处理完整工具组、逐段 Snip 边界与工具结果引用。当前用户轮、系统、Task/todos、未知工具和错误结果保护；重复 tool_call_id 仅在本组匹配。原始消息、reasoning 和 HTTP 历史不被该模块修改。
- `tools/snip.py` 经现有 Registry/Executor/错误中间件进入普通循环，低于提醒线也能主动调用；校验当前用户、Thread、Run、工作区和模型当前调用。Snip、提醒、Compact 重置增量计数，快照恢复后继续计数。
- `middleware.py` 在 memory/todos/workspace 注入之后计算真实请求视图。60 分钟严格超时后，候选总体保留最近 5 项，并额外保护当前用户轮；先保存/确认工具原结果，再保存引用元数据，不重放 Bash/Edit/Write。
- Auto-Compact 使用 1,000,000 / 剩余 33,000 默认阈值，输出严格九段 `<summary>`，禁止工具和任务执行，不发送摘要正文 SSE；继承旧摘要，按每次完整序列化请求预算分块，逐字符覆盖源资料，受保护信息无法容纳时明确停止。
- 摘要分块继承已知同模型 `max(1, usage/estimate)` 比率；后续摘要 usage 可进一步提高保守比率，包含前序摘要、JSON 转义、提示、输出预留和安全余量。压缩后的预算复核也使用已知保守比率。
- 主模型与摘要模型的进度上下文、空响应重试预算隔离；实际 API 每次尝试前保存时间。OpenAICompatible 采集有/无 choices 的 usage 流包，拒绝 bool/non-positive token；新请求前清空旧 usage。`max_output_tokens` 追加在旧 callback 形参之后，位置参数兼容，SDK 重试配置保持原值 0。
- policy 从 Coordinator 贯穿 SubagentMiddleware/Dispatcher/Executor；每个子 Agent 独立注册 Snip，成功保存后同步自身 `task.compression`。关闭策略仍应用历史摘要和裁剪，不复制父 Snip 实例。
- 未实现 Context Collapse；未新增成功 HTTP 响应字段。内部压缩字段仅进入持久化状态，既有 Pydantic API schema 继续过滤。

## 验证记录

所有后端命令都在上述 MODULE_WORKTREE 的 `backend/` 执行，使用已有 Python 3.12.14 `.venv`。模型为本地 mock，文件与 SQLite 均使用临时隔离 fixture；未访问真实模型或线上数据库。

|阶段|命令/证据|退出状态与结果|
|---|---|---|
|修改前基线（复用主 agent 记录）|`.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q`；`/tmp/deer-context-baseline.log`|0；855 passed，14 skipped，1 依赖弃用警告，20.22 秒|
|草稿问题复现|`.venv/bin/python -m pytest tests/app/context_compression/test_compression.py -q`|1；摘要成功路径明确触发 `UnboundLocalError: after_tokens`；同次另一个失败属于测试 mock 缺少 `close`，不是产品缺陷|
|测试环境调整|最初 async 标记使用了环境未安装的 pytest-asyncio；随后改为项目已有 anyio/asyncio backend|未安装依赖、未改运行配置；该次环境错误不当作目标缺陷证据|
|首轮常规回归|常规命令；`/tmp/deer-context-current.log`|1；12 failed / 865 passed / 14 skipped。失败均已定位为新增 Snip 工具表、额外 metadata Checkpoint 或内部序列化/API 区别导致的旧断言不匹配；无“历史失败”归因|
|针对性迭代|`.venv/bin/python -m pytest tests/app/context_compression -q`|最终该阶段 0；58 passed，1 依赖弃用警告，8.79 秒。最终回归覆盖之后的小范围预算/API 断言调整|
|涉及现有业务的定向回归|压缩目录 + web_search/web_fetch/subagent_executor 等已有测试|0；相应阶段 84 passed，1 依赖弃用警告，5.71 秒；之后最终常规回归再次覆盖|
|最终后端常规回归|`.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q`；`/tmp/deer-context-final-backend.log`|0；**913 passed，14 skipped，1 warning，31.70 秒**。855 项既有测试 + 58 项新增测试|
|差异自查|在 MODULE_WORKTREE 运行 `git diff --check`，查看全部 backend 跟踪差异以及新增实现/测试|0；无空白错误，无暂存文件；确认未修改线上或用户数据|

14 skipped 与基线同数；唯一 warning 为依赖 `langsmith.wrappers._openai_agents` 弃用提示。未执行真实模型 `tests/app/model/test_factory.py` 或学习测试。项目没有前端 lint script，后端未安装 Ruff，未把不存在的检查列为通过。

旧测试调整保留完整业务断言：工具表显式增加 Snip；Checkpoint 连续步数按新增的计数/尝试保存更新，并通过工具消息 ID 验证完整结果保存；API 用例显式保存非空压缩元数据，仍验证原始消息全量返回、内部字段过滤及跨用户 404。没有通过关闭压缩或删除边界断言掩盖问题。

前端不在本开发者验证范围；主 agent 已提供 126 tests、typecheck、build 通过记录，最终组合/独立审查由主 agent 核对。

## 验收对应与自查

|验收|实现与验证对应|
|---|---|
|AC-1|真实 LeadAgent → Snip → 下一轮模型请求；低于 10K 调用、提醒只注入请求、三种重置、JSON 恢复后的新消息计数|
|AC-2|完整工具组、跨轮重复 tool_call_id、非连续边界、当前/未知/系统/Task/todos/错误结果保护；原始 Message 与 reasoning 不变；工具错误与跨 Run 拒绝|
|AC-3|3599/3600/3601 秒边界，跨 Bash/read/write 候选总体留 5、当前轮额外保护，真实文件原文读取，缺失既有文件拒绝清理；引用状态随快照保存|
|AC-4|966999/967000/967001 默认触发边界、配置校验/开关、系统/工具定义/参数/reasoning 预算、模型切换校准、实际 usage 流包及无效值；3×usage 摘要完整分块预算|
|AC-5|九段结构/重复或空章节/超长输出/工具调用校验，旧摘要承接、当前用户与进度信息、转义/Unicode 全部源字符覆盖、保护内容仍超限停止、提交失败不发布摘要|
|AC-6|取消等待在途保存、保存失败保留原状态、摘要取消/保存异常不被状态清理覆盖、摘要重试不污染主进度、主流异常后已执行工具不重放、API 尝试逐次时间记录|
|AC-7|真实 Coordinator 的 memory/todo/workspace 最终预算，主子普通循环、并发子状态合并、子 Snip 拒绝父消息及开关独立性、SQLite 新旧 JSON 恢复，HTTP 原始聊天与 schema 兼容；既有 todos/memory/落盘/checkpoint 回归通过|
|AC-8|后端 `compacting` / 同 message_id `complete` 状态契约已覆盖；后端回归完成。前端与独立审查仍由主 agent 合并验证，不以开发者自查替代独立 review|

自查修复了摘要草稿未定义预算、成功后状态清理不可达、memory 注入顺序、子开关与实例共享、usage 在失败请求前重置、bool token 校验、摘要输入 JSON/usage 校准及输出预算等问题。移除了生产路径已不用的 `split_transcript`；完整性测试直接验证真实摘要分块。

没有已知未解决的后端缺陷。估算并非供应商专用 tokenizer 的精确计数；摘要语义质量仍由模型与已明确的提示契约共同决定，本任务未获授权使用真实服务验证。以上为已确认设计边界，不声称精确余量或真实供应商验收。独立审查尚未进行。

## 独立审查 R1 修复与复验（2026-10-07）

状态：DONE（该条反馈已实现修复并完成自测，交原 reviewer 复审；不自行宣布独立审查通过）。本节替代上次交回的稳定后端版本信息，之前的验证记录仍保留作历史证据。

审查反馈 Important：Snip 和空闲清理调用旧 `reset_usage` 时，清空 `last_prompt_tokens` / `last_request_estimate`，连同当前模型已知的低估校准一起丢失；Compact 完成后的相同重置路径也存在问题。后续本地估算低于阈值、但已知校准后超限的请求可能直接发送。这是本次实现缺陷，不能用“近似 tokenizer”解释为可接受误差。

先加用例后修复：在仍未修复的代码上，4 项新增用例全部因未抛出 `ContextBudgetExceeded` 而失败，确认测试能够捕获原缺陷。涵盖普通 LeadAgent 的 Snip 后下一次真实模型调用、idle 后下一次调用（含 JSON 恢复）、Compact 重置后新请求。修复后相同 4 项全部通过，Snip 场景仅发出最初一次模型请求，idle 场景至多尝试无工具摘要，绝不发送已知超限的普通请求。

修复内容：

- `CompressionState` 新增独立持久化的 `calibration_model` / `calibration_ratio`。比率必须是有限、至少为 1 的数值；未包含新字段的旧快照正常加载，可从尚存的同模型 usage/request estimate 推导。
- `budget.py` 提供统一的 `conservative_ratio`、`remember_calibration`、`record_usage`。同模型既有低估系数不会因为缺失 usage、较低的新观测或活动视图重置而降低；其他模型不会使用该系数。
- 原 `reset_usage` 改为语义明确的 `reset_request_baseline`，仅清空旧请求基准，并先保留旧快照中的已知校准。Snip、idle、Compact 三处都采用相同逻辑。
- 普通请求预算、摘要分块预算、Compact 后预算共同使用匹配当前模型的保守校准。成功摘要调用获得更高比率时，经现有持久化提交边界保存；不会用旧模型主请求基准覆盖新模型摘要的校准。
- 补充真实 SQLite 旧 JSON 恢复、重置后再写入/重读、同模型阻止请求与切换模型正常调用，以及摘要新模型 usage、后续缺失 usage、无效校准字段校验。

实际命令均在 MODULE_WORKTREE/backend 执行，模型/文件/SQLite 继续使用离线隔离 fixture：

|阶段|命令与日志|退出状态与结果|
|---|---|---|
|本次修复前可复用完整基线|上节最终回归 `/tmp/deer-context-final-backend.log`|0；913 passed、14 skipped、1 依赖弃用 warning|
|原缺陷失败证据|`.venv/bin/python -m pytest tests/app/context_compression/test_compression.py -q -k 'snip_keeps_known or idle_retains_calibration or compact_reset_keeps'`；`/tmp/deer-context-review-r1-before.log`|1；4 failed、38 deselected、1 warning，0.66 秒；四例均 `DID NOT RAISE ContextBudgetExceeded`|
|相同用例修复复验|同一条 `-k` 命令；`/tmp/deer-context-review-r1-after.log`|0；4 passed、38 deselected、1 warning，0.53 秒|
|压缩模块定向回归|`.venv/bin/python -m pytest tests/app/context_compression -q`；`/tmp/deer-context-review-r1-targeted.log`|0；69 passed、1 warning，5.52 秒；包括现有摘要流程与 usage 流包|
|最终后端完整回归|`.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q`；`/tmp/deer-context-review-r1-backend.log`|0；**924 passed、14 skipped、1 warning，23.73 秒**|
|差异检查|MODULE_WORKTREE：`git diff --check`、`git diff --cached --stat`|0；无空白错误、无暂存内容|

本次新增 11 项回归，总计 69 项新增压缩测试；855 项原始基线测试也全部通过。14 skipped 和依赖弃用 warning 与此前一致；没有新环境阻塞、没有真实服务验证或部署。AC-4、AC-5、AC-7 的校准与恢复覆盖得到补全，其他验收项的回归全部通过。

最终稳定后端内容摘要（仍为 26 个后端变更文件，算法同上）：`8a7531b3fa587538b762879a3240904997a03133000d12e00ed695c7454b5e6a`。需求 SHA256 保持 `dc752df10012800e7e9339a587932395ea9ccb59e41812775413170108be4c8e`，HEAD/分支未改变。没有修改 frontend 或需求，也未暂存、提交、推送、合并、部署。本报告追加完成后再次冻结 backend 与报告写入，等待原 reviewer 复审。
