



import asyncio
from dataclasses import dataclass, field
import logging
import math
import os
from typing import Any, Literal, List
from collections.abc import AsyncIterator


logger = logging.getLogger(__name__)


@dataclass(frozen=True) #创建后不能更改
class StreamEvent:
    # 持久化事件使用 RunEvent.id；metadata/心跳等控制消息使用空字符串。
    id: str
    event:str #事件分类，例如 "run_event"
    data: dict[str, Any] #具体内容，例如：{ "tool_name": "read_file"}，这里就是RunEvent对象的to_dict()方法返回的字典

  



@dataclass
class _RunStream:
    events: List[StreamEvent] = field(default_factory=list) #每次创建一个实例，都默认生成一个新的list
    condition: asyncio.Condition = field(default_factory=asyncio.Condition) #锁
    ended: bool = False
    next_id: int = 1 #下一个事件的ID



class MemoryStreamBridge:
    def __init__(self, retention_seconds: float | None = None) -> None:
        self._streams: dict[str, _RunStream] = {}
        self._cleanup_tasks: dict[str, asyncio.Task[None]] = {}
        self._retention_seconds = (
            self._load_retention_seconds()
            if retention_seconds is None
            else retention_seconds
        )
        if (
            not math.isfinite(self._retention_seconds)
            or self._retention_seconds < 0
        ):
            raise ValueError("retention_seconds 必须是大于等于 0 的有限数字")


    #获取或者创建一个 RunStream
    def _get_or_create_stream(self, run_id: str) -> _RunStream:
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()

        return self._streams[run_id]


    #重连时返回最后收到的事件的数组索引+1
    def _resolve_start_index(self,run_id:str,last_event_id:str | None) -> int:
        stream = self._get_or_create_stream(run_id)

        if last_event_id is None:
            return 0

        for index, event in enumerate(stream.events):
            if event.id == last_event_id:
                return index + 1

        return 0



    #异步发布者
    async def publish(
        self,
        run_id: str,
        event: str,  #传输层的事件分类，例如具体的run_event
        data: dict[str, Any], #具体的事件，data = {
   #             "id": "事件编号",
    #            "run_id": "本次运行编号",
      #          "event_type": "tool.start",
          #      "payload": {"tool_name": "read_file"},
            #    "sequence": 3,
#}
        ) -> None:

        #根据run_id获取或者创建一个 RunStream
        stream = self._get_or_create_stream(run_id)

        #等待通知，获得锁，
        async with stream.condition:

            # 持久化 RunEvent 必须沿用数据库中的 UUID。
            # 这样服务重启后，Last-Event-ID 仍能在 SQLite 中找到。
            event_id = ""
            if event == "run_event" and isinstance(data.get("id"), str):
                event_id = data["id"]

            stream_event = StreamEvent(
                id=event_id,
                event=event, #事件分类
                data=data, #具体事件
            )

            #事件追加到 RunStream 中
            stream.events.append(stream_event)

            #更新 next_id，下一次事件的 id
            stream.next_id += 1


            #通知所有等待的订阅者
            stream.condition.notify_all()



    #转态变为end
    async def publish_end(self, run_id: str) -> None: 
        
        #获取或者创建一个 RunStream
        stream = self._get_or_create_stream(run_id)

        #等待通知
        async with stream.condition:
            stream.ended = True

            #通知所有等待的订阅者
            stream.condition.notify_all()

        self._schedule_cleanup(run_id, stream)

    async def stream_exists(self, run_id: str) -> bool:
        """返回当前进程是否仍保留指定 Run 的内存 Stream。"""
        return run_id in self._streams

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        """延迟删除一个 Run 的内存事件，同时保护同 ID 的替换对象。"""
        stream = self._streams.get(run_id)
        if stream is None:
            return
        if delay > 0:
            await asyncio.sleep(delay)

        # 延迟期间若该键被替换，旧任务不能误删新对象。
        if self._streams.get(run_id) is stream:
            self._streams.pop(run_id, None)

    async def close(self) -> None:
        """取消并等待所有延迟清理任务，然后释放全部 Stream。"""
        tasks = list(self._cleanup_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._cleanup_tasks.clear()
        self._streams.clear()

    def _schedule_cleanup(self, run_id: str, stream: _RunStream) -> None:
        """为一个结束 Stream 幂等创建延迟清理任务。"""
        existing = self._cleanup_tasks.get(run_id)
        if existing is not None and not existing.done():
            return

        async def cleanup_ended_stream() -> None:
            if self._retention_seconds > 0:
                await asyncio.sleep(self._retention_seconds)
            if self._streams.get(run_id) is stream:
                self._streams.pop(run_id, None)

        task = asyncio.create_task(
            cleanup_ended_stream(),
            name=f"stream-cleanup-{run_id}",
        )
        self._cleanup_tasks[run_id] = task
        task.add_done_callback(
            lambda finished, cleanup_run_id=run_id: self._consume_cleanup_task(
                cleanup_run_id,
                finished,
            )
        )

    def _consume_cleanup_task(
        self,
        run_id: str,
        task: asyncio.Task[None],
    ) -> None:
        """移除已完成任务并读取异常，避免 asyncio 警告。"""
        if self._cleanup_tasks.get(run_id) is task:
            self._cleanup_tasks.pop(run_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "清理 Run Stream 失败：%s",
                run_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    @staticmethod
    def _load_retention_seconds() -> float:
        raw_value = os.getenv("DEER_MINI_STREAM_RETENTION_SECONDS", "60")
        try:
            return float(raw_value)
        except ValueError as error:
            raise ValueError(
                "DEER_MINI_STREAM_RETENTION_SECONDS 必须是数字"
            ) from error


    #异步订阅者,15s心跳，没隔15s就会返回一个心跳事件
    async def subscribe(self, 
                        run_id: str,
                        last_event_id: str | None = None,
                        heartbeat_interval: float = 15.0,) -> AsyncIterator[StreamEvent]: #这是一个异步的“多次产出器”，每次yield一条流事件
        #获取或者创建一个 RunStream
        stream = self._get_or_create_stream(run_id)

        #下一个事件的索引，没有事件就是0
        next_index = self._resolve_start_index(run_id, last_event_id)
        

        while True:

            #获取RunStream的锁，保证在等待通知和处理事件时不会被其他协程修改
            async with stream.condition:
                time_out = False
                #如果下一个事件的索引大于等于当前事件列表的长度，说明没有新的事件产生，需要等待通知 
                while next_index >= len(stream.events):

                    #是否已经结束，如果已经结束，则直接返回，结束订阅
                    if stream.ended:
                        return


                    try:
                        #等待通知，直到有新的事件产生或者超时，wait会释放condition，同时自己进入前等待，当有新的事件产生时，notify_all会唤醒所有等待的协程，重新获取condition锁，然后继续执行
                        await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
                    except asyncio.TimeoutError:
                        time_out = True
                        break

                if time_out:
                    #如果超时，则返回一个心跳事件
                    stream_event = StreamEvent(
                        id="",
                        event="__heartbeat__",
                        data={},
                    )
                    

                else:
                    #获取当前事件
                    stream_event = stream.events[next_index]

                    #更新下一个事件的索引
                    next_index += 1
                    #推送消息


            #yield 会把控制权交给订阅者，并暂停当前函数。但它暂停时仍在 async with stream.condition 内，等于一直占着conditon锁。
            #yield要在with外面，否则会导致其他协程无法获取condition锁，无法继续执行。
            yield stream_event
