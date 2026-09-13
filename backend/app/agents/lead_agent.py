"""实现 deer_mini 的核心“模型 → 工具 → 模型”循环。"""

import logging
from collections.abc import Sequence

from app.agents.middleware import AgentMiddleware, MiddlewareManager
from app.domain.threads import ThreadState
from app.domain.common import new_id
from app.model.base import ChatModel
from app.runtime.context import RuntimeContext
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


logger = logging.getLogger(__name__)


class LeadAgent:
    """本项目第一版的主 Agent，只负责模型与工具循环。"""

    def __init__(
        self,
        model: ChatModel,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        *,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        max_tool_rounds: int = 8,
        middlewares: Sequence[AgentMiddleware] | None = None,
    ) -> None:
        """
        输入：
        - model：本次 Run 使用的真实聊天模型。
        - tool_registry：提供给模型的可用工具说明书。
        - tool_executor：统一执行模型选择的工具。
        - max_tool_rounds：最多允许模型连续要求多少轮工具。
        """
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds 必须至少为 1")

        self._model = model
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._thinking_enabled = thinking_enabled
        self._reasoning_effort = reasoning_effort
        self._max_tool_rounds = max_tool_rounds
        self._middleware = MiddlewareManager(middlewares)

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

        try:
            try:
                await self._middleware.before_agent(state, context)
                final_state = await self._run_loop(state, context)
            except BaseException as error:
                # 清理钩子失败时不能掩盖模型、工具或取消操作的原始异常。
                try:
                    await self._middleware.after_agent(state, context, error)
                except Exception:
                    logger.exception("Agent 失败后的 Middleware 清理也发生异常")
                raise
            else:
                await self._middleware.after_agent(final_state, context, None)
                return final_state
        finally:
            # LeadAgent 每次 Run 使用独立模型客户端；无论成功还是异常都要释放连接池。
            await self._model.close()

    async def _run_loop(
        self,
        state: ThreadState,
        context: RuntimeContext,
    ) -> ThreadState:
        """执行真正的模型与工具循环。"""

        for round_number in range(1, self._max_tool_rounds + 1):
            # 实时片段和最后的完整消息使用同一个身份，恢复状态时才能去重。
            message_id = new_id()

            async def record_text_delta(text: str) -> None:
                await context.record_event(
                    "text.delta", {"text": text, "message_id": message_id},
                )

            async def record_reasoning_delta(text: str) -> None:
                # 思考与正文使用同一条消息编号，前端不会把它们拆成两个回复。
                await context.record_event(
                    "reasoning.delta", {"text": text, "message_id": message_id},
                )

            await self._middleware.before_model(state, context)
            assistant_message = await self._model.chat(
                messages=state.messages,
                tools=self._tool_registry.definitions(),
                thinking_enabled=self._thinking_enabled,
                reasoning_effort=self._reasoning_effort,
                on_text_delta=record_text_delta,
                on_reasoning_delta=record_reasoning_delta,
            )
            assistant_message.id = message_id
            state.messages.append(assistant_message)
            await self._middleware.after_model(
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
                return state

            # 第一版支持一轮多个工具调用，但按模型给出的顺序执行。
            for tool_call in assistant_message.tool_calls:
                await context.record_event(
                    "tool.start",
                    {
                        "round": round_number,
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                )

                tool_message = await self._tool_executor.execute(
                    tool_call,
                    context,
                )
                state.messages.append(tool_message)
                await self._middleware.after_tool(
                    state,
                    context,
                    tool_call,
                    tool_message,
                )
                await context.save_checkpoint(state)

                await context.record_event(
                    "tool.end",
                    {
                        "round": round_number,
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_call.name,
                        "content": tool_message.content,
                    },
                )

        raise RuntimeError(
            f"Agent 连续进行了 {self._max_tool_rounds} 轮工具调用，已停止以避免无限循环"
        )
