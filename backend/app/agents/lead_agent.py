"""实现 deer_mini 的核心“模型 → 工具 → 模型”循环。"""

import asyncio
import logging
import math
from collections.abc import Sequence

from app.runtime.errors import log_runtime_exception
from app.agents.middleware import AgentMiddleware, MiddlewareManager
from app.agents.middleware_stack import build_runtime_middlewares
from app.agents.tool_calls import execute_tool_calls, repair_interrupted_tool_history
from app.domain.threads import ThreadState
from app.domain.common import new_id
from app.model.errors import ModelCallProgress, model_call_progress
from app.model.base import ChatModel
from app.model.lifecycle import close_chat_model
from app.runtime.context import RuntimeContext
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.domain.messages import Message


logger = logging.getLogger(__name__)


class LeadAgent:
    """本项目第一版的主 Agent，只负责模型与工具循环。"""

    def __init__(
        self,
        model: ChatModel,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        *,
        system_prompt: str = "",
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        max_tool_rounds: int = 8,
        middlewares: Sequence[AgentMiddleware] | None = None,
        model_close_timeout: float = 5.0,
    ) -> None:
        """
        输入：
        - model：本次 Run 使用的真实聊天模型。
        - tool_registry：提供给模型的可用工具说明书。
        - tool_executor：统一执行模型选择的工具。
        - max_tool_rounds：模型调用总轮数上限，含工具决策与继续提醒后的请求。
        - system_prompt: 系统提示，用于指导模型的行为。
        """
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds 必须至少为 1")
        if not math.isfinite(model_close_timeout) or model_close_timeout <= 0:
            raise ValueError("model_close_timeout 必须是大于 0 的有限数字")

        self._model = model
        self._system_prompt = system_prompt
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._thinking_enabled = thinking_enabled
        self._reasoning_effort = reasoning_effort
        self._max_tool_rounds = max_tool_rounds
        self._middleware = MiddlewareManager(
            build_runtime_middlewares() if middlewares is None else middlewares,
        )
        self._model_close_timeout = model_close_timeout


    def _build_model_messages(
        self,
        messages: list[Message],
    ) -> list[Message]:
        """整理本次模型请求，保留持久化历史的原始结构。"""
        system_parts = []
        conversation_messages = []

        if self._system_prompt:
            system_parts.append(self._system_prompt)

        for message in messages:
            if message.role == "system":
                # 收集已有的工作目录等系统上下文。
                system_parts.append(message.content)
            else:
                # 用户、模型、工具消息保持原来的顺序和角色。
                conversation_messages.append(message)

        if not system_parts:
            return conversation_messages

        system_message = Message(
            role="system",
            content="\n\n".join(system_parts),
        )

        return [system_message, *conversation_messages]

    async def run(
        self,
        state: ThreadState,
        context: RuntimeContext,
    ) -> ThreadState:
        """
        让模型持续回答或调用工具，直到取得最终回答。

        输出：包含用户消息、模型消息、工具结果和最终回答的 ThreadState。
        副作用：调用模型、执行工具、写入事件，并在关键步骤保存 Checkpoint。
        """

        original_error: BaseException | None = None
        try:
            try:
                # 上次进程可能停在“模型已决定调用、工具尚未返回”的位置。
                # 补齐明确的中断结果后继续新消息，不自动重放可能有副作用的工具。
                if repair_interrupted_tool_history(state):
                    await context.save_checkpoint(state)
                await self._middleware.before_agent(state, context)
                final_state = await self._run_loop(state, context)
            except BaseException as error:
                # 清理钩子失败时不能掩盖模型、工具或取消操作的原始异常。
                try:
                    await self._middleware.after_agent(state, context, error)
                except (Exception, asyncio.CancelledError):
                    log_runtime_exception(logger, "Agent 失败后的 Middleware 清理也发生异常")
                raise
            else:
                await self._middleware.after_agent(final_state, context, None)
                return final_state
        except BaseException as error:
            original_error = error
            raise
        finally:
            # LeadAgent 每次 Run 使用独立模型客户端；无论成功还是异常都要释放连接池。
            try:
                await close_chat_model(self._model, self._model_close_timeout)
            except (Exception, asyncio.CancelledError):
                if original_error is None:
                    raise
                log_runtime_exception(logger, "模型连接清理失败，保留 Agent 的原始异常")

    async def _run_loop(
        self,
        state: ThreadState,
        context: RuntimeContext,
    ) -> ThreadState:
        """执行真正的模型与工具循环。"""

        for round_number in range(1, self._max_tool_rounds + 1):
            # 实时片段和最后的完整消息使用同一个身份，恢复状态时才能去重。
            message_id = new_id()
            progress = ModelCallProgress(message_id=message_id)

            async def record_text_delta(text: str) -> None:
                if text:
                    progress.visible_output = True
                await context.record_event(
                    "text.delta", {"text": text, "message_id": message_id},
                )

            async def record_reasoning_delta(text: str) -> None:
                if text:
                    progress.visible_output = True
                # 思考与正文使用同一条消息编号，前端不会把它们拆成两个回复。
                await context.record_event(
                    "reasoning.delta", {"text": text, "message_id": message_id},
                )

            await self._middleware.before_model(state, context)
            model_messages = await self._middleware.prepare_model_messages(
                state, context, self._build_model_messages(state.messages),
            )
            # 最内层只负责请求模型；重试、日志等包装行为由中间件决定。
            async def call_model() -> Message:
                return await self._model.chat(
                    messages=model_messages,
                    tools=self._tool_registry.definitions(),
                    thinking_enabled=self._thinking_enabled,
                    reasoning_effort=self._reasoning_effort,
                    on_text_delta=record_text_delta,
                    on_reasoning_delta=record_reasoning_delta,
                )

            token = model_call_progress.set(progress)
            try:
                assistant_message = await self._middleware.wrap_model_call(
                    state, context, call_model,
                )
            finally:
                model_call_progress.reset(token)
            assistant_message.id = message_id
            state.messages.append(assistant_message)
            continue_requested = await self._middleware.after_model(
                state,
                context,
                assistant_message,
            )

            # 每轮完整消息（包括工具调用决定）都保存为关键状态。
            await context.save_checkpoint(state)
            await context.record_event(
                "message.complete", {"message": assistant_message.to_dict(), "round": round_number},
            )
            if not assistant_message.tool_calls:
                if continue_requested:
                    continue
                return state

            await execute_tool_calls(
                state=state, context=context, calls=assistant_message.tool_calls,
                round_number=round_number, executor=self._tool_executor,
                middleware=self._middleware,
            )

        raise RuntimeError(
            f"Agent 已达到 {self._max_tool_rounds} 轮模型调用上限，已停止以避免无限循环"
        )
