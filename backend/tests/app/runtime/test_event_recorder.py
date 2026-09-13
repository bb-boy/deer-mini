"""真实 SQLite 批次、慢写入、失败重试和有界收尾的回归测试。"""

import asyncio
import threading

import pytest

from app.domain.events import RunEvent
from app.infrastructure import database
from app.repositories.events_repository import EventRepository
from app.runtime.event_recorder import EventRecorder
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services import thread_service
from app.services.run_service import RunService


@pytest.fixture
def storage(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "events.db")
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()
    thread = thread_service.ThreadService().create_thread("alice", "events")
    run = RunService().create_run("alice", thread.id, "test")
    return thread, run


class GatedRepository(EventRepository):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def append_batch(self, events, user_id):
        self.calls += 1
        self.started.set()
        if not self.release.wait(3):
            raise AssertionError("测试未释放写入")
        return super().append_batch(events, user_id)


@pytest.mark.parametrize("event_type", ["text.delta", "reasoning.delta"])
def test_live_text_arrives_while_database_write_is_blocked(storage, event_type):
    thread, run = storage
    repo = GatedRepository()

    async def scenario():
        bridge = MemoryStreamBridge()
        recorder = EventRecorder("alice", thread.id, run.id, bridge, repo, batch_size=1)
        try:
            await recorder.record_event("run.start", {})
            assert await asyncio.to_thread(repo.started.wait, 2)
            event = await asyncio.wait_for(
                recorder.record_event(event_type, {"text": "立即显示"}), 0.2,
            )
            assert event.sequence is None
            received = []
            async for item in bridge.subscribe(run.id):
                received.append(item)
                if item.data.get("event_type") == event_type:
                    break
            assert received[-1].data["payload"]["text"] == "立即显示"
            assert received[-1].id != event.id
            assert EventRepository().list_for_run(thread.id, run.id, "alice") == []
            closing = asyncio.create_task(recorder.close())
            await asyncio.sleep(0.01)
            assert not closing.done(), "正常收尾必须等待在途写入"
            repo.release.set()
            await closing
            assert [item.event_type for item in repo.list_for_run(thread.id, run.id, "alice")] == ["run.start"]
        finally:
            repo.release.set()
            await recorder.close()
            await bridge.close()

    asyncio.run(scenario())


def test_retry_after_committed_batch_is_idempotent_and_ordered(storage):
    thread, run = storage

    class CommitThenFail(EventRepository):
        calls = 0

        def append_batch(self, events, user_id):
            self.calls += 1
            saved = super().append_batch(events, user_id)
            if self.calls == 1:
                raise OSError("模拟提交成功后调用方未收到结果")
            return saved

    async def scenario():
        bridge = MemoryStreamBridge()
        repo = CommitThenFail()
        recorder = EventRecorder("alice", thread.id, run.id, bridge, repo, flush_interval=1)
        emitted = [await recorder.record_event(kind, {"n": index}) for index, kind in enumerate(
            ["run.start", "tool.start", "tool.end", "run.end"]
        )]
        await recorder.close()
        saved = repo.list_for_run(thread.id, run.id, "alice")
        assert [event.id for event in saved] == [event.id for event in emitted]
        assert [event.sequence for event in saved] == [1, 2, 3, 4]
        assert all(event.sequence is None for event in emitted)
        assert repo.calls == 2
        await bridge.close()

    asyncio.run(scenario())


def test_permanent_log_failure_is_bounded_and_warns(storage, caplog):
    thread, run = storage

    class BrokenRepository:
        calls = 0

        def append_batch(self, events, user_id):
            self.calls += 1
            raise OSError("日志磁盘故障")

    async def scenario():
        bridge = MemoryStreamBridge()
        repo = BrokenRepository()
        recorder = EventRecorder("alice", thread.id, run.id, bridge, repo,
                                 retry_delay=0.005, flush_interval=1)
        await recorder.record_event("run.end", {"status": "success"})
        await recorder.close()
        assert repo.calls == 3
        assert recorder.dropped_events == 1
        await bridge.publish_end(run.id)
        events = [event async for event in bridge.subscribe(run.id)]
        assert events[-1].data["event_type"] == "run.end"
        await bridge.close()

    asyncio.run(scenario())
    assert "辅助日志" in caplog.text


