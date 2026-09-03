# Thread Workspace 文件 API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让浏览器安全地上传、列出和下载 Thread Workspace 文件，上传结果能立即被 Agent 的 `read_file` 使用。

**Architecture:** 新增不依赖 FastAPI 的 `WorkspaceFileService` 处理路径边界、流式写入、大小限制和文件元数据；API 层只做用户归属检查、multipart 接收和 HTTP 错误映射。上传先写同目录临时文件再原子替换，列表和下载都跳过或拒绝符号链接。

**Tech Stack:** Python 3.12、FastAPI、python-multipart、标准库 pathlib/tempfile、pytest。

---

### Task 1: 写失败的文件 API 测试

**Files:**
- Create: `backend/tests/app/api/test_workspace_files.py`

- [x] 使用隔离 SQLite 和 `tmp_path/workspace` 创建 Thread。
- [x] 验证上传文件落在 workspace，并能列表和下载。
- [x] 验证嵌套输出文件能列表和下载。
- [x] 验证错误 user_id、路径穿越、越界符号链接被拒绝。
- [x] 验证超过 10 MiB 的上传返回 413，且不留下临时文件。
- [x] 运行测试，确认当前因路由不存在而失败。

### Task 2: 实现 WorkspaceFileService

**Files:**
- Create: `backend/app/services/workspace_file_service.py`

- [x] 定义 `WorkspaceFile` 元数据和明确的路径/大小异常。
- [x] `save_upload()` 校验根目录文件名，以 1 MiB 分块写入随机 staging 文件，超过限制时删除 staging。
- [x] 完成写入后使用 `os.replace()` 原子替换目标文件。
- [x] `list_files()` 递归返回普通文件，跳过 staging 文件和符号链接。
- [x] `resolve_download()` 解析相对路径并确认最终路径仍位于 workspace 内。

### Task 3: 接入 FastAPI

**Files:**
- Modify: `backend/app/api/schemas.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/requirements.txt`
- Modify: `backend/.env.example`

- [x] 新增 `WorkspaceFileResponse`。
- [x] 新增 `POST /api/threads/{thread_id}/files`，接收 multipart 的 `file` 字段。
- [x] 新增 `GET /api/threads/{thread_id}/files`。
- [x] 新增 `GET /api/threads/{thread_id}/files/{relative_path:path}`。
- [x] 将路径错误映射为 400、不存在映射为 404、超限映射为 413。
- [x] 增加 `python-multipart` 和 `DEER_MINI_MAX_UPLOAD_BYTES=10485760`。

### Task 4: 验证

**Files:**
- Verify only

- [x] 路由测试由失败变为通过。
- [x] 在临时后端副本运行查询、原子事务、Runtime 和 read_file 回归。
- [x] 执行 `compileall` 和 `git diff --check`。
- [x] 只读确认真实数据库 active Run 仍为 93；不启动正式 API，不提交 Git。
