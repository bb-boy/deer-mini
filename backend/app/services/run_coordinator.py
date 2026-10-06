"""把 HTTP 创建 Run 的请求接到真正的 Agent Runtime。"""

import asyncio
import logging
import math
import os
from typing import cast
from collections.abc import Callable

from app.runtime.errors import log_runtime_exception
from app.agents.prompts.builder import apply_prompt_template
from app.agents.lead_agent import LeadAgent
from app.agents.todo_middleware import TodoMiddleware
from app.agents.middleware_stack import build_runtime_middlewares
from app.agents.workspace_context_middleware import WorkspaceContextMiddleware
from app.domain.runs import Run
from app.domain.threads import ThreadState
from app.model.factory import ModelFactory
from app.model.base import ChatModel
from app.memory.service import MemoryService
from app.model.errors import ModelCallError
from app.runtime.agent_runtime import AgentRuntime
from app.runtime.async_io import run_sync
from app.runtime.event_recorder import EventRecorder
from app.runtime.stream_bridge import MemoryStreamBridge
from app.sandbox.base import CommandRunner
from app.sandbox.docker_runner import DockerCommandRunner, load_docker_runner_from_env
from app.sandbox.manager import (
    ThreadSandboxManager,
    cleanup_orphaned_thread_sandboxes,
)
from app.services.run_service import RunService
from app.services.file_checkpoint_service import FileCheckpointService
from app.services.thread_operations import ThreadOperations
from app.repositories.thread_repository import ThreadRepository
from app.repositories.sandbox_safety_repository import SandboxSafetyRepository
from app.services.thread_service import (
    ThreadService,
    cleanup_pending_thread_deletions,
)
from app.subagents.middleware import SubagentMiddleware
from app.tools.bash import BashTool
from app.tools.executor import ToolExecutor
from app.tools.read_file_rewrite import ReadFileTool
from app.tools.read_tool_result import ReadToolResultTool
from app.tools.registry import ToolRegistry
from app.tools.task import TaskTool
from app.tools.web_search import WebSearchTool
from app.tools.web_fetch import WebFetchTool
from app.tools.write_todos import WriteTodosTool


logger = logging.getLogger(__name__)
_AUTO_BASH_RUNNER = object()

ORPHAN_RECOVERY_ERROR = (
    "Run execution was lost because the service restarted"
)


def _load_run_timeout_seconds() -> float:
    """从环境变量读取 Run 总超时，默认四分钟。"""
    raw_value = os.getenv("DEER_MINI_RUN_TIMEOUT_SECONDS", "240")
    try:
        timeout_seconds = float(raw_value)
    except ValueError as error:
        raise ValueError(
            "DEER_MINI_RUN_TIMEOUT_SECONDS 必须是数字"
        ) from error

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError(
            "DEER_MINI_RUN_TIMEOUT_SECONDS 必须是大于 0 的有限数字"
        )
    return timeout_seconds


