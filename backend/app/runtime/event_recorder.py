"""先发布实时事件，再由单个后台任务按序批量保存辅助日志。"""

import asyncio
import json
import logging
import math
from collections import deque
from copy import deepcopy

from app.domain.events import RunEvent, RunEventType
from app.repositories.events_repository import EventRepository
from app.runtime.stream_bridge import MemoryStreamBridge


logger = logging.getLogger(__name__)
# 收尾超时也保留任务引用并观察结果，不能留下无人处理的后台异常。
_writers: set[asyncio.Task[None]] = set()


class EventRecorder:
    """一次 Run 的实时发布与辅助日志缓冲。

    user_id/thread_id/run_id 指定事件归属；stream_bridge 负责内存发送；
    event_repository 负责 SQLite。其余参数限制批次、缓存、重试与收尾时间。
    文字片段只实时发送；完整消息和工具过程等日志在后台保存。
    """

    def __init__(
        self, user_id: str, thread_id: str, run_id: str,
        stream_bridge: MemoryStreamBridge,
        event_repository: EventRepository | None = None,
        *, batch_size: int = 32, flush_interval: float = 0.1,
        max_buffer_events: int = 512, max_buffer_bytes: int = 4 * 1024 * 1024,
        max_attempts: int = 3, retry_delay: float = 0.05,
        close_timeout: float = 2.0,
    ) -> None:
        if min(batch_size, max_buffer_events, max_buffer_bytes, max_attempts) < 1:
            raise ValueError("日志批次、缓存和重试次数必须大于 0")
        if any(not math.isfinite(value) or value <= 0
               for value in (flush_interval, retry_delay, close_timeout)):
            raise ValueError("日志等待时间必须是大于 0 的有限数字")
        self._user_id, self._thread_id, self._run_id = user_id, thread_id, run_id
        self._stream_bridge = stream_bridge
        self._event_repository = event_repository or EventRepository()
        self._batch_size, self._flush_interval = batch_size, flush_interval
        self._max_buffer_events, self._max_buffer_bytes = max_buffer_events, max_buffer_bytes
        self._max_attempts, self._retry_delay = max_attempts, retry_delay
        self._close_timeout = close_timeout
        self._buffer: deque[tuple[RunEvent, int]] = deque()
        self._pending_count = self._pending_bytes = 0
        self.dropped_events = 0
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._abandon = False

    async def record_event(self, event_type: RunEventType, payload: dict) -> RunEvent:
        """输入事件名与内容，返回内存事件；返回不代表日志已经入库。

        实时 data 仍是 RunEvent 的字典，sequence 留空。副作用是发布 Stream，
        并为需要保存的事件安排后台写入；数据库故障不会阻止本方法发送。
        """
        if self._closing:
            raise RuntimeError("Run 的事件记录器已经关闭")
        event = RunEvent(
            run_id=self._run_id, thread_id=self._thread_id,
            event_type=event_type, payload=deepcopy(payload),
        )
        # 两种片段都只实时发布；完整正文和思考随 message.complete / Checkpoint 保存。
        if event_type not in {"text.delta", "reasoning.delta"}:
            self._enqueue(event)
        await self._stream_bridge.publish(self._run_id, "run_event", event.to_dict())
        return event

    def _enqueue(self, event: RunEvent) -> None:
        size = len(json.dumps(event.to_dict(), ensure_ascii=False).encode("utf-8"))
        # 限制包括正在写入的批次，不能仅限制等待队列。
        if (self._pending_count >= self._max_buffer_events
                or self._pending_bytes + size > self._max_buffer_bytes):
            self.dropped_events += 1
            if self.dropped_events == 1:
                logger.warning("Run %s 日志缓冲达到上限，部分辅助日志将缺失", self._run_id)
            return
        self._buffer.append((event, size))
        self._pending_count += 1
        self._pending_bytes += size
        if self._worker is None:
            self._worker = asyncio.create_task(self._write_loop(), name=f"run-journal-{self._run_id}")
            _writers.add(self._worker)
            self._worker.add_done_callback(self._observe_writer)
        if len(self._buffer) >= self._batch_size:
            self._wake.set()

    def _observe_writer(self, task: asyncio.Task[None]) -> None:
        _writers.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Run %s 日志工作任务异常", self._run_id,
                         exc_info=(type(error), error, error.__traceback__))

    async def _write_loop(self) -> None:
        while not self._abandon:
            if not self._closing and len(self._buffer) < self._batch_size:
                try:
                    await asyncio.wait_for(self._wake.wait(), self._flush_interval)
                except TimeoutError:
                    pass
                self._wake.clear()
            if not self._buffer:
                if self._closing:
                    return
                continue

            batch = [self._buffer.popleft() for _ in range(min(self._batch_size, len(self._buffer)))]
            try:
                await self._write_batch([event for event, _ in batch])
            finally:
                self._pending_count -= len(batch)
                self._pending_bytes -= sum(size for _, size in batch)

    async def _write_batch(self, events: list[RunEvent]) -> None:
        for attempt in range(self._max_attempts):
            if self._abandon:
                break
            try:
                # connect/事务/提交都在线程中执行，不占用发送 SSE 的事件循环。
                await asyncio.to_thread(self._event_repository.append_batch, events, self._user_id)
                return
            except Exception:
                logger.warning("Run %s 的 %d 条辅助日志写入失败（第 %d 次）",
                               self._run_id, len(events), attempt + 1, exc_info=True)
                if attempt + 1 < self._max_attempts and not self._abandon:
                    try:
                        await asyncio.wait_for(self._wake.wait(), self._retry_delay * (2 ** attempt))
                    except TimeoutError:
                        pass
                    self._wake.clear()
        self.dropped_events += len(events)
        logger.warning("Run %s 放弃 %d 条辅助日志；Run 结果由关键状态决定",
                       self._run_id, len(events))

    async def close(self) -> None:
        """停止接收事件，有限等待剩余日志；不会取消已经开始的 SQLite 提交。"""
        self._closing = True
        self._wake.set()
        if self._worker is None:
            return
        done, _ = await asyncio.wait({self._worker}, timeout=self._close_timeout)
        if not done:
            self._abandon = True
            self._wake.set()
            queued = len(self._buffer)
            self.dropped_events += queued
            self._pending_count -= queued
            self._pending_bytes -= sum(size for _, size in self._buffer)
            self._buffer.clear()
            logger.warning("Run %s 日志收尾超过 %.2f 秒，停止后续重试；在途写入继续观察",
                           self._run_id, self._close_timeout)

    @staticmethod
    async def shutdown_writers(timeout: float = 2.0) -> None:
        """应用退出时再次等待在途写入；不把后台异常留给事件循环。"""
        tasks = {task for task in _writers if task.get_loop() is asyncio.get_running_loop()}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                logger.warning("退出时仍有 %d 个辅助日志写入未结束", len(pending))
