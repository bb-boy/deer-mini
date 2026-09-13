"""单进程内存实时流：独立游标、有界回放、结束后延迟清理。"""

import asyncio
import json
import logging
import math
import os
from collections import deque
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from app.domain.common import new_id


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamEvent:
    # id 是实时流游标；data 中的 RunEvent.id/sequence 有各自的含义。
    id: str
    event: str
    data: dict[str, Any]


@dataclass
class _RunStream:
    events: deque[tuple[int, StreamEvent, int]] = field(default_factory=deque)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    epoch: str = field(default_factory=new_id)
    ended: bool = False
    next_id: int = 1
    buffered_bytes: int = 0

    @property
    def first_id(self) -> int:
        return self.events[0][0] if self.events else self.next_id


class MemoryStreamBridge:
    def __init__(
        self, retention_seconds: float | None = None,
        *, max_events: int = 4096, max_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._streams: dict[str, _RunStream] = {}
        self._cleanup_tasks: dict[str, asyncio.Task[None]] = {}
        self._retention_seconds = (
            self._load_retention_seconds() if retention_seconds is None else retention_seconds
        )
        if not math.isfinite(self._retention_seconds) or self._retention_seconds < 0:
            raise ValueError("retention_seconds 必须是大于等于 0 的有限数字")
        if max_events < 1 or max_bytes < 1:
            raise ValueError("实时流缓存上限必须大于 0")
        self._max_events, self._max_bytes = max_events, max_bytes

    def _get_or_create_stream(self, run_id: str) -> _RunStream:
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()
        return self._streams[run_id]

    @staticmethod
    def _resolve_cursor(stream: _RunStream, cursor: str | None) -> tuple[int, str | None]:
        """游标只用于当前内存流，不能拿数据库 sequence 来续传。"""
        if not cursor:
            return stream.first_id, "buffer_expired" if stream.first_id > 1 else None
        try:
            prefix, epoch, number = cursor.split(":")
            sequence = int(number)
            if (prefix == "s" and epoch == stream.epoch
                    and 1 <= sequence < stream.next_id
                    and str(sequence) == number):
                if sequence + 1 >= stream.first_id:
                    return sequence + 1, None
                return stream.first_id, "buffer_expired"
        except (ValueError, AttributeError):
            pass
        return stream.first_id, "cursor_lost"

    async def publish(self, run_id: str, event: str, data: dict[str, Any]) -> None:
        """将内容装进 StreamEvent 并唤醒订阅者；不访问 SQLite。"""
        stream = self._get_or_create_stream(run_id)
        # 与调用方的可变字典分开，后续日志提交不能修改已发布的内容。
        snapshot = deepcopy(data)
        size = len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
        async with stream.condition:
            if stream.ended:
                raise RuntimeError("不能向已结束的 Stream 追加事件")
            sequence = stream.next_id
            entry = StreamEvent(
                id=f"s:{stream.epoch}:{sequence}", event=event, data=snapshot,
            )
            stream.next_id += 1
            stream.events.append((sequence, entry, size))
            stream.buffered_bytes += size
            # 单条大事件也不能绕过缓存限制；缺失时客户端会恢复 Checkpoint。
            while (len(stream.events) > self._max_events
                   or stream.buffered_bytes > self._max_bytes):
                _, _, removed_size = stream.events.popleft()
                stream.buffered_bytes -= removed_size
            stream.condition.notify_all()
        # 模型 SDK 可能一次交出许多已缓冲片段。显式让出执行机会，SSE 才能及时读取。
        await asyncio.sleep(0)

    async def publish_end(self, run_id: str) -> None:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            stream.ended = True
            stream.condition.notify_all()
        self._schedule_cleanup(run_id, stream)

    async def stream_exists(self, run_id: str) -> bool:
        return run_id in self._streams

    async def subscribe(
        self, run_id: str, last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        """按实时游标续传；缓存缺口明确通知 API 恢复状态，绝不伪装成完整回放。"""
        stream = self._get_or_create_stream(run_id)
        next_id, gap = self._resolve_cursor(stream, last_event_id)
        while True:
            async with stream.condition:
                if next_id < stream.first_id:
                    next_id, gap = stream.first_id, "buffer_expired"
                if gap:
                    entry = StreamEvent("", "stream.gap", {"reason": gap})
                    gap = None
                elif next_id < stream.next_id:
                    entry = stream.events[next_id - stream.first_id][1]
                    next_id += 1
                elif stream.ended:
                    return
                else:
                    try:
                        await asyncio.wait_for(stream.condition.wait(), heartbeat_interval)
                    except TimeoutError:
                        entry = StreamEvent("", "__heartbeat__", {})
                    else:
                        continue
            # yield 放在锁外，让发布者可以继续追加下一条。
            yield entry

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        stream = self._streams.get(run_id)
        if stream is None:
            return
        if delay > 0:
            await asyncio.sleep(delay)
        if self._streams.get(run_id) is stream:
            self._streams.pop(run_id, None)

    async def close(self) -> None:
        tasks = list(self._cleanup_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._cleanup_tasks.clear()
        for stream in self._streams.values():
            async with stream.condition:
                stream.ended = True
                stream.condition.notify_all()
        self._streams.clear()

    def _schedule_cleanup(self, run_id: str, stream: _RunStream) -> None:
        existing = self._cleanup_tasks.get(run_id)
        if existing is not None and not existing.done():
            return

        async def cleanup_ended_stream() -> None:
            if self._retention_seconds > 0:
                await asyncio.sleep(self._retention_seconds)
            if self._streams.get(run_id) is stream:
                self._streams.pop(run_id, None)

        task = asyncio.create_task(cleanup_ended_stream(), name=f"stream-cleanup-{run_id}")
        self._cleanup_tasks[run_id] = task
        task.add_done_callback(lambda finished: self._consume_cleanup_task(run_id, finished))

    def _consume_cleanup_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._cleanup_tasks.get(run_id) is task:
            self._cleanup_tasks.pop(run_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("清理 Run Stream 失败：%s", run_id,
                         exc_info=(type(error), error, error.__traceback__))

    @staticmethod
    def _load_retention_seconds() -> float:
        try:
            return float(os.getenv("DEER_MINI_STREAM_RETENTION_SECONDS", "60"))
        except ValueError as error:
            raise ValueError("DEER_MINI_STREAM_RETENTION_SECONDS 必须是数字") from error
