# 上下文压缩实施计划

权威需求：本目录 requirements.md。BASE_SHA 与唯一 MODULE_WORKTREE 见需求文档。module-developer 负责后端实现、自测与修复，主 agent 负责前端和集成，独立 module-reviewer 负责稳定版本审查；共享工作树，审查期间停止写入。

|任务|提供/消费接口|所有权|依赖|验收与验证|
|---|---|---|---|---|
|1 状态、策略与视图|CompressionState 随 ThreadState/SubagentTask JSON 保存；策略校验；原消息到活动视图/工具原子组|module-developer，backend/app/context_compression、domain|无|AC-2/3/4/7 单元测试、旧快照|
|2 Snip 与计数|SnipTool.bind(state, context)，现有 ToolExecutor；稳定 ID 与计数|module-developer，tools/snip.py、context_compression|1|AC-1/2/6 工具循环/失败注入|
|3 Auto-Compact 与预算|末位 prepare_model_messages；同模型 tools=[] 摘要调用；usage/max_output_tokens 适配器|module-developer，context_compression、model|1|AC-4/5/6 分块/结构/阈值/取消|
|4 主子集成与过程展示|Coordinator 最后挂中间件；独立子工具和压缩状态；model.status 增加 compacting 阶段|module-developer 后端；主 agent frontend hook/label|1-3|AC-7/8 集成、后端回归、前端检查|
|5 自查与独立审查|全部 BASE_SHA 差异、未跟踪交付文件、需求版本与测试证据|主 agent + 独立 reviewer|1-4|全部 AC，修复复审与最终验证|

设计合同：窗口/剩余/提醒使用 token，空闲使用秒，保留数使用工具结果条数。Message.id 定位消息，tool_call_id 仅在所属完整交互组中匹配。压缩元数据属于当前状态，不跨用户/Thread/主子上下文。先保存副本再更新内存，原始消息列表不变；关闭策略不撤销已保存的压缩状态。

进度：所有任务已完成。首审发现的 usage 校准问题已由原开发者修复并通过回归，原 reviewer 复审为 Spec Compliance PASS / Code Quality PASS / APPROVE。最终后端 924 passed、前端 126 tests/typecheck/build 通过；详情见 progress.md 与 review.md。未提交或部署。
