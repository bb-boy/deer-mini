# AGENTS.md

## 1. Project Overview

- deer-mini is a lightweight AI agent project inspired by DeerFlow, providing conversations, tool calls, subtasks, attachment processing, and execution progress views.
- Stack: Python 3.12 + FastAPI + SQLite; the frontend uses React 19 + TypeScript + Vite + Tailwind CSS.
- The remote deployment is at `tx:/home/pl/deer_mini`. systemd manages the FastAPI backend and Caddy frontend services; Caddy also proxies `/api` requests.

## 2. Commands

Run backend commands from `backend/` and frontend commands from `frontend/`. Install dependencies and start development servers in an isolated development environment.

- Install backend dependencies: `.venv/bin/python -m pip install -r requirements.txt` (requires an existing Python 3.12 virtual environment).
- Start the backend development server: `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8005 --reload`.
- Run routine backend regression tests: `.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py`.
- Install frontend dependencies: `pnpm install --frozen-lockfile` (use `pnpm 10.30.3`).
- Start the frontend development server: `pnpm dev`; type check: `pnpm typecheck`; test: `pnpm test`; build: `pnpm build`.
- Lint: the frontend currently has no `lint` script, and Ruff is not installed in the backend virtual environment. Do not list nonexistent commands as required checks.
- Read deployment logs: `journalctl -u deer-mini-backend.service -n 100 --no-pager`; use `deer-mini-frontend.service` for frontend logs.

## 3. Architecture

- Frontend pages and components live in `frontend/src/`, API clients in `frontend/src/api/`, and conversation execution and SSE state are managed by `frontend/src/hooks/`.
- Backend routes live in `backend/app/api/`; `services/` coordinates business logic, `runtime/` manages Run lifecycles, `agents/` runs the model and tool loop, and `tools/` and `subagents/` implement concrete capabilities.
- SQLite table definitions live in `backend/app/infrastructure/database.py`, with database access encapsulated in `repositories/`; see `backend/docs/hooks.md`, `backend/docs/model-error-handling.md`, `backend/docs/tool-results.md`, and `docs/thread-file-flow.md` for detailed mechanisms.

## 4. Conventions

- Use `snake_case` for Python modules and functions and `PascalCase` for classes. Keep type annotations in new code; use Chinese docstrings to explain the purpose, inputs, outputs, and side effects of complex modules.
- Frontend business components generally use `PascalCase.tsx`, and hooks use `useXxx.ts`. Preserve the existing naming conventions in directories such as `ui/` and `ai-elements/`; do not rename files in bulk.
- Preserve existing Pydantic schemas for successful API responses and use `HTTPException` with `detail` for HTTP errors. Update frontend and backend types and event fields together.
- Register tools in `ToolRegistry` and execute them through `ToolExecutor` wrapped by middleware. Reuse shared middleware for logging, error classification, retries, and storing large tool results on disk.
- Return ordinary tool errors as tool messages marked with `is_error`. Propagate final model failures, cancellation, and critical state persistence failures according to the existing Runtime boundaries.
- Place tests in the existing test directories and use mock models, temporary databases, and temporary files by default. Review the actual diff before committing and keep unrelated changes out.

### Git Conventions

- Before starting work, run `git status --short`, `git branch --show-current`, and `git diff`. If changes are staged, also inspect `git diff --cached` to distinguish existing work from the current task.
- Name new development branches directly after the module or feature, using concise lowercase words separated by hyphens, such as `structured-memory` or `sse-streaming`; do not add a `codex/` prefix. Do not switch branches in the directory used by running services; prefer a separate worktree or isolated copy for development.
- A new worktree does not automatically include uncommitted changes from the original checkout. Verify the development baseline and preserve existing changes; do not stash, overwrite, or discard them without authorization.
- Follow the existing `type: description` commit message style. Use `feat`, `fix`, `docs`, `refactor`, `test`, or `chore`, and clearly describe the change.
- Keep each commit focused on one explainable, reversible change. Separate feature changes from unrelated formatting and changes belonging to other tasks.
- Stage changes using explicit file paths or `git add -p`. When other work is present, do not use `git add .` or `git add -A`. Before committing, inspect `git diff --cached` and run `git diff --cached --check`.
- Complete checks relevant to the change before committing. At handoff, explain what changed, validation results, and any unfinished work. Commit, push, and merge according to the user's authorization for the current task.

## 5. Hard Constraints

- Do not add `.env`, API keys, databases, runtime logs, or user uploads to Git. Do not expose secrets or complete provider error bodies in logs, documentation, or chat.
- Without explicit authorization, do not delete, reset, or directly modify the live SQLite database or user files. Automated regression tests must not use live databases, user directories, or real model services.
- Thread, Run, attachment, and event endpoints must verify user ownership. New endpoints must use `_require_owned_thread()`, `_require_owned_run()`, or equivalent checks.
- File access must use the current Thread's path resolution and boundary checks. Do not bypass directory isolation, symlink checks, or upload size limits.
- Do not swallow `asyncio.CancelledError` or `StatePersistenceError`. Do not report persistence failures, timeouts, or interruptions as success; only declare completion after state persistence is confirmed.
- Model request retries must not replay tools that have already executed. After text or reasoning has been emitted, do not automatically regenerate from the beginning and concatenate it with a failed fragment. Keep SDK retries at `max_retries=0`; middleware controls attempt counts.
- The current runtime is designed for a single process. Do not enable multiple Uvicorn workers without an architectural change. Changes to running services, Caddy configuration, or release directories must be part of deployment work explicitly authorized by the user.
- Without explicit authorization, do not run `git reset --hard`, `git clean -f`, restoration operations that overwrite existing changes, force pushes, branch deletion, or operations that rewrite existing commit history.

## 6. Gotchas

- In noninteractive SSH sessions on `tx`, `pnpm` may not be on PATH. Use `/opt/deer-mini-runtime/bin/pnpm`. For the backend, use the project's `.venv/bin/python` rather than the system Python.
- Backend startup initializes SQLite and finalizes orphaned active Runs. Debug instances must use separate data. Configure isolated database and user directories with absolute paths in `DEER_MINI_DATABASE_PATH` and `DEER_MINI_DATA_ROOT`.
- Ports `8005` and `5175` on `tx` are already used by running services. For debugging on the same machine, use different ports and point `DEER_MINI_API_PROXY_TARGET` at the debug backend. Do not start another development server on the live service ports.
- Caddy serves the deployed frontend from a configured release directory under `frontend/releases/`. Running `pnpm build` alone does not update the live site; verify Caddy's actual target when deploying.
- `tests/app/model/test_factory.py` calls a real model service, and `tests/learning/` contains separate learning tests. Routine regression commands exclude both. Run real provider validation separately.
- The Bash tool requires Docker and explicit enablement; web tools require `TAVILY_API_KEY`. Tools are absent from the model's tool list when their prerequisites are not enabled or configured.
- `write_todos` only saves the complete task list and is available only to the lead agent. The model decides whether to revise the plan. Incomplete todos can trigger at most two continuation reminders; this is not a separate Replanner and does not guarantee task completion.
- `uploads/`, `workspace/`, and `outputs/` are the current Thread's three file areas. Bash starts in `/mnt/user-data/workspace` by default; write deliverables to `/mnt/user-data/outputs/`.
- Checking ownership through `user_id` is not login authentication. The current code has no `requireAuth()` middleware; do not describe it as an existing capability.
