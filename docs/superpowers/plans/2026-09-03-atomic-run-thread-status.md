# Run 与 Thread 状态原子更新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用一个 SQLite 事务同时更新 Run 与 Thread，消除两张表出现不一致状态的可能。

**Architecture:** 新增 `RunLifecycleRepository`，只负责跨 `runs`、`threads` 两张表的生命周期事务；`RunRepository` 继续负责 Run 的普通增删查改，`RunService` 负责业务入口和不存在记录的错误语义。每次转换使用 `BEGIN IMMEDIATE`，Run 条件更新失败返回 `False`，Thread 更新失败抛出异常并由连接上下文自动回滚。

**Tech Stack:** Python 3.12、标准库 `sqlite3`、pytest、SQLite。

---

### Task 1: 用回归测试证明当前存在部分提交

**Files:**
- Create: `backend/tests/app/repositories/test_run_lifecycle_repository.py`

- [x] **Step 1: 写隔离数据库测试夹具**

  使用 `monkeypatch` 将 `app.infrastructure.database.DATABASE_PATH` 指向 `tmp_path`，调用 `initialize_database()`，确保不读取或修改真实 `backend/data/deer_mini.db`。

- [x] **Step 2: 写失败注入测试**

  在 SQLite 中创建 `BEFORE UPDATE OF status ON threads` 触发器并执行 `SELECT RAISE(ABORT, ...)`。分别验证启动和完成流程：Thread 更新失败时，Run 必须仍保持转换前状态。

- [x] **Step 3: 验证测试先失败**

  Run: `PYTHONPATH=. .venv/bin/pytest -q tests/app/repositories/test_run_lifecycle_repository.py`

  Expected: 回滚断言失败；当前实现已经单独提交 Run 更新。

### Task 2: 实现跨表事务 Repository

**Files:**
- Create: `backend/app/repositories/run_lifecycle_repository.py`

- [x] **Step 1: 提供四个生命周期入口**

  实现以下签名：

  ```python
  start(run_id: str, user_id: str) -> bool
  finish(run_id: str, user_id: str, status: str, error: str | None = None) -> bool
  interrupt(run_id: str, user_id: str, error: str | None = None) -> bool
  recover_orphan(run_id: str, user_id: str, error: str) -> bool
  ```

- [x] **Step 2: 每个入口在同一事务内修改两张表**

  每次先执行 `BEGIN IMMEDIATE`，读取目标 Run 的 `thread_id`，带来源状态条件更新 Run，再更新同一用户的 Thread。Thread 更新行数不是 1 时抛出 `RuntimeError`，让连接上下文执行 `ROLLBACK`。

- [x] **Step 3: 保留条件状态转换**

  `start` 只接受 `pending`；`finish` 只接受 `running` 且目标只能是 `success/error/timeout`；`interrupt` 与 `recover_orphan` 只接受 `pending/running`。

### Task 3: 让 RunService 使用事务入口

**Files:**
- Modify: `backend/app/services/run_service.py`

- [x] **Step 1: 注入生命周期 Repository**

  `RunService.__init__` 增加可选的 `RunLifecycleRepository`，保留现有 `RunRepository` 与 `ThreadRepository` 供创建和查询使用。

- [x] **Step 2: 替换四个分步更新**

  `start_run`、`finish_run`、`interrupt_run`、`recover_orphaned_run` 委托给生命周期 Repository；保留原有的不存在 Run 行为和布尔返回值。

- [x] **Step 3: 验证测试变绿**

  Run: `PYTHONPATH=. .venv/bin/pytest -q tests/app/repositories/test_run_lifecycle_repository.py tests/app/services/test_run_service.py`

  Expected: 所有测试通过。

### Task 4: 扩大验证范围

**Files:**
- Verify only

- [x] **Step 1: 验证 Runtime 和 Coordinator 调用兼容**

  Run: `PYTHONPATH=. .venv/bin/pytest -q tests/app/services tests/app/runtime/test_agent_runtime.py tests/app/runtime/test_event_recorder.py`

- [x] **Step 2: 静态检查**

  Run: `.venv/bin/python -m compileall -q app tests/app/repositories/test_run_lifecycle_repository.py`

- [x] **Step 3: 检查改动和空白错误**

  Run: `git diff --check && git status --short`

  不启动 `app.main`，以免启动恢复流程修改真实数据库中的历史 Run；不创建 Git 提交。
