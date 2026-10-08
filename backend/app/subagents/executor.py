"""运行一张子任务工作单，复用现有的“模型 → 工具 → 模型”循环。

DeerFlow 对应：SubagentExecutor._create_agent/_build_initial_state/_aexecute。
Mini 为每个父 Run 建立一个执行器，多个 execute 可以并发等待模型/工具；
只把“合并子任务状态并保存完整父快照”这一小段按顺序执行。
"""

import asyncio
import logging
import math
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace

from app.agents.lead_agent import LeadAgent
from app.agents.middleware_stack import build_runtime_middlewares
from app.agents.workspace_context_middleware import WorkspaceContextMiddleware
from app.context_compression.middleware import ContextCompressionMiddleware
from app.context_compression.policy import CompressionPolicy
from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent, RunEventType
from app.domain.messages import Message
from app.domain.subagents import SubagentStatus, SubagentTask
from app.domain.threads import ThreadState
from app.domain.tools import ToolResult
from app.model.base import ChatModel
from app.model.lifecycle import close_chat_model
from app.runtime.async_io import finish_inflight
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.subagents.prompts import build_subagent_prompt
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.snip import SnipTool


logger = logging.getLogger(__name__)
# 与 DeerFlow 的 general-purpose 配置一致，工具表本身也禁止继续委派。
# write_todos 绑定父状态，只能由主 Agent 更新，不能传给子 Agent 共享写入。
DISALLOWED_TOOLS = frozenset({"task", "ask_clarification", "present_files", "write_todos"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})
CHILD_EVENT_TYPES: dict[RunEventType, RunEventType] = {
    "text.delta": "subagent.text.delta",
    "reasoning.delta": "subagent.reasoning.delta",
    "message.complete": "subagent.message.complete",
    "model.status": "subagent.model.status",
    "model.interrupted": "subagent.model.interrupted",
    "tool.start": "subagent.tool.start",
    "tool.end": "subagent.tool.end",
}