class RunCoordinator:
    """创建 Run，并让每个 Run 在当前进程的后台执行。"""

    def __init__(
        self,
        stream_bridge: MemoryStreamBridge,
        run_service: RunService | None = None,
        run_timeout_seconds: float | None = None,
        bash_runner: CommandRunner | None | object = _AUTO_BASH_RUNNER,
        memory_service: MemoryService | None = None,
    ) -> None:
        self._stream_bridge = stream_bridge
        self._memory_service = memory_service or MemoryService()
        self._run_service = run_service or RunService()
        self._run_timeout_seconds = (
            _load_run_timeout_seconds()
            if run_timeout_seconds is None
            else run_timeout_seconds
        )
        if (
            not math.isfinite(self._run_timeout_seconds)
            or self._run_timeout_seconds <= 0
        ):
            raise ValueError("run_timeout_seconds 必须是大于 0 的有限数字")
        # 默认不暴露 Bash。只有环境变量显式开启，或测试/调用方注入 Runner
        # 时，模型的 tools 列表中才会出现 bash。
        self._uses_environment_bash_config = bash_runner is _AUTO_BASH_RUNNER
        if self._uses_environment_bash_config:
            self._bash_runner: CommandRunner | None = (
                load_docker_runner_from_env()
            )
        else:
            self._bash_runner = cast(CommandRunner | None, bash_runner)
        self._sandbox_manager: ThreadSandboxManager | None = None
        if isinstance(self._bash_runner, DockerCommandRunner):
            self._sandbox_manager = ThreadSandboxManager(self._bash_runner)
            self._bash_runner = self._sandbox_manager
        elif isinstance(self._bash_runner, ThreadSandboxManager):
            self._sandbox_manager = self._bash_runner
        # 按 run_id 保存后台任务，取消接口才能找到指定的 Agent Loop。
        self._tasks: dict[str, asyncio.Task[ThreadState]] = {}
        self._run_owners: dict[str, tuple[str, str]] = {}
        self._sandbox_started = False
        self.operations = ThreadOperations()
        self.file_checkpoints = FileCheckpointService()

    def _build_tool_registry(self) -> ToolRegistry:
        """创建一次 Run 使用的工具表，避免在不同入口重复开关逻辑。"""
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        registry.register(ReadToolResultTool())
        if self._bash_runner is not None:
            registry.register(BashTool(self._bash_runner))
        # 搜索与网页读取共用后台密钥；未配置时不向模型暴露这两个工具。
        tavily_api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if tavily_api_key:
            registry.register(WebSearchTool(tavily_api_key))
            registry.register(WebFetchTool(tavily_api_key))
        registry.register(TaskTool())
        registry.register(WriteTodosTool())
        return registry

    def _owned_file_thread(self, user_id: str, thread_id: str):
        thread = ThreadRepository().get(thread_id, user_id)
        if thread is None:
            raise LookupError("Thread 不存在")
        if self.file_checkpoints.repo.operations(thread, pending_only=True):
            raise RuntimeError("对话文件恢复尚待完成，暂不能开始新操作")
        return thread

    async def create_and_start_run(self, *, user_id: str, thread_id: str,
                                   message: str, model_name: str, thinking_enabled: bool,
                                   reasoning_effort: str | None) -> Run:
        with self.operations.write(user_id, thread_id, "run"):
            self._owned_file_thread(user_id, thread_id)
            result = await self._create_and_start_run(user_id=user_id, thread_id=thread_id,
                message=message, model_name=model_name, thinking_enabled=thinking_enabled,
                reasoning_effort=reasoning_effort)
            self.operations.register_run(user_id, thread_id, result.id)
            return result

    async def preview_restore(self, *, user_id: str, thread_id: str, point_id: str) -> dict:
        # 预览也需要一致文件状态。先停止运行后可重新取得稳定的版本。
        with self.operations.write(user_id, thread_id, "preview"):
            thread = self._owned_file_thread(user_id, thread_id)
            if self._sandbox_manager is not None:
                await self._sandbox_manager.stop_thread(user_id=user_id, thread_id=thread_id)
            return await run_sync(self.file_checkpoints.preview, thread, point_id)

    async def restore_thread(self, *, user_id: str, thread_id: str, operation_id: str,
                             point_id: str, revision: int, fingerprint: str) -> dict:
        thread = ThreadRepository().get(thread_id, user_id)
        if thread is None:
            raise LookupError("Thread 不存在")
        previous = self.file_checkpoints.repo.operation(thread, operation_id)
        if previous is not None:
            if (previous['restore_point_id'], previous['expected_revision'], previous['fingerprint']) != (
                    point_id, revision, fingerprint):
                raise RuntimeError("恢复操作编号已用于不同请求")
            return previous
        with self.operations.write(user_id, thread_id, "restore", allow_run=True):
            self._owned_file_thread(user_id, thread_id)
            # 独占标志已登记；取消和 Runtime 收尾不需要这把操作准入。
            for run in self._run_service.list_inflight_runs():
                if run.user_id == user_id and run.thread_id == thread_id:
                    await self.cancel_run(user_id=user_id, thread_id=thread_id, run_id=run.id)
            if self._sandbox_manager is not None:
                try:
                    await self._sandbox_manager.stop_thread(user_id=user_id, thread_id=thread_id)
                except BaseException:
                    self.operations.block(user_id, thread_id)
                    raise
            try:
                return await run_sync(self.file_checkpoints.restore, thread, operation_id,
                                      point_id, revision, fingerprint)
            finally:
                if self.file_checkpoints.repo.operations(thread, pending_only=True):
                    self.operations.block(user_id, thread_id)

    async def recover_file_operations(self) -> None:
        for user_id, thread_id in await run_sync(self.file_checkpoints.recover_pending):
            self.operations.block(user_id, thread_id)

    async def _create_and_start_run(
        self,
        *,
        user_id: str,
        thread_id: str,
        message: str,
        model_name: str,
        thinking_enabled: bool,
        reasoning_effort: str | None,
    ) -> Run:
        """
        创建 pending Run，并把完整 Agent 执行注册为后台任务。

        模型客户端先于 Run 创建，以便配置错误不会留下无法执行的 Run。
        """
        model_factory = ModelFactory(model_name)
        model = model_factory.create_chat_model()

        try:
            registry = self._build_tool_registry()
            task_tool = registry.get("task")
            assert isinstance(task_tool, TaskTool)
            todo_tool = registry.get("write_todos")
            assert isinstance(todo_tool, WriteTodosTool)
            system_prompt = await asyncio.to_thread(
                apply_prompt_template,
                web_search_enabled=registry.get("web_search") is not None,
                subagent_enabled=True,
            )
            extra_middlewares = [
                WorkspaceContextMiddleware(registry),
                TodoMiddleware(todo_tool),
                SubagentMiddleware(
                    task_tool, registry, model_factory.create_chat_model,
                    thinking_enabled=thinking_enabled, reasoning_effort=reasoning_effort,
                ),
            ]
            if self._memory_service.enabled:
                extra_middlewares.append(self._memory_service.middleware(model_factory.create_chat_model))
            agent = LeadAgent(
                model=model,
                tool_registry=registry,
                tool_executor=ToolExecutor(registry),
                thinking_enabled=thinking_enabled,
                reasoning_effort=reasoning_effort,
                system_prompt=system_prompt,
                middlewares=build_runtime_middlewares(extra_middlewares),
            )
            run = self._run_service.create_run(
                user_id=user_id,
                thread_id=thread_id,
                model_name=model_name,
                thinking_enabled=thinking_enabled,
                reasoning_effort=reasoning_effort,
            )
            task = asyncio.create_task(
                AgentRuntime(
                    self._stream_bridge, sandbox_lifecycle=self._sandbox_manager,
                    file_checkpoints=self.file_checkpoints,
                    on_sandbox_failure=lambda: self.operations.block(user_id, thread_id),
                ).run(
                    user_id=user_id,
                    thread_id=thread_id,
                    run_id=run.id,
                    user_message=message,
                    agent=agent,
                    timeout_seconds=self._run_timeout_seconds,
                ),
                name=f"agent-run-{run.id}",
            )
        except BaseException:
            await model.close()
            raise

        self._tasks[run.id] = task
        self._run_owners[run.id] = (user_id, thread_id)
        task.add_done_callback(
            lambda finished_task, run_id=run.id: self._consume_finished_task(
                run_id,
                finished_task,
                memory_factory=model_factory.create_chat_model,
            )
        )
        return run

    async def cancel_run(
        self,
        *,
        user_id: str,
        thread_id: str,
        run_id: str,
    ) -> Run:
        """
        取消指定 Run，并返回 SQLite 中的最终状态。

        正常情况由 AgentRuntime 记录 interrupted 事件并更新状态；
        如果任务尚未真正开始，Coordinator 会完成同样的持久化和流收尾。
        """
        run = self._run_service.get_run(run_id, user_id)
        if run is None or run.thread_id != thread_id:
            raise ValueError("Thread 或 Run 不存在")
        if run.status not in {"pending", "running"}:
            raise RuntimeError(f"Run 已经结束，当前状态为 {run.status}")

        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel("cancelled_by_user")
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Runtime 已负责把真正的执行错误保存为 error；
                # 这里重新读取状态并返回，不用取消错误覆盖原错误。
                log_runtime_exception(logger, "取消 Run 时，后台任务以异常结束")

        refreshed = self._run_service.get_run(run_id, user_id)
        if refreshed is not None and refreshed.status in {"pending", "running"}:
            # 极早取消时，协程可能一次也没有获得执行机会，因此 Runtime 的
            # except/finally 都不会运行；这里负责补齐事件、状态和 SSE 结束信号。
            recorder = EventRecorder(
                user_id=user_id,
                thread_id=thread_id,
                run_id=run_id,
                stream_bridge=self._stream_bridge,
            )
            try:
                confirmed = await run_sync(
                    self._run_service.interrupt_run, run_id, user_id, "cancelled_by_user",
                )
                if confirmed:
                    await recorder.record_event("run.interrupted", {
                        "status": "interrupted", "status_confirmed": True,
                        "reason": "cancelled_by_user",
                    })
            finally:
                try:
                    await recorder.close()
                finally:
                    await self._stream_bridge.publish_end(run_id)

        result = self._run_service.get_run(run_id, user_id)
        if result is None:
            raise ValueError("Run 不存在")
        if result.status != "interrupted":
            # Agent 可能在取消信号送达前已经成功或失败，不能把这种情况
            # 伪装成“取消成功”。
            raise RuntimeError(f"Run 未被取消，当前状态为 {result.status}")
        return result

    async def recover_orphaned_runs(self) -> list[Run]:
        """
        应用启动时收尾上一次进程遗留的 pending/running Run。

        SQLite Mini 是单进程部署，所以启动前存在的活跃记录不可能属于
        当前进程。多 Worker 版本必须增加租约后才能使用同类判断。
        """
        recovered_runs: list[Run] = []
        for candidate in self._run_service.list_inflight_runs():
            try:
                recovered = self._run_service.recover_orphaned_run(
                    candidate.id,
                    candidate.user_id,
                    ORPHAN_RECOVERY_ERROR,
                )
            except Exception:
                log_runtime_exception(logger,
                    "无法恢复孤儿 Run %s，继续处理其他记录",
                    candidate.id,
                )
                continue
            if not recovered:
                continue

            recorder = EventRecorder(
                user_id=candidate.user_id,
                thread_id=candidate.thread_id,
                run_id=candidate.id,
                stream_bridge=self._stream_bridge,
            )
            try:
                await recorder.record_event(
                    "run.error",
                    {
                        "error_type": "OrphanedRunRecovery",
                        "message": ORPHAN_RECOVERY_ERROR,
                        "reason": "service_restart",
                    },
                )
            except Exception:
                # Run/Thread 已经恢复为终态；单条事件写入失败不能阻止
                # 其余孤儿 Run 被恢复。
                log_runtime_exception(logger,
                    "无法为孤儿 Run %s 记录恢复事件",
                    candidate.id,
                )
            finally:
                try:
                    await recorder.close()
                finally:
                    await self._stream_bridge.publish_end(candidate.id)

            result = self._run_service.get_run(
                candidate.id,
                candidate.user_id,
            )
            if result is not None:
                recovered_runs.append(result)

        if recovered_runs:
            logger.warning(
                "应用启动时恢复了 %d 个孤儿 Run",
                len(recovered_runs),
            )
        return recovered_runs

    async def start(self) -> None:
        await self._memory_service.start()
        await cleanup_pending_thread_deletions()
        if self._sandbox_manager is not None:
            await run_sync(SandboxSafetyRepository().mark_active)
            await self._sandbox_manager.start()
            self._sandbox_started = True
        elif self._uses_environment_bash_config:
            await cleanup_orphaned_thread_sandboxes()

    async def delete_thread(self, *, user_id: str, thread_id: str) -> None:
        with self.operations.write(user_id, thread_id, "delete"):
            self._owned_file_thread(user_id, thread_id)
            await self._delete_thread(user_id=user_id, thread_id=thread_id)

    async def _delete_thread(self, *, user_id: str, thread_id: str) -> None:
        async def delete_workspace() -> None:
            # 删除容器会 await；回来后重新确认没有新建的 pending Run。
            if any(
                run.user_id == user_id and run.thread_id == thread_id
                for run in self._run_service.list_inflight_runs()
            ):
                raise RuntimeError("运行中或等待执行的 Thread 不能删除")
            await ThreadService().delete_thread(thread_id, user_id)

        if self._sandbox_manager is None:
            await delete_workspace()
        else:
            await self._sandbox_manager.delete_thread(
                user_id=user_id, thread_id=thread_id,
                delete_workspace=delete_workspace,
            )

    async def shutdown(self) -> None:
        """应用退出时取消仍在运行的 Agent，避免遗留 running Run。"""
        self._memory_service.stop_accepting()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel("server_shutdown")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._memory_service.shutdown()
        await EventRecorder.shutdown_writers()
        if self._sandbox_manager is not None:
            await self._sandbox_manager.close()
            if self._sandbox_started:
                await run_sync(SandboxSafetyRepository().mark_clear)

    def _consume_finished_task(
        self,
        run_id: str,
        task: asyncio.Task[ThreadState],
        *, memory_factory: Callable[[], ChatModel] | None = None,
    ) -> None:
        """移除已完成任务，并读取异常，避免 asyncio 输出未处理警告。"""
        if self._tasks.get(run_id) is task:
            self._tasks.pop(run_id, None)
            owner = self._run_owners.pop(run_id, None)
            if owner is not None:
                self.operations.release_run(*owner, run_id)
        if task.cancelled():
            return

        error = task.exception()
        if isinstance(error, ModelCallError):
            # 不打印 cause 链：供应商异常正文可能包含请求内容或配置。
            logger.error("Agent 模型请求失败 run=%s reason=%s attempts=%s status=%s",
                run_id, error.reason, error.attempts, error.status_code)
            return
        if isinstance(error, TimeoutError):
            logger.info("Agent Run 已自动超时：%s", run_id)
            return
        if error is not None:
            log_runtime_exception(logger, "Agent 后台任务执行失败", error=error)
            return
        if memory_factory is not None:
            # Runtime 成功返回时，最终 checkpoint、Run 终态和收尾均已确认。
            self._memory_service.schedule(task.result(), run_id, memory_factory)
