"""把同步数据库操作交给线程，并妥善处理写入期间的取消。"""

import asyncio
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


async def finish_inflight(task: asyncio.Task[T]) -> T:
    """等待在途操作结束，再传递取消，避免 SQLite 仍在写时开始下一次收尾。"""
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancelled = error
    if cancelled is not None:
        raise cancelled
    return result


async def run_sync(function: Callable[..., T], *args, **kwargs) -> T:
    """输入同步操作及参数；在线程里执行并返回结果，数据库错误继续向上抛出。"""
    return await finish_inflight(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))
