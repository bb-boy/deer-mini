"""An example of code that runs outside every tool call."""

import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from app.agents.middleware import AgentMiddleware
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext


# Uvicorn's service logger has an INFO handler; the default root logger may not.
logger = logging.getLogger("uvicorn.error")


class ToolCallLoggingMiddleware(AgentMiddleware):
    """Log the boundary of each tool call without exposing its arguments or output."""

    async def wrap_tool_call(
        self,
        state: ThreadState,
        context: RuntimeContext,
        tool_call: ToolCall,
        call_next: Callable[[], Awaitable[Message]],
    ) -> Message:
        started = perf_counter()
        logger.info("工具调用前 name=%s id=%s", tool_call.name, tool_call.id)
        try:
            return await call_next()
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            logger.info(
                "工具调用后 name=%s id=%s elapsed_ms=%.1f",
                tool_call.name, tool_call.id, elapsed_ms,
            )
