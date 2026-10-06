# 公共存储与记忆恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 已验证并在 SQLite 登记的记忆保存项跨重启继续保存，公共存储错误明确表达提交边界。

**Architecture:** 文件公共层只报告技术事实；MemoryStore 在用户锁内执行稳定操作和版本比较；SQLite Repository 登记任务、保存项、尝试历史。后台调度以持久绝对时间控制恢复，不调用模型；管理命令限定用户范围。保持单进程服务、现有 Runtime 边界和聊天协议。

**Tech Stack:** Python 3.12, SQLite, FastAPI, pytest；不修改前端。

开发基线：远端 main d673fe0。独立分支 storage-foundations；本地开发、远端独立工作树验证，不合并、不部署。原设计目录的未提交文件完整保留。

### 1. 公共存储（独立文件职责）
- [x] 先增加 `backend/tests/app/storage/test_errors.py`、`test_file_io.py`，验证替换前失败为未提交、目录同步失败为不确定、硬链接/符号链接/越界/读写上限。
- [x] 新建 `backend/app/storage/errors.py`，`StorageError` 提供 backend/operation/category/stage/commit_state/recovery 及安全字段；保留原因链。取消和 StatePersistenceError 不包装。
- [x] 新建 `backend/app/storage/file_io.py`，提供以目录 fd 为边界的 bounded read、atomic write 和流式写入；不做隐式重试。
- [x] 适配 `filesystem/tool_result_store.py`、`services/workspace_file_service.py` 和 `infrastructure/database.py`；SQLite 仍使用原 Repository，不吞没完整性冲突的业务语义。
- [x] 在远端隔离目录运行公共存储、附件、工具结果、Repository 和 Runtime 回归。

### 2. 记忆保存操作（稳定身份与版本）
- [x] 新增 `backend/tests/app/memory/test_operations.py`：正文成功索引失败、重复核实不刷新时间、新版本冲突、损坏正文保留、旧格式兼容。
- [x] `backend/app/memory/operations.py` 定义 `PreparedMemoryOperation`：operation_id, memory_id, expected_version, result_digest, record。记录需 JSON 序列化，结果携带稳定操作来源。
- [x] `MemoryStore.prepare(user_id, changes, source_thread_id, source_run_id)` 在用户锁内生成所有操作；`apply_operation(user_id, operation, *, allow_write=True)` 比较旧版本与操作身份后写正文和索引；只核实模式不得启动新内容写入。
- [x] `MemoryExtractor.prepare(state, run_id)` 返回已校验操作；保留 `extract()` 作为现有调用兼容入口。运行生产服务时必须走 SQLite 登记。
- [x] MemoryStore 采用公共文件能力，损坏正文有安全日志，索引修复失败可观察。

### 3. SQLite 登记与恢复调度
- [x] 新增 `backend/tests/app/memory/test_save_repository.py`、`test_recovery.py`，用临时数据库、可控时钟和注入故障验证登记原子性、删除竞态、期限、部分成功和清理。
- [x] 新增 `backend/app/memory/task_schema.py` 和 `backend/app/repositories/memory_task_repository.py`，在数据库初始化中建任务/项/历史表，来源标识无级联外键；任务登记事务检查来源 Thread 所有权。
- [x] 新增 `backend/app/memory/recovery.py`：启动扫描、有限指数退避、用户写入串行、截止后只核实、不重放模型、记录每项结果；关闭等待同步提交结束。
- [x] `MemoryService` 抽取后先登记再调度，`RunCoordinator.start()` 启动恢复；`ThreadRepository.delete()` 同事务取消任务，阻止晚到登记。
- [x] 成功/取消项清除复制正文；只对全部解决且关闭满30天的任务清理历史；未解决不清理。

### 4. 后台管理与完整验证
- [x] 新增 `backend/app/memory/admin.py` 和 CLI 测试：list/show/retry/cancel/resolve-conflict/cleanup，所有任务操作要求 user_id，正文只有 show --include-content 才显示。
- [x] 新增 `backend/docs/memory-recovery.md` 写明入口、参数、状态、故障边界及单进程管理方式；更新设计状态。
- [x] 对照设计第12节逐项验收，独立规格和代码质量审查，修复后回归。
- [x] 完整后端回归：`.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py`，始终使用隔离绝对数据路径，不读写线上数据或调用真实模型。
- [x] 检查实际 diff 和 `git diff --check`；保留工作树供审阅，不合并、不部署。

## 测试执行

本地源码根：`/Users/jk/Documents/ChatGPT/de/work/storage-foundations`。
远端测试根：`/home/pl/.config/superpowers/worktrees/deer_mini/storage-foundations`。
使用远端测试根独立 `backend/.venv`（Python 3.12.13），只同步代码/测试/文档，不复制 .env、数据库或用户目录。每个测试通过 autouse fixture 使用独立临时数据库和用户根目录；专门的路径配置测试可以覆盖这些默认值。不要全局设置数据库环境变量覆盖旧测试的独立目录。例行回归排除真实模型测试，需 Docker 等外部资源的测试按原规则跳过。


## 完成记录（2026-10-05）

- 公共错误、安全文件读取/原子和流式写入、SQLite 分类均已实现；上传/工具结果与原 Runtime 边界完成适配。
- 登记任务、保存项、尝试历史、恢复调度、取消/冲突及内容保留已实现。恢复仅使用已登记的操作，模型调用发生在登记前。
- 管理入口：`python -m app.memory.admin --user USER ...`。默认隐藏正文；当前记忆和待保存内容分别使用显式详情选项。
- 规格与代码审查发现的问题已用回归测试验证修复，包括目录入口持久化、多个用户在途提交取消、登记失败后的下一轮核实、越过截止时间后的最终核实、日志原因链脱敏与并发同内容新增冲突。
- 全量回归：742 passed, 10 skipped, 1 existing LangSmith deprecation warning。命令：`.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q --tb=short`。
- `git diff --check` 与新增文件空白检查通过；没有前端行为或 SSE/API 成功响应类型修改。
- 远端线上主工作区仍为 `main@d673fe0`，工作区干净；未访问线上数据库或用户目录进行测试，未部署、未合并。实现保留在本地和远端 `storage-foundations` 开发工作树的未提交修改中。
