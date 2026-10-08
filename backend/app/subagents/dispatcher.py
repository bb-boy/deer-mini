"""登记子任务，限制同时执行的数量，并等待真实执行器的结果。

DeerFlow 对应 task_tool、SubagentLimitMiddleware 和执行器的调度池。
Mini 使用同一事件循环中的 Semaphore：三个名额满了，后面的工作单就排队。
"""

import asyncio
import logging
from collections.abc import Callable
from copy import deepcopy

from app.context_compression.policy import CompressionPolicy
from app.domain.subagents import SubagentTask
from app.domain.threads import ThreadState
from app.domain.tools import ToolResult
from app.model.base import ChatModel
from app.runtime.async_io import finish_inflight
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.subagents.executor import SubagentExecutor
from app.subagents.limits import DEFAULT_MAX_CONCURRENT_SUBAGENTS, DEFAULT_MAX_TOTAL_SUBAGENTS
from app.tools.registry import ToolRegistry


logger = logging.getLogger(__name__)


class SubagentDispatcher:
    """只协调一个父 Run 的子任务，不创建新的主 Run。

    输入：parent_state 是已恢复的父对话；context 提供本 Run 的身份与保存/
    事件入口；tool_registry 是子 Agent 可继承的真实工具；model_factory
    为每个实际开始的子任务创建独立模型。max_concurrent 是同时执行名额，
    max_total 是本 Run 累计工作单上限；timeout_seconds/max_tool_rounds
    限制单个子执行；thinking_enabled/reasoning_effort 沿用父模型设置。
    输出：execute 返回与原调用编号对应的 ToolResult。
    副作用：保存 pending 工作单、发布状态事件、启动模型和工具；本类不直接访问 SQLite。
    """

    def __init__(
        self, *, parent_state: ThreadState, context: RuntimeContext,
        tool_registry: ToolRegistry, model_factory: Callable[[], ChatModel],
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_SUBAGENTS,
        max_total: int = DEFAULT_MAX_TOTAL_SUBAGENTS,
        timeout_seconds: float = 120.0, max_tool_rounds: int = 8,
        thinking_enabled: bool = False, reasoning_effort: str | None = None,
        compression_policy: CompressionPolicy | None = None,
    ) -> None:
        for value in (max_concurrent, max_total):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("子任务并发数和累计额度必须是正整数")
        self._state = parent_state
        self._context = context
        self._max_total = max_total
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # 登记新工作单与合并子消息必须共用一把锁，否则旧快照可能丢掉新工作单。
        self._checkpoint_lock = asyncio.Lock()
        self._active_calls: set[str] = set()
        self._executor = SubagentExecutor(
            parent_state=parent_state, context=context, tool_registry=tool_registry,
            model_factory=model_factory, timeout_seconds=timeout_seconds,
            max_tool_rounds=max_tool_rounds, thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort, checkpoint_lock=self._checkpoint_lock,
            compression_policy=compression_policy,
        )

    async def execute(self, candidate: SubagentTask, context: RuntimeContext) -> ToolResult:
        """输入一张新工作单和调用上下文；登记成功后排队，再执行并返回结果。

        相同 Run 中重复的调用编号复用已登记工作单；已完成任务不再执行。
        取消排队时也保存 cancelled，不能把从未启动的任务永远留在 pending。
        """
        candidate.to_dict()
        identity = (self._context.user_id, self._context.thread_id, self._context.run_id)
        if (context.user_id, context.thread_id, context.run_id) != identity or (
            context.workspace_path != self._context.workspace_path
        ) or (candidate.user_id, candidate.thread_id, candidate.run_id) != identity:
            raise ValueError("委派请求不属于已绑定的用户、Thread、Run 或工作目录")
        if candidate.subagent_type != "general-purpose":
            raise ValueError("Unknown subagent type. Available: general-purpose")
        if candidate.status != "pending" or candidate.messages or candidate.result is not None or candidate.error is not None:
            raise ValueError("只能提交尚未执行的新工作单")
        if candidate.tool_call_id in self._active_calls:
            raise ValueError("这个 task 调用已经在排队或执行，不能重复启动")
        self._active_calls.add(candidate.tool_call_id)
        task: SubagentTask | None = None

        async def admit() -> None:
            nonlocal task
            async with self._checkpoint_lock:
                current = [item for item in self._state.subtasks.values() if item.run_id == context.run_id]
                for item in current:
                    if item.tool_call_id == candidate.tool_call_id:
                        if (item.description, item.prompt, item.subagent_type) != (
                            candidate.description, candidate.prompt, candidate.subagent_type,
                        ):
                            raise ValueError("同一个 task 调用编号不能改成另一项任务")
                        task = item
                        return
                if len(current) >= self._max_total:
                    raise ValueError(f"Task limit reached: maximum {self._max_total} delegations per run")
                snapshot = deepcopy(self._state)
                snapshot.subtasks[candidate.task_id] = deepcopy(candidate)
                try:
                    await self._context.save_checkpoint(snapshot)
                except StatePersistenceError:
                    raise
                except Exception as error:
                    raise StatePersistenceError("子任务工作单未能确认保存，不能开始执行") from error
                self._state.subtasks[candidate.task_id] = candidate
                # 在受保护的提交内部保留引用：取消可能发生在提交成功、await 返回之前。
                task = candidate

        try:
            await finish_inflight(asyncio.create_task(admit()))
            assert task is not None
            if task.status == "pending":
                await self._publish_pending(task)
            # 名额覆盖模型创建、工具循环和收尾；等待名额的时间仍受父 Run 总超时限制。
            async with self._semaphore:
                return await self._executor.execute(task)
        except asyncio.CancelledError as error:
            if task is not None and task.status == "pending":
                try:
                    await finish_inflight(asyncio.create_task(self._executor.cancel_pending(task, error)))
                except asyncio.CancelledError:
                    pass  # 第二次取消也等到收尾结束，然后继续抛出原取消。
            raise
        finally:
            self._active_calls.discard(candidate.tool_call_id)

    async def _publish_pending(self, task: SubagentTask) -> None:
        """工作单已保存后发送排队状态；通知失败只记应用警告。"""
        try:
            await self._context.record_event("subagent.status", {
                "task_id": task.task_id, "parent_tool_call_id": task.tool_call_id,
                "description": task.description, "subagent_type": task.subagent_type,
                "status": "pending", "status_confirmed": True, "result": None, "error": None,
            })
        except Exception:
            logger.warning("子任务 %s 的排队通知失败，工作单已保存", task.task_id, exc_info=True)