class SubagentExecutor:
    """把工作单变成真实的独立 Agent 执行，不接管父 Run 的生命周期。

    输入：parent_state 是父对话的完整状态；context 提供本次 Run 的身份、
    工作区、实时事件和 Checkpoint 入口；tool_registry 是父方可用工具；
    model_factory 必须每次创建新客户端，例如 ModelFactory(name).create_chat_model。
    timeout_seconds 限制一次子 Agent 执行；max_tool_rounds 限制模型轮数；
    thinking_enabled/reasoning_effort 控制子模型推理；model_close_timeout 限制关闭连接的等待。
    checkpoint_lock 可与 Dispatcher 共用，保护工作单登记和子状态合并。
    输出：execute 返回关联原始 tool_call_id 的 ToolResult。
    副作用：调用模型和工具，通过 context 写 Checkpoint、发事件，更新工作单。
    本类不直接访问 Repository，不新建 Run，也不关闭父方 SSE 或工作环境。
    """

    def __init__(
        self, *, parent_state: ThreadState, context: RuntimeContext,
        tool_registry: ToolRegistry, model_factory: Callable[[], ChatModel],
        timeout_seconds: float = 120.0, max_tool_rounds: int = 8,
        thinking_enabled: bool = False, reasoning_effort: str | None = None,
        model_close_timeout: float = 5.0,
        checkpoint_lock: asyncio.Lock | None = None,
        compression_policy: CompressionPolicy | None = None,
    ) -> None:
        if (parent_state.user_id, parent_state.thread_id) != (context.user_id, context.thread_id):
            raise ValueError("父状态与 RuntimeContext 不属于同一用户和 Thread")
        if parent_state.workspace_path not in {None, context.workspace_path}:
            raise ValueError("父状态与 RuntimeContext 的工作目录不一致")
        if any(not math.isfinite(value) or value <= 0
               for value in (timeout_seconds, model_close_timeout)):
            raise ValueError("子任务超时和模型关闭时间必须是大于 0 的有限数字")
        if not isinstance(max_tool_rounds, int) or isinstance(max_tool_rounds, bool) or max_tool_rounds < 1:
            raise ValueError("max_tool_rounds 必须是大于 0 的整数")
        self._compression_policy = compression_policy or CompressionPolicy.from_env()
        self._parent_state = parent_state
        self._context = context
        self._registry = tool_registry
        self._model_factory = model_factory
        self._timeout_seconds = timeout_seconds
        self._max_tool_rounds = max_tool_rounds
        self._thinking_enabled = thinking_enabled
        self._reasoning_effort = reasoning_effort
        self._model_close_timeout = model_close_timeout
        self._checkpoint_lock = checkpoint_lock if checkpoint_lock is not None else asyncio.Lock()
        self._active: set[str] = set()

    def _validate_task(self, task: SubagentTask) -> None:
        """只校验工作单归属和登记身份，防止覆盖别人的任务或历史 Run。"""
        task.to_dict()
        if (task.user_id, task.thread_id, task.run_id) != (
            self._context.user_id, self._context.thread_id, self._context.run_id,
        ):
            raise ValueError("子任务不属于当前用户、Thread 和 Run")
        if self._parent_state.subtasks.get(task.task_id) is not task:
            raise ValueError("请传入 parent_state.subtasks 中已登记的原工作单")
        if task.subagent_type != "general-purpose":
            raise ValueError(f"暂不支持子 Agent 类型：{task.subagent_type}")

    async def execute(self, task: SubagentTask) -> ToolResult:
        """输入已登记的 pending 工作单，执行后保存终态并返回主方工具结果。

        已结束工作单只返回已有结果，避免重复执行有副作用的工具；running
        工作单拒绝重复启动。取消保存 cancelled 后继续抛出 CancelledError；
        关键状态失败抛出 StatePersistenceError，由父 Runtime 确认 Run 失败。
        """
        self._validate_task(task)
        if task.task_id in self._active or task.status == "running":
            raise RuntimeError("子任务已经在运行；不能重复启动或自动重放未完成工具")
        if task.status in TERMINAL_STATUSES:
            return self._tool_result(task)
        if task.messages or task.result is not None or task.error is not None:
            raise ValueError("pending 工作单必须是尚未执行的新任务")

        # 这里没有 await；同一执行器中的另一个调用不能抢先启动同一张工作单。
        self._active.add(task.task_id)
        child = ThreadState(
            user_id=task.user_id, thread_id=task.thread_id,
            workspace_path=self._context.workspace_path,
            messages=[Message(role="user", content=task.prompt)],
        )
        child_context = self._child_context(task)
        deadline = asyncio.timeout(self._timeout_seconds)
        try:
            async with deadline:
                # 保存独立的用户消息和 running 状态，再请求模型。
                await self._transition(task, child, "running")
                child = await self._run_child(child, child_context)
            final_message = child.messages[-1]
            if final_message.role != "assistant" or final_message.tool_calls:
                raise RuntimeError("子 Agent 没有返回完整的最终回答")
            await self._transition(task, child, "completed", result=final_message.content)
            return self._tool_result(task)
        except asyncio.CancelledError as error:
            if task.status not in TERMINAL_STATUSES:
                try:
                    await self._finish_error(task, child, "cancelled", error)
                except asyncio.CancelledError:
                    # 第二次取消已经等到收尾完成；仍把第一次取消交还父 Runtime。
                    pass
            raise
        except StatePersistenceError:
            # 保存失败不等于普通的子任务失败，不能包装成工具文字继续运行。
            raise
        except Exception as error:
            status: SubagentStatus = "timed_out" if deadline.expired() else "failed"
            await self._finish_error(task, child, status, error)
            return self._tool_result(task)
        finally:
            self._active.discard(task.task_id)

    async def cancel_pending(self, task: SubagentTask, error: BaseException) -> None:
        """取消尚未拿到执行名额的工作单；保存终态，不创建模型、不执行工具。"""
        self._validate_task(task)
        if task.status != "pending" or task.task_id in self._active:
            raise ValueError("只能通过此入口取消尚未启动的子任务")
        child = ThreadState(
            user_id=task.user_id, thread_id=task.thread_id,
            workspace_path=self._context.workspace_path, messages=deepcopy(task.messages),
        )
        await self._finish_error(task, child, "cancelled", error)

    async def _run_child(self, child: ThreadState, context: RuntimeContext) -> ThreadState:
        """创建新工具表和新模型，交给现有 LeadAgent；不复制父对话消息。"""
        registry = ToolRegistry()
        for definition in self._registry.definitions():
            if definition.name not in DISALLOWED_TOOLS and definition.name != "snip":
                tool = self._registry.get(definition.name)
                assert tool is not None
                registry.register(tool)
        if self._compression_policy.enabled and self._compression_policy.snip_enabled:
            registry.register(SnipTool())
        prompt = build_subagent_prompt([item.name for item in registry.definitions()])
        model = self._model_factory()
        try:
            agent = LeadAgent(
                model, registry, ToolExecutor(registry), system_prompt=prompt,
                thinking_enabled=self._thinking_enabled, reasoning_effort=self._reasoning_effort,
                max_tool_rounds=self._max_tool_rounds,
                middlewares=build_runtime_middlewares([
                    WorkspaceContextMiddleware(registry),
                    ContextCompressionMiddleware(
                        model, registry,
                        model_key=getattr(model, "_model_name", "subagent"),
                        policy=self._compression_policy,
                    ),
                ]),
                model_close_timeout=self._model_close_timeout,
            )
        except BaseException:
            # 正常情况下由 LeadAgent 关闭；只有构造 Agent 失败时在这里释放。
            try:
                await close_chat_model(model, self._model_close_timeout)
            except (Exception, asyncio.CancelledError):
                logger.exception("子 Agent 创建失败后的模型清理也失败，保留原异常")
            raise
        return await agent.run(child, context)

    def _child_context(self, task: SubagentTask) -> RuntimeContext:
        """为这个子任务包装事件和保存入口；身份、工作目录沿用父 Runtime。"""
        async def record_event(event_type: RunEventType, payload: dict) -> RunEvent:
            return await self._context.record_event(
                CHILD_EVENT_TYPES[event_type], {**payload, **self._identity(task)},
            )

        async def save_checkpoint(child: ThreadState) -> Checkpoint:
            return await self._save_child(task, child, "running")

        return replace(self._context, record_event=record_event, save_checkpoint=save_checkpoint)

    @staticmethod
    def _identity(task: SubagentTask) -> dict:
        """返回事件归属；父工具编号单列，避免覆盖子 Agent 内部工具编号。"""
        return {
            "task_id": task.task_id, "parent_tool_call_id": task.tool_call_id,
            "subagent_type": task.subagent_type, "description": task.description,
        }

    async def _save_child(
        self, task: SubagentTask, child: ThreadState, status: SubagentStatus,
        *, result: str | None = None, error: str | None = None,
    ) -> Checkpoint:
        """合并完整父快照并保存；成功后才更新内存工作单，不传子状态给父 saver。"""
        async def persist() -> Checkpoint:
            async with self._checkpoint_lock:
                self._validate_task(task)
                if (child.user_id, child.thread_id, child.workspace_path) != (
                    task.user_id, task.thread_id, self._context.workspace_path,
                ) or child.subtasks or child.todos or child.todos_run_id is not None or child.todos_tool_call_id is not None:
                    raise ValueError("子状态的归属、工作目录、嵌套任务或父清单不合法")
                if task.status in TERMINAL_STATUSES:
                    raise ValueError("已结束的子任务不能再修改状态")
                if task.status == "pending" and status not in {"running", "cancelled"}:
                    raise ValueError("子任务必须先进入 running")
                candidate = replace(
                    task, messages=deepcopy(child.messages), status=status, result=result, error=error,
                    compression=deepcopy(child.compression),
                )
                snapshot = deepcopy(self._parent_state)
                snapshot.subtasks[task.task_id] = candidate
                saved = await self._context.save_checkpoint(snapshot)
                # 只更新这一张工作单；父消息和其他子任务不会被旧快照覆盖。
                task.messages = deepcopy(candidate.messages)
                task.status, task.result, task.error = status, result, error
                task.compression = deepcopy(candidate.compression)
                return saved

        try:
            # 外部取消也先完成在途写入及内存更新，避免数据库与内存各说一个状态。
            return await finish_inflight(asyncio.create_task(persist()))
        except StatePersistenceError:
            raise
        except Exception as error:
            raise StatePersistenceError(f"子任务 {task.task_id} 的关键状态未能确认保存") from error

    async def _transition(
        self, task: SubagentTask, child: ThreadState, status: SubagentStatus,
        *, result: str | None = None, error: str | None = None,
    ) -> None:
        """保存状态后通知订阅者；辅助通知失败写应用日志，不篡改已保存的结果。"""
        await self._save_child(task, child, status, result=result, error=error)
        try:
            await self._context.record_event("subagent.status", {
                **self._identity(task), "status": status, "status_confirmed": True,
                "result": result, "error": error,
            })
        except Exception:
            logger.warning("子任务 %s 的状态通知失败，状态已保存", task.task_id, exc_info=True)

    async def _finish_error(
        self, task: SubagentTask, child: ThreadState, status: SubagentStatus, error: BaseException,
    ) -> None:
        """保存失败、超时或取消及完整已取得消息；保留收尾之前的原始异常。"""
        details = (
            f"Subagent execution timed out after {self._timeout_seconds:g} seconds."
            if status == "timed_out" else str(error) or type(error).__name__
        )
        try:
            await finish_inflight(asyncio.create_task(
                self._transition(task, child, status, error=details),
            ))
        except StatePersistenceError as save_error:
            save_error.execution_error = error
            save_error.add_note(f"子任务原始执行异常：{type(error).__name__}: {error}")
            raise

    @staticmethod
    def _tool_result(task: SubagentTask) -> ToolResult:
        """把工作单结果交还父工具调用；不把子 Agent 的完整消息混入主对话。"""
        if task.status == "completed":
            if task.result is None or task.error is not None:
                raise StatePersistenceError("已完成的子任务缺少有效结果")
            return ToolResult(tool_call_id=task.tool_call_id, name="task", content=task.result)
        return ToolResult(
            tool_call_id=task.tool_call_id, name="task",
            content=f"Task {task.status}: {task.error or task.status}", is_error=True,
        )