def test_queue_limit_includes_inflight_batch_and_does_not_drop_stream(storage):
    thread, run = storage
    repo = GatedRepository()

    async def scenario():
        bridge = MemoryStreamBridge()
        recorder = EventRecorder("alice", thread.id, run.id, bridge, repo,
                                 batch_size=1, max_buffer_events=2)
        try:
            await recorder.record_event("tool.start", {"n": 0})
            assert await asyncio.to_thread(repo.started.wait, 2)
            for n in range(1, 10):
                await recorder.record_event("tool.start", {"n": n})
            assert recorder._pending_count == 2
            assert recorder.dropped_events == 8
            repo.release.set()
            await recorder.close()
            await bridge.publish_end(run.id)
            assert len([event async for event in bridge.subscribe(run.id)]) == 10
            assert len(repo.list_for_run(thread.id, run.id, "alice")) == 2
        finally:
            repo.release.set()
            await recorder.close()
            await bridge.close()

    asyncio.run(scenario())


def test_oversize_log_still_streams_and_payload_is_a_snapshot(storage):
    thread, run = storage

    async def scenario():
        bridge = MemoryStreamBridge()
        recorder = EventRecorder("alice", thread.id, run.id, bridge, max_buffer_bytes=32)
        payload = {"arguments": {"items": ["before"]}}
        await recorder.record_event("tool.start", payload)
        payload["arguments"]["items"].append("after")
        await recorder.close()
        await bridge.publish_end(run.id)
        events = [event async for event in bridge.subscribe(run.id)]
        assert events[0].data["payload"]["arguments"]["items"] == ["before"]
        assert recorder.dropped_events == 1
        await bridge.close()

    asyncio.run(scenario())


def test_close_timeout_stops_new_batches_but_observes_inflight_write(storage):
    thread, run = storage
    repo = GatedRepository()

    async def scenario():
        bridge = MemoryStreamBridge()
        recorder = EventRecorder("alice", thread.id, run.id, bridge, repo,
                                 batch_size=1, close_timeout=0.02)
        try:
            await recorder.record_event("tool.start", {"n": 1})
            assert await asyncio.to_thread(repo.started.wait, 2)
            await recorder.record_event("tool.end", {"n": 2})
            await asyncio.wait_for(recorder.close(), 0.3)
            assert recorder.dropped_events == 1
            assert not recorder._worker.done()
            repo.release.set()
            await EventRecorder.shutdown_writers()
            assert recorder._worker.done()
            assert repo.calls == 1
            assert len(repo.list_for_run(thread.id, run.id, "alice")) == 1
        finally:
            repo.release.set()
            await EventRecorder.shutdown_writers()
            await bridge.close()

    asyncio.run(scenario())


def test_partial_batch_flushes_without_waiting_for_run_end(storage):
    thread, run = storage

    class ObservedRepository(EventRepository):
        saved = threading.Event()

        def append_batch(self, events, user_id):
            result = super().append_batch(events, user_id)
            self.saved.set()
            return result

    async def scenario():
        bridge = MemoryStreamBridge()
        repo = ObservedRepository()
        recorder = EventRecorder("alice", thread.id, run.id, bridge, repo, flush_interval=0.01)
        await recorder.record_event("run.start", {})
        assert await asyncio.to_thread(repo.saved.wait, 2)
        assert not recorder._closing
        await recorder.close()
        await bridge.close()

    asyncio.run(scenario())


def test_batch_rollback_and_ownership_checks(storage):
    thread, run = storage
    repo = EventRepository()
    first = RunEvent(run.id, thread.id, "tool.start", {"n": 1})
    conflicting = RunEvent(run.id, thread.id, "tool.end", {"n": 2}, id=first.id)
    with pytest.raises(ValueError, match="其他内容"):
        repo.append_batch([first, conflicting], "alice")
    assert first.sequence is None
    assert repo.list_for_run(thread.id, run.id, "alice") == []
    with pytest.raises(ValueError, match="given user"):
        repo.append_batch([first], "mallory")
    assert repo.list_for_run(thread.id, run.id, "alice") == []
