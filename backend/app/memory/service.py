"""成功 Run 的后台记忆调度；按用户串行抽取，失败不改变主 Run 结果。"""

import asyncio
import logging
import math
import os
from collections.abc import Callable
from copy import deepcopy

from app.agents.memory_context_middleware import MemoryContextMiddleware
from app.domain.threads import ThreadState
from app.memory.extractor import MemoryExtractor
from app.memory.selector import MemorySelector
from app.memory.store import MemoryStore
from app.model.base import ChatModel


logger = logging.getLogger(__name__)
ModelFactory = Callable[[], ChatModel]


def memory_enabled() -> bool:
    """读取显式开关，默认启用；无索引时不增加选择模型调用。"""
    value = os.getenv("DEER_MINI_MEMORY_ENABLED", "true").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError("DEER_MINI_MEMORY_ENABLED 必须是 true/false 或 1/0")
    return value in {"true", "1"}


class MemoryService:
    """管理用户级记忆的读入中间件和有限生命周期的后台写入任务。

    输入：安全存储、启用开关与退出等待预算；模型工厂由具体 Run 提供。
    副作用：调度独立模型调用和文件写入。当前只支持单进程，不持久化任务队列。
    """

    def __init__(self, *, store: MemoryStore | None = None,
                 enabled: bool | None = None, shutdown_timeout: float = 5.0) -> None:
        if not math.isfinite(shutdown_timeout) or shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout 必须为正的有限数字")
        self.store = store or MemoryStore()
        self.enabled = memory_enabled() if enabled is None else enabled
        self._shutdown_timeout = shutdown_timeout
        self._closing = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._tails: dict[str, asyncio.Task[None]] = {}

    def middleware(self, model_factory: ModelFactory) -> MemoryContextMiddleware:
        """为一个 lead Run 创建独立上下文快照，子 agent 不另行检索。"""
        return MemoryContextMiddleware(MemorySelector(self.store, model_factory))

    def stop_accepting(self) -> None:
        """服务退出前停止接收成功回调产生的新记忆任务。"""
        self._closing = True

    def schedule(self, state: ThreadState, run_id: str,
                 model_factory: ModelFactory) -> None:
        """接收已确认成功的 Run 快照；同用户按排队顺序使用最新记忆。"""
        if not self.enabled or self._closing:
            return
        snapshot = deepcopy(state)
        previous = self._tails.get(state.user_id)

        async def extract() -> None:
            if previous is not None:
                # 本任务被取消时不取消前一个用户任务；其失败也不阻断下一项。
                await asyncio.shield(asyncio.gather(previous, return_exceptions=True))
            await MemoryExtractor(self.store, model_factory).extract(snapshot, run_id)

        task = asyncio.create_task(extract(), name=f"memory-extract-{run_id}")
        self._tasks.add(task)
        self._tails[state.user_id] = task

        def completed(finished: asyncio.Task[None]) -> None:
            self._tasks.discard(finished)
            if self._tails.get(snapshot.user_id) is finished:
                self._tails.pop(snapshot.user_id, None)
            if not finished.cancelled() and (error := finished.exception()) is not None:
                # 不打印异常正文、记忆内容或 cause 链。
                logger.warning("记忆更新失败 run=%s reason=%s", run_id, type(error).__name__)

        task.add_done_callback(completed)

    async def flush(self, *, timeout: float = 5.0) -> bool:
        """限时等待当前队列；返回是否全部完成，不取消仍在进行的任务。"""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必须为正的有限数字")
        tasks = set(self._tasks)
        if not tasks:
            return True
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        return not pending

    async def shutdown(self) -> None:
        """停止入队，限时等待，然后取消剩余任务并等待有界模型清理。"""
        self.stop_accepting()
        await self.flush(timeout=self._shutdown_timeout)
        remaining = list(self._tasks)
        for task in remaining:
            task.cancel("memory_service_shutdown")
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
