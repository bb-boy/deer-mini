# Native Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkboxes for tracking.

**Goal:** 完成原生 Read/Grep/Glob/Edit/Write 并与现有 Bash/WebSearch 共同用于 agent。
**Architecture:** 工具实现复用 ThreadPaths、安全描述符 IO、AtomicWriter 和公共中间件；注册/提示/前端保留现有调用链。设计依据 `docs/native-tools/design.md`。
**Tech Stack:** Python 3.12、Pydantic、regex、pytest、React 19、TypeScript、Vitest。

## Task 1: 文件工具（单一 backend implementer 负责）
Files: create `backend/app/tools/file_access.py`, `glob.py`, `grep.py`, `write_file.py`, `edit_file.py`; modify `backend/app/tools/read_file_rewrite.py`, `backend/requirements.txt`; tests in `backend/tests/app/tools/test_native_*.py` and existing `test_read_file_rewrite.py`.

- [x] 先写并运行行为测试；例如创建后未授权覆盖失败且字节不变、歧义编辑失败、`**/*.py` 包含顶层和子目录、line_numbers 保留 base 行号。尚未实现时使用 registry lookup 或导入能力断言得到明确失败。
```python
assert registry.get("write_file") is not None
assert json.loads(glob_result.content)["files"] == ["workspace/a.py", "workspace/pkg/b.py"]
assert (await write_tool.execute(overwrite_call, context)).is_error
assert target.read_bytes() == original
```
- [x] 用 ThreadPaths 解析后从 thread root 描述符逐段打开父目录；读取使用 open_regular_file/bounded_read；写入使用 AtomicWriter，完整事务放在取消安全 run_sync 内并串行原生修改。
- [x] 各文件独立 Tool 类：GlobTool、GrepTool、WriteFileTool、EditFileTool；schema、返回值、错误语义按 design.md。ReadFileTool 保持默认返回文本、编码/base/offset。
- [x] 明确落实扫描预算、regex timeout、内部文件排除、UTF-8/二进制跳过和结构化不完整标记，测试下调预算以避免慢测试。
- [x] 执行 `.venv/bin/python -m pytest tests/app/tools -q`，覆盖持久化故障、符号/硬链接、取消期间 IO 完成及并发写入。记录首次失败与修复后结果。

## Task 2: 注册、模型提示和展示（主 agent 负责）
Files: modify `backend/app/services/run_coordinator.py`, `backend/app/agents/workspace_context_middleware.py`, `frontend/src/components/AssistantProcess.tsx`; tests `backend/tests/app/services/test_run_coordinator_tools.py`, `backend/tests/app/agents/test_native_tools_integration.py`.

- [x] 先修改 registry 测试，声明以下必备工具，并运行观察缺少新工具的失败。
```python
native_names = ["read_file", "glob", "grep", "edit_file", "write_file", "read_tool_result"]
```
- [x] 导入并注册新类，顺序为上面列表；保留 Bash/Tavily 开关以及 task/write_todos 权限。
```python
registry.register(ReadFileTool())
registry.register(GlobTool())
registry.register(GrepTool())
registry.register(EditFileTool())
registry.register(WriteFileTool())
registry.register(ReadToolResultTool())
```
- [x] 根据 registry 可用项添加定位、读取、精确编辑、显式覆盖和 outputs 路径说明；前端添加 glob/grep/edit_file 标签及 path/pattern 摘要。
- [x] 用真实 registry + ToolExecutor + mock model/临时 Thread 验证 create→edit→read/search，失败以 tool message is_error 返回；用现有文件 checkpoint 验证原生写改恢复。
- [x] 在独立 frontend 目录执行 `/opt/deer-mini-runtime/bin/pnpm install --frozen-lockfile` 后 typecheck/test/build；运行集成与已有结果落盘测试。

审查补充：Read 增加 preserve_newlines=false 可选参数；设 true 可精确读取 CRLF/CR。Read→Edit 契约、严格布尔和编号/范围兼容测试通过。子任务测试同步新增继承工具列表。

## Task 3: 回归和独立审查
- [x] 使用独立环境变量，执行 backend 常规回归 `.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q`；记录跳过项。
- [x] 检查完整 diff、未跟踪文件、`git diff --check`；交付前不执行推送/合并/部署。
- [x] 派发新上下文 deer-mini-module-reviewer，提供 BASE/HEAD、全部未提交差异、design.md 和验证记录；先确认需求符合，再审代码质量和集成。
- [x] 修复与复审闭环，更新 `docs/native-tools/verification.md` 与本计划状态，保留 native-tools 工作树交付。
