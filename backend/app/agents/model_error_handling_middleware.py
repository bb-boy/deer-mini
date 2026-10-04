"""仅重试当前模型请求；不重放工具，不吞掉取消或本地错误。"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from app.agents.middleware import AgentMiddleware
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.model.call_policy import ModelCallPolicy
from app.model.errors import (
    EmptyModelResponseError, ModelCallError, ModelCallProgress,
    ModelRequestTimeoutError, PUBLIC_REASONS, classify_model_error,
    model_call_progress, retry_after_seconds, safe_request_id,
)
from app.runtime.context import RuntimeContext


logger = logging.getLogger("uvicorn.error")


class ModelErrorHandlingMiddleware(AgentMiddleware):
    def __init__(
        self, policy: ModelCallPolicy | None = None, *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.policy = policy or ModelCallPolicy.from_env()
        self._sleep, self._random_value = sleep, random_value

    async def wrap_model_call(
        self, state: ThreadState, context: RuntimeContext,
        call_next: Callable[[], Awaitable[Message]],
    ) -> Message:
        progress = model_call_progress.get()
        token = None
        if progress is None:
            progress = ModelCallProgress()
            token = model_call_progress.set(progress)
        attempts = 0
        limit = self.policy.max_attempts
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.policy.call_timeout_seconds

        async def status(phase, **extra):
            await context.record_event("model.status", {
                "phase": phase, "message_id": progress.message_id,
                "attempt": attempts, "max_attempts": limit, **extra,
            })

        async def fail(reason, original, status_code=None):
            error = ModelCallError(reason, attempts, status_code=status_code)
            await status("failed", reason=reason, status_code=status_code, message=str(error))
            if progress.visible_output:
                await context.record_event("model.interrupted", {
                    "message_id": progress.message_id, "reason": reason, "message": str(error),
                })
            logger.warning(
                "模型请求失败 thread=%s run=%s message=%s attempts=%s reason=%s status=%s request_id=%s",
                context.thread_id, context.run_id, progress.message_id, attempts,
                reason, status_code, safe_request_id(original),
            )
            raise error from original

        total_timeout = asyncio.timeout_at(deadline)
        try:
            async with total_timeout:
                while True:
                    attempts += 1
                    await status("attempt")
                    attempt_timeout = asyncio.timeout_at(min(deadline, loop.time() + self.policy.attempt_timeout_seconds))
                    try:
                        try:
                            async with attempt_timeout:
                                message = await call_next()
                        except TimeoutError as error:
                            if not attempt_timeout.expired():
                                raise
                            raise ModelRequestTimeoutError() from error
                        if not message.content.strip() and not message.reasoning_content and not message.tool_calls:
                            raise EmptyModelResponseError()
                    except Exception as original:
                        decision = classify_model_error(original)
                        if decision is None:
                            # 保存状态失败、参数校验 bug 等维持原异常身份。
                            raise
                        limit = min(limit, decision.max_attempts)
                        if progress.visible_output:
                            await fail("stream_interrupted", original, decision.status_code)
                        if not decision.retryable or attempts >= limit:
                            await fail(decision.reason, original, decision.status_code)
                        if decision.reason == "empty_response" and context.model_retry_budget.empty_response_retry_used:
                            await fail(decision.reason, original, decision.status_code)

                        base = self.policy.burst_delay_seconds if decision.reason == "burst_rate" else self.policy.base_delay_seconds
                        delay = min(self.policy.delay_cap_seconds, base * 2 ** (attempts - 1) + self._random_value() * 0.5)
                        retry_after = retry_after_seconds(original)
                        if retry_after is not None:
                            delay = max(delay, retry_after)
                        if delay > self.policy.max_wait_seconds or delay >= deadline - loop.time():
                            await fail(decision.reason, original, decision.status_code)
                        if decision.reason == "empty_response":
                            context.model_retry_budget.empty_response_retry_used = True
                        logger.info(
                            "模型请求重试 thread=%s run=%s message=%s next_attempt=%s max_attempts=%s reason=%s status=%s delay_s=%.3f request_id=%s",
                            context.thread_id, context.run_id, progress.message_id,
                            attempts + 1, limit, decision.reason, decision.status_code,
                            delay, safe_request_id(original),
                        )
                        await status("retry", attempt=attempts + 1, delay_seconds=delay,
                            reason=decision.reason, status_code=decision.status_code,
                            message=f"{PUBLIC_REASONS[decision.reason]}，等待 {delay:g} 秒后重试，第 {attempts + 1}/{limit} 次尝试")
                        await self._sleep(delay)
                    else:
                        await status("complete")
                        return message
        except TimeoutError as original:
            if not total_timeout.expired():
                raise
            await fail("stream_interrupted" if progress.visible_output else "call_timeout", original)
        finally:
            if token is not None:
                model_call_progress.reset(token)
