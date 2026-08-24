"""Tests for backend/app/runtime/stream_bridge.py."""

# 在这里编写 pytest 的 test_* 函数。

import asyncio

from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.thread_service import ThreadService
from app.services.run_service import RunService

def test_stream_bridge_publish_and_subscribe():
    # 这里可以编写测试 stream_bridge 的 publish 和 subscribe 方法的代码

    user_id = "alice"
    thread = ThreadService().create_thread(user_id, "test thread")
    run = RunService().create_run(user_id, thread.id, "test-model")
    

    #创建一个内存bridge
    bridge = MemoryStreamBridge()
    runstream = bridge._get_or_create_stream(run.id)
    assert runstream is not None
    assert runstream.events == []
    assert runstream.next_id == 1
    assert type(runstream.condition) is asyncio.Condition

    async def publish_and_subscribe():
        # 发布一个事件
        event_data = {"message": "Hello, World!"}
        event_data2 = {"info": "This is a test"}
        await bridge.publish(run.id, "run_event", event_data)
        await bridge.publish(run.id, "run_event", event_data2)
        await bridge.publish_end(run.id)

        # 订阅事件
        streamed_events = []

        async for stream_event in bridge.subscribe(run.id):
            streamed_events.append(stream_event)

        assert len(streamed_events) == 2

        assert streamed_events[0].event == "run_event"
        assert streamed_events[0].data == event_data

        assert streamed_events[1].event == "run_event"
        assert streamed_events[1].data == event_data2
        
    asyncio.run(publish_and_subscribe())