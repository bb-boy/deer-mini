"""有界地关闭一个模型客户端，避免清理连接无限拖住取消和流收尾。"""

import asyncio
import logging
import math

from app.model.base import ChatModel
from app.runtime.async_io import finish_inflight


logger = logging.getLogger(__name__)
_late_closers: set[asyncio.Task[None]] = set()


def _observe_late_close(task: asyncio.Task[None]) -> None:
    """输入超时后结束的关闭任务；观察结果并移除引用，异常写应用日志。"""
    _late_closers.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.warning("模型连接在收尾超时后关闭失败",
                       exc_info=(type(error), error, error.__traceback__))


async def close_chat_model(model: ChatModel, timeout_seconds: float = 5.0) -> None:
    """关闭这个客户端的网络连接，成功返回 None，失败继续抛出异常。

    输入：model 是本次 Agent 独占的客户端；timeout_seconds 是关闭等待上限。
    副作用：关闭连接；超时时请求取消关闭操作，并保留引用观察后续异常。
    外部取消先等待这段有界清理，再交还调用方，不遗留无人观察的清理任务。
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("模型关闭等待时间必须是大于 0 的有限数字")

    async def close_with_deadline() -> BaseException | None:
        task = asyncio.create_task(model.close(), name="close-chat-model")
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
        if not done:
            task.cancel()
            _late_closers.add(task)
            task.add_done_callback(_observe_late_close)
            return TimeoutError(f"模型连接关闭超过 {timeout_seconds:g} 秒")
        try:
            task.result()
        except (Exception, asyncio.CancelledError) as error:
            # 返回异常对象，让外部取消仍然优先于清理过程中产生的新异常。
            return error
        return None

    error = await finish_inflight(asyncio.create_task(close_with_deadline()))
    if error is not None:
        raise error
