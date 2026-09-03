# deer_mini Docker Bash Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Agent 在临时受限 Docker 容器中执行 Bash，并将结果通过现有工具闭环返回模型。

**Architecture:** `BashTool` 只负责工具协议与结果格式，`DockerCommandRunner` 负责容器命令、输出上限和清理。Bash 默认关闭，由 `RunCoordinator` 根据环境配置选择性注册。

**Tech Stack:** Python 3.12、asyncio subprocess、Docker CLI、pytest。

---

### Task 1: CommandRunner 契约与 Docker 参数

**Files:**
- Create: `backend/app/sandbox/base.py`
- Create: `backend/app/sandbox/docker_runner.py`
- Test: `backend/tests/app/sandbox/test_docker_runner.py`

- [ ] 先写失败测试，约束只挂载当前 Workspace、网络关闭、只读根目录、资源限制和命令作为独立 argv。
- [ ] 实现 `CommandResult`、`CommandRunner` 与 Docker 参数构造。
- [ ] 重跑测试变绿。

关键接口：

```python
class CommandRunner(Protocol):
    async def run(self, *, command: str, workspace_path: str,
                  run_id: str, tool_call_id: str) -> CommandResult: ...
```

### Task 2: 输出、超时和取消清理

**Files:**
- Modify: `backend/app/sandbox/docker_runner.py`
- Test: `backend/tests/app/sandbox/test_docker_runner.py`

- [ ] 先写失败测试，使用假 Docker 可执行文件产生大输出、阻塞和记录 kill。
- [ ] 实现有界管道排空、`asyncio.wait_for`、取消时 shield 清理、进程组回收和 `docker rm -f` 兜底。
- [ ] 重跑测试变绿，确认没有残留子进程。

### Task 3: BashTool

**Files:**
- Create: `backend/app/tools/bash.py`
- Test: `backend/tests/app/tools/test_bash.py`

- [ ] 先写失败测试，覆盖定义、参数校验、成功、非零退出和超时结果。
- [ ] 实现 `BashTool.execute()`，只通过 `CommandRunner` 产生副作用。
- [ ] 重跑测试变绿。

### Task 4: 条件注册与配置

**Files:**
- Modify: `backend/app/services/run_coordinator.py`
- Modify: `backend/.env.example`
- Test: `backend/tests/app/services/test_run_coordinator_tools.py`

- [ ] 先写失败测试，确认默认只有 `read_file`，显式 Runner 时包含 `bash`。
- [ ] 实现 Runner 注入、环境配置解析和选择性注册。
- [ ] 重跑测试变绿。

### Task 5: 真实 Docker 与完整回归

**Files:**
- Test: `backend/tests/app/sandbox/test_docker_runner_live.py`

- [ ] 写 opt-in 真实 Docker + Agent Loop 测试，在临时 Workspace 生成文件并检查输出。
- [ ] 写真实 Docker 超时测试，确认容器最终被删除。
- [ ] 运行 `RUN_DOCKER_SANDBOX_TEST=1` 测试并检查没有 `deer-mini-` 残留容器。
- [ ] 运行后端相关回归、compileall、`git diff --check`。
- [ ] 只读确认真实数据库 active Run 仍为 93，不提交 Git。
