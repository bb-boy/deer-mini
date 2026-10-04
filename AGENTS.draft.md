# AGENTS.md

## 1. 项目概览（Project Overview）

- deer-mini 是参考 DeerFlow 实现的轻量级 AI Agent 项目，提供对话、工具调用、子任务、附件处理和运行过程展示。
- 技术栈：Python 3.12 + FastAPI + SQLite；前端使用 React 19 + TypeScript + Vite + Tailwind CSS。
- 远端部署在 `tx:/home/pl/deer_mini`，由 systemd 管理 FastAPI 后端和 Caddy 前端服务，Caddy 同时代理 `/api` 请求。

## 2. 常用命令（Commands）

以下后端命令在 `backend/` 执行，前端命令在 `frontend/` 执行；安装依赖和开发启动使用独立开发环境。

- 后端安装依赖：`.venv/bin/python -m pip install -r requirements.txt`（已有 Python 3.12 虚拟环境）。
- 后端启动开发：`.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8005 --reload`。
- 后端日常回归：`.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py`。
- 前端安装依赖：`pnpm install --frozen-lockfile`（使用 `pnpm 10.30.3`）。
- 前端启动开发：`pnpm dev`；类型检查：`pnpm typecheck`；测试：`pnpm test`；构建：`pnpm build`。
- Lint：目前没有前端 `lint` 脚本，后端虚拟环境也未安装 Ruff，不要把不存在的命令写成必过检查。
- 查看线上日志：`journalctl -u deer-mini-backend.service -n 100 --no-pager`；前端使用 `deer-mini-frontend.service`。

## 3. 架构（Architecture）

- 前端页面和组件在 `frontend/src/`，API 客户端在 `frontend/src/api/`，对话运行及 SSE 状态由 `frontend/src/hooks/` 管理。
- 后端路由在 `backend/app/api/`；`services/` 组织业务，`runtime/` 管理 Run 生命周期，`agents/` 执行模型与工具循环，`tools/` 和 `subagents/` 提供具体能力。
- SQLite 表结构在 `backend/app/infrastructure/database.py`，读写封装在 `repositories/`；详细机制见 `backend/docs/hooks.md`、`backend/docs/model-error-handling.md`、`backend/docs/tool-results.md` 和 `docs/thread-file-flow.md`。

## 4. 编码约定（Conventions）

- Python 模块和函数使用 `snake_case`，类使用 `PascalCase`；新代码保持类型注解，复杂模块用中文 docstring 说明用途、输入输出和副作用。
- 前端业务组件通常使用 `PascalCase.tsx`，Hook 使用 `useXxx.ts`；现有 `ui/`、`ai-elements/` 等目录沿用各自命名，不批量改名。
- API 正常响应沿用现有 Pydantic schema，HTTP 错误使用 `HTTPException` 和 `detail`；前后端类型、事件字段同步修改。
- 工具注册到 `ToolRegistry`，通过中间件包裹的 `ToolExecutor` 统一执行；日志、错误分类、重试和大结果落盘复用公共中间件。
- 普通工具错误返回带 `is_error` 的工具消息；模型最终失败、取消和关键状态保存失败按现有 Runtime 边界向外传播。
- 测试放在现有测试目录，默认使用模拟模型和临时数据库、文件；提交前检查实际 diff，不混入无关修改。

### Git 约定

- 开工先执行 `git status --short`、`git branch --show-current` 和 `git diff`；有暂存内容时同时检查 `git diff --cached`，确认已有修改与本次任务的边界。
- 新开发分支使用 `codex/<任务简述>`；在线服务使用的目录不直接切换分支，开发优先使用独立 worktree 或隔离副本。
- 新 worktree 不会自动包含原目录的未提交修改；开发前核对代码基线，保留原目录中的修改，不擅自 stash、覆盖或丢弃。
- 提交信息沿用现有 `type: description` 风格，type 使用 `feat`、`fix`、`docs`、`refactor`、`test`、`chore`，描述清楚本次改变。
- 一个提交对应一个可说明、可回退的改动；功能、无关格式化和其他任务的修改分开提交。
- 用明确文件路径或 `git add -p` 暂存本次修改；存在其他工作时不使用 `git add .` 或 `git add -A`。提交前检查 `git diff --cached` 和 `git diff --cached --check`。
- 提交前完成与改动相关的检查；交付时说明改了什么、验证结果和未完成事项。提交、推送和合并按用户本次任务的授权执行。

## 5. 硬性约束（Hard Constraints）

- 不把 `.env`、API Key、数据库、运行日志或用户上传资料加入 Git；不在日志、文档或聊天中输出密钥和完整供应商错误正文。
- 未经明确授权，不删除、重置或直接修改线上 SQLite 和用户文件；自动回归测试不得使用线上数据库、用户目录或真实模型服务。
- Thread、Run、附件和事件接口必须校验用户归属；新增接口沿用 `_require_owned_thread()`、`_require_owned_run()` 或等效检查。
- 文件访问必须经过当前 Thread 的路径解析和边界检查；不绕过目录隔离、符号链接检查和上传大小限制。
- 不吞掉 `asyncio.CancelledError` 或 `StatePersistenceError`，不把保存失败、超时和中断标成成功；状态确认保存后才能宣告完成。
- 模型请求重试不得重放已经执行的工具；已经输出正文或思考后，不自动从头生成并拼接失败片段；SDK 重试保持 `max_retries=0`，由中间件控制次数。
- 当前运行时按单进程设计，未经架构调整不启用多个 Uvicorn worker；在线服务、Caddy 配置和发布目录的变更必须属于用户明确授权的部署工作。
- 未经明确授权，不执行 `git reset --hard`、`git clean -f`、覆盖已有修改的恢复操作、强制推送、删除分支或改写已有提交历史。

## 6. 易踩的坑（Gotchas）

- 在 `tx` 的 SSH 非交互会话中，`pnpm` 可能不在 PATH；使用 `/opt/deer-mini-runtime/bin/pnpm`。后端使用项目 `.venv/bin/python`，不要直接使用系统 Python。
- 后端启动时自动初始化 SQLite，并收尾遗留的活跃 Run；调试实例必须使用独立数据。通过绝对路径 `DEER_MINI_DATABASE_PATH` 和 `DEER_MINI_DATA_ROOT` 配置隔离的数据库及用户目录。
- `tx` 的 `8005`、`5175` 已有线上服务；同机调试使用其他端口，并通过 `DEER_MINI_API_PROXY_TARGET` 指向调试后端。不要再启动一份开发服务占用线上端口。
- 线上前端由 Caddy 读取 `frontend/releases/` 下的指定发布目录；仅运行 `pnpm build` 不会更新线上页面，部署时需核对 Caddy 实际指向。
- `tests/app/model/test_factory.py` 会请求真实模型，`tests/learning/` 是独立学习测试；日常回归命令不包含它们。真实供应商验证应单独执行。
- Bash 工具依赖 Docker 和显式开关；网页工具依赖 `TAVILY_API_KEY`。未启用或未配置时，工具不会出现在模型工具列表中。
- `write_todos` 只保存完整任务清单，只有主 Agent 可调用；修改计划由模型决定。未完成清单最多触发两次继续提醒，不是独立的 Replanner，也不保证任务最终完成。
- `uploads/`、`workspace/`、`outputs/` 是当前 Thread 的三个文件区域；Bash 默认从 `/mnt/user-data/workspace` 执行，交付文件写入 `/mnt/user-data/outputs/`。
- `user_id` 归属检查不等于登录认证；当前代码没有 `requireAuth()` 中间件，不要把它描述成已经具备的能力。
