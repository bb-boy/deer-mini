# 上下文压缩进度

更新：2026-10-07（Asia/Shanghai）。状态：确认范围开发完成，验证与独立复审通过；尚未提交或部署。

## 固定基线与工作目录

- 原源码 tx:/home/pl/deer_mini 的干净 main；BASE_SHA 与当前 HEAD 均为 3eed4d358c494093dd978c9acd655d989a719e6d。线上源码、服务和数据未修改。
- 本地源码主仓库：/Users/jk/Documents/ChatGPT/de/.worktree/deer-mini-source。
- 唯一 MODULE_WORKTREE：/Users/jk/Documents/ChatGPT/de/.worktree/deer-mini-source/.worktree/context-compression；分支 context-compression。实现、自测、修复、复审均在该工作树完成。
- 权威 requirements.md SHA256：dc752df10012800e7e9339a587932395ea9ccb59e41812775413170108be4c8e。全程未改变确认需求。
- 最终后端 26 个修改/未跟踪文件组合 SHA256：8a7531b3fa587538b762879a3240904997a03133000d12e00ed695c7454b5e6a。算法见 developer-report.md。
- 未暂存、提交、推送、合并、部署或修改线上数据。后续如有新实现变更，需补齐受影响验证与审查。

## 交付范围

- AC-1/2：注册 Snip，由模型在普通循环按消息 ID 主动选择；约新增 10K token 仅提醒。保存后才应用，保留原始聊天、当前完整用户轮、系统信息与不可重复结果，工具调用及结果保持成组。
- AC-3：距离上次 API 尝试严格超过 60 分钟，清理可恢复工具正文，总体保留最近 5 项，当前用户轮额外保护；先保存原文，引用可读取，不重放工具。
- AC-4/5：默认 1,000,000 窗口剩余约 33,000 token 触发单独、无工具的九段交接摘要；校验摘要、分块输入、保存后生效。无 Context Collapse 或 90%/95% 档。
- AC-6/7：主/子独立持久化、模型 usage 保守校准、旧快照兼容；取消与持久化失败继续传递，过大保护内容明确停止，摘要不混入回答流。
- AC-8：前端 Snip 标签和主/子 compacting 过程展示已完成；后端回归、前端检查及独立审查通过。

## 角色记录

采用 module-development 的开发者与独立 reviewer 流程。用户明确授权本次开发子 Agent 继承当前模型，项目角色配置未修改。

- /root/context_backend（module-developer）负责 backend/**、developer-report.md、自测及 R1 修复；主 agent 负责前端、其余文档和集成。
- /root/context_review（module-reviewer）首次使用独立上下文，未参与实现；首审与复审均审查同一工作树的冻结版本。
- 原 custom study/glm-5.3-flash 出现连续供应商错误，两轮已确认 cancelled，worker 9ee73f92-ad15-4a82-9497-7a1b84c2ac4e 已关闭，成果保留。不得续接该 worker。

## 验证证据

后端执行目录为 MODULE_WORKTREE/backend，Python 3.12.14 独立 venv；完整命令 `.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q`。

|阶段|结果|证据|
|---|---|---|
|后端修改前基线|855 passed、14 skipped|/tmp/deer-context-baseline.log|
|首审前后端完整回归|913 passed、14 skipped|/tmp/deer-context-final-backend.log|
|R1 修复前最小回归|4 failed，均未阻止已知超限请求|/tmp/deer-context-review-r1-before.log|
|相同用例修复后|4 passed|/tmp/deer-context-review-r1-after.log|
|最终压缩定向回归|69 passed|/tmp/deer-context-review-r1-targeted.log；reviewer 另行独立执行同组测试|
|最终后端完整回归|924 passed、14 skipped，23.73 秒|/tmp/deer-context-review-r1-backend.log|
|前端基线/最终测试|125 / 126 tests passed|/tmp/deer-context-frontend-{baseline,tests}.log|
|前端类型检查/构建|typecheck、build 通过|/tmp/deer-context-frontend-{typecheck,build}.log|

前端正式检查使用 pnpm 10.30.3，依赖按锁文件安装，锁文件未变化。前端 3 个修改文件与首审验证时哈希一致，复用仍适用结果。后端唯一 warning 为依赖弃用提示；14 skipped 与基线一致；前端构建存在大 chunk 提示。没有虚构不存在的 lint/Ruff 检查。测试仅使用 mock 模型及临时文件/SQLite，未执行真实模型、线上数据或学习测试。

## 审查及最终状态

首审发现 1 项 Important：Snip/空闲清理清空旧请求基准时丢失同模型 usage 校准，后续可能绕过摘要触发线。已由原开发者修复：将校准独立持久化，覆盖 Snip、空闲清理、Compact、无 usage、SQLite 恢复与模型切换；4 项原失败用例及新增边界回归全部通过。

原 reviewer 复审结论：Spec Compliance PASS、Code Quality PASS、Recommendation APPROVE，无未解决问题。独立复现的请求本地估算 5,870、校准估算 11,740；修复后会进入摘要并在受保护内容仍过大时明确停止。详见 review.md。

首审快照 /tmp/deer-context-review-manifest.json、/tmp/deer-context-review.diff；R1 审查快照 /tmp/deer-context-review-r1-manifest.json、/tmp/deer-context-review-r1.diff。R1 审查覆盖 34 文件并核对前后哈希不变。审查通过后只更新本进度、计划状态及新增 review.md 结论记录，生产代码与测试保持审查版本。

已完成开发交付，无实现或验证待办。token 仍为结合 usage 的近似计量；真实供应商兼容性和摘要语义质量未通过真实模型验证。提交、合并和部署按用户后续授权执行。
