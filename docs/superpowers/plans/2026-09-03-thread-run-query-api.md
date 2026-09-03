# Thread、Run 与状态查询 API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让浏览器能在重载后读取用户的 Thread 列表、Run 列表、最新 ThreadState 和 Checkpoint 历史。

**Architecture:** Repository 增加受 `user_id` 限制的分页查询；API schema 把 dataclass 状态转换成稳定 JSON；FastAPI 路由只负责参数校验、归属校验和 404 映射。所有接口只读 SQLite，不启动 Agent。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLite、pytest。

---

### Task 1: 路由失败测试

**Files:**
- Create: `backend/tests/app/api/test_query_routes.py`

- [x] 创建隔离数据库和不带 lifespan 的 FastAPI 测试应用。
- [x] 验证 Thread 列表分页、用户隔离、单 Thread 查询。
- [x] 验证 Run 列表按创建时间倒序并限制 Thread 归属。
- [x] 验证最新 Checkpoint 和指定 Run 的 Checkpoint 历史。
- [x] 运行测试并确认因路由尚不存在而失败。

### Task 2: Repository 查询

**Files:**
- Modify: `backend/app/repositories/thread_repository.py`
- Modify: `backend/app/repositories/run_repository.py`

- [x] 实现 `ThreadRepository.list_for_user(user_id, limit, offset)`，按 `updated_at DESC, created_at DESC` 返回。
- [x] 实现 `RunRepository.list_for_thread(thread_id, user_id, limit, offset)`，按 `created_at DESC` 返回。
- [x] 所有 SQL 同时包含用户归属条件，不能只凭可猜测的 ID 读取数据。

### Task 3: 响应模型和路由

**Files:**
- Modify: `backend/app/api/schemas.py`
- Modify: `backend/app/api/routes.py`

- [x] 新增 ToolCall、Message、ThreadState 和 Checkpoint 响应模型。
- [x] 新增 `GET /api/threads` 与 `GET /api/threads/{thread_id}`。
- [x] 新增 `GET /api/threads/{thread_id}/runs`。
- [x] 新增 `GET /api/threads/{thread_id}/state`。
- [x] 新增 `GET /api/threads/{thread_id}/runs/{run_id}/checkpoints`。
- [x] 列表限制为 1 到 100 条，默认 50 条；不存在或不属于用户时返回 404。

### Task 4: 验证

**Files:**
- Verify only

- [x] 运行路由测试和 Repository/Service 回归测试。
- [x] 在临时后端副本初始化 SQLite 后运行相关 Runtime 测试。
- [x] 执行 `compileall`、`git diff --check`，并确认真实数据库 active Run 仍为 93。
- [x] 不创建 Git 提交。
