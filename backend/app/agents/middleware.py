"""Agent Loop 各执行阶段的最小 Middleware 接口和调度器。"""

from collections.abc import Sequence

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
    ) -> None:
        """模型返回并加入 state 后执行。"""

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
    ) -> None:
        for middleware in reversed(self._middlewares):
            await middleware.after_model(state, context, message)

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
