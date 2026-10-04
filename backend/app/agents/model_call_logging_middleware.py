"""记录模型调用边界，不记录提示词、回复或异常详情。"""

import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from app.agents.middleware import AgentMiddleware
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext


logger = logging.getLogger("uvicorn.error")


class ModelCallLoggingMiddleware(AgentMiddleware):
    async def wrap_model_call(
        self,
        state: ThreadState,
        context: RuntimeContext,
        call_next: Callable[[], Awaitable[Message]],
    ) -> Message:
        started = perf_counter()
        outcome = "success"
        logger.info("模型调用前 thread=%s run=%s", context.thread_id, context.run_id)
        try:
            return await call_next()
        except BaseException as error:
            # 记录异常类型，保留原异常（包括取消）交给外层 Runtime。
            outcome = type(error).__name__
            raise
        finally:
            logger.info(
                "模型调用后 thread=%s run=%s outcome=%s elapsed_ms=%.1f",
                context.thread_id, context.run_id, outcome,
                (perf_counter() - started) * 1000,
            )
