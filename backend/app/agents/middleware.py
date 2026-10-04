"""Agent Loop 各执行阶段的最小 Middleware 接口和调度器。"""

from collections.abc import Awaitable, Callable, Sequence

from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext


class AgentMiddleware:
    """提供默认无操作钩子；具体 Middleware 只覆盖自己关心的阶段。"""

    async def before_agent(
        self,
        state: ThreadState,
        context: RuntimeContext,
    ) -> None:
        """整个 Agent Loop 开始前执行一次。"""

    async def before_model(
        self,
        state: ThreadState,
        context: RuntimeContext,
    ) -> None:
        """每次请求模型前执行。"""

    async def after_model(
        self,
        state: ThreadState,
        context: RuntimeContext,
        message: Message,
    ) -> bool | None:
        """模型返回并加入 state 后执行；返回 True 可让普通回答后再请求一轮。"""

    async def prepare_model_messages(
        self, state: ThreadState, context: RuntimeContext, messages: list[Message],
    ) -> list[Message]:
        """整理本次模型请求；临时控制提醒不应写进 state.messages。"""
        return messages

    async def wrap_model_call(
        self,
        state: ThreadState,
        context: RuntimeContext,
        call_next: Callable[[], Awaitable[Message]],
    ) -> Message:
        """包住一次模型调用；默认直接执行下一层。"""
        return await call_next()

    async def wrap_tool_call(
        self,
        state: ThreadState,
        context: RuntimeContext,
        tool_call: ToolCall,
        call_next: Callable[[], Awaitable[Message]],
    ) -> Message:
        """包住一次工具调用；默认直接执行下一层。"""
        return await call_next()

    async def after_tool(
        self,
        state: ThreadState,
        context: RuntimeContext,
        tool_call: ToolCall,
        tool_message: Message,
    ) -> None:
        """工具结果加入 state 后执行。"""

    async def after_agent(
        self,
        state: ThreadState,
        context: RuntimeContext,
        error: BaseException | None,
    ) -> None:
        """整个 Agent Loop 结束时执行；error 表示本次失败原因。"""

    async def finalize_tool_results(
        self, state: ThreadState, context: RuntimeContext,
        tool_calls: list[ToolCall], tool_messages: list[Message],
    ) -> bool | None:
        """本轮正常完成或中断结果补齐后执行；返回 True 表示结果发生变化。"""


class MiddlewareManager:
    """按照稳定顺序调度一组 AgentMiddleware。"""

    def __init__(
        self,
        middlewares: Sequence[AgentMiddleware] | None = None,
    ) -> None:
        self._middlewares = tuple(middlewares or ())

    async def before_agent(
        self,
        state: ThreadState,
        context: RuntimeContext,
    ) -> None:
        for middleware in self._middlewares:
            await middleware.before_agent(state, context)

    async def before_model(
        self,
        state: ThreadState,
        context: RuntimeContext,
    ) -> None:
        for middleware in self._middlewares:
            await middleware.before_model(state, context)

    async def after_model(
        self,
        state: ThreadState,
        context: RuntimeContext,
        message: Message,
    ) -> bool:
        continue_requested = False
        for middleware in reversed(self._middlewares):
            result = await middleware.after_model(state, context, message)
            continue_requested = bool(result) or continue_requested
        return continue_requested

    async def prepare_model_messages(
        self, state: ThreadState, context: RuntimeContext, messages: list[Message],
    ) -> list[Message]:
        for middleware in self._middlewares:
            messages = await middleware.prepare_model_messages(state, context, messages)
        return messages

    async def wrap_model_call(
        self,
        state: ThreadState,
        context: RuntimeContext,
        call_next: Callable[[], Awaitable[Message]],
    ) -> Message:
        """按注册顺序进入模型中间件，再按相反顺序返回。"""
        async def invoke(index: int) -> Message:
            if index == len(self._middlewares):
                return await call_next()
            return await self._middlewares[index].wrap_model_call(
                state, context, lambda: invoke(index + 1),
            )

        return await invoke(0)

    async def wrap_tool_call(
        self,
        state: ThreadState,
        context: RuntimeContext,
        tool_call: ToolCall,
        call_next: Callable[[], Awaitable[Message]],
    ) -> Message:
        """按注册顺序进入中间件，再按相反顺序返回。"""
        async def invoke(index: int) -> Message:
            if index == len(self._middlewares):
                return await call_next()
            return await self._middlewares[index].wrap_tool_call(
                state, context, tool_call, lambda: invoke(index + 1),
            )

        return await invoke(0)

    async def after_tool(
        self,
        state: ThreadState,
        context: RuntimeContext,
        tool_call: ToolCall,
        tool_message: Message,
    ) -> None:
        for middleware in reversed(self._middlewares):
            await middleware.after_tool(
                state,
                context,
                tool_call,
                tool_message,
            )

    async def after_agent(
        self,
        state: ThreadState,
        context: RuntimeContext,
        error: BaseException | None,
    ) -> None:
        for middleware in reversed(self._middlewares):
            await middleware.after_agent(state, context, error)

    async def finalize_tool_results(
        self, state: ThreadState, context: RuntimeContext,
        tool_calls: list[ToolCall], tool_messages: list[Message],
    ) -> bool:
        changed = False
        for middleware in self._middlewares:
            result = await middleware.finalize_tool_results(state, context, tool_calls, tool_messages)
            changed = bool(result) or changed
        return changed
