



import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, List
from collections.abc import AsyncIterator


@dataclass(frozen=True) #创建后不能更改
class StreamEvent:
    id: str #这次 Run 中第几条实时事件，例如 1、2、3
    event:str #事件名称，例如 "run_event"
    data: dict[str, Any] #具体内容，例如：{"event_type": "tool.start", "tool_name": "read_file"}
  



@dataclass
class _RunStream:
    events: List[StreamEvent] = field(default_factory=list) #每次创建一个实例，都默认生成一个新的list
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    ended: bool = False
    next_id: int = 1



class MemoryStreamBridge:
    def __init__(self) -> None:
        self._streams: dict[str, _RunStream] = {}


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
        event: str,
        data: dict[str, Any],
        ) -> None:

        #获取或者创建一个 RunStream
        stream = self._get_or_create_stream(run_id)

        #等待通知
        async with stream.condition:
            stream_event = StreamEvent(
                id=str(stream.next_id),
                event=event,
                data=data,
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


    #异步订阅者,15s心跳，没隔15s就会返回一个心跳事件
    async def subscribe(self, 
                        run_id: str,
                        last_event_id: str | None = None,
                        heartbeat_interval: float = 15.0,) -> AsyncIterator[StreamEvent]: #这是一个异步的“多次产出器”，每次yield一条流事件
        #获取或者创建一个 RunStream
        stream = self._get_or_create_stream(run_id)

        #下一个事件的索引
        next_index = self._resolve_start_index(run_id, last_event_id)
        

        while True:
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
