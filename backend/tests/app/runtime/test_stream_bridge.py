"""MemoryStreamBridge 发布、订阅和延迟清理测试。"""

import asyncio

from app.runtime.stream_bridge import MemoryStreamBridge


def test_stream_bridge_publish_and_subscribe():
    async def scenario():
        bridge = MemoryStreamBridge(retention_seconds=60)
        await bridge.publish("run-basic", "run_event", {"message": "hello"})
        await bridge.publish("run-basic", "run_event", {"message": "world"})
        await bridge.publish_end("run-basic")

        events = [event async for event in bridge.subscribe("run-basic")]

        assert [event.data for event in events] == [
            {"message": "hello"},
            {"message": "world"},
        ]
        await bridge.close()

    asyncio.run(scenario())


def test_ended_stream_is_retained_then_removed():
    async def scenario():
        bridge = MemoryStreamBridge(retention_seconds=0.03)
        await bridge.publish("run-retained", "run_event", {"value": 1})
        await bridge.publish_end("run-retained")

        assert await bridge.stream_exists("run-retained") is True
        await asyncio.sleep(0.05)
        assert await bridge.stream_exists("run-retained") is False
        await bridge.close()

    asyncio.run(scenario())


def test_connected_subscriber_drains_events_even_after_dictionary_cleanup():
    async def scenario():
        bridge = MemoryStreamBridge(retention_seconds=0)
        received = []

        async def consume():
            async for event in bridge.subscribe(
                "run-subscriber",
                heartbeat_interval=1,
            ):
                received.append(event)

        subscriber = asyncio.create_task(consume())
        await asyncio.sleep(0)
        await bridge.publish("run-subscriber", "run_event", {"tail": True})
        await bridge.publish_end("run-subscriber")
        await subscriber
        await asyncio.sleep(0)

        assert [event.data for event in received] == [{"tail": True}]
        assert await bridge.stream_exists("run-subscriber") is False
        await bridge.close()

    asyncio.run(scenario())


def test_publish_end_is_idempotent_and_close_reaps_tasks():
    async def scenario():
        bridge = MemoryStreamBridge(retention_seconds=60)
        await bridge.publish("run-close", "run_event", {"value": 1})

        await bridge.publish_end("run-close")
        await bridge.publish_end("run-close")

        assert len(bridge._cleanup_tasks) == 1
        await bridge.close()
        assert bridge._cleanup_tasks == {}
        assert await bridge.stream_exists("run-close") is False

    asyncio.run(scenario())
