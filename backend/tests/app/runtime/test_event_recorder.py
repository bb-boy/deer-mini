"""Tests for backend/app/runtime/event_recorder.py."""

# 在这里编写 pytest 的 test_* 函数。

from zipfile import Path

import pytest

from app.infrastructure import database
from app.runtime.event_recorder import EventRecorder
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services import thread_service
from app.services.run_service import RunService
import asyncio
from app.repositories.events_repository import EventRepository


@pytest.fixture
def isolated_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """让这一个测试使用独立的数据库和工作目录。"""
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "deer_mini.db")
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()


def test_record_event_persists_and_streams(isolated_storage) -> None:
    user_id = "alice"

    thread = thread_service.ThreadService().create_thread(user_id, "event recorder test")
    run = RunService().create_run(user_id, thread.id, "demo-model")

    bridge = MemoryStreamBridge()

    recorder = EventRecorder(user_id, thread.id, run.id, bridge)

    # 记录一个事件

    async def record_and_check():
        event_type = "run.start"
        payload = {"message": "Run started"}
        event = await recorder.record_event(event_type, payload)

        # 检查事件是否被持久化到数据库中
       
        repo = EventRepository()
        retrieved_event = repo.get(event.id)
        assert retrieved_event is not None
        assert retrieved_event.event_type == event_type
        assert retrieved_event.payload == payload

        # 检查事件是否被广播到流中
        streamed_events = await bridge.subscribe(run.id)
        assert len(streamed_events) > 0
        last_streamed_event = streamed_events[-1]
        assert last_streamed_event["event"] == "run_event"
        assert last_streamed_event["data"]["event_type"] == event_type
        assert last_streamed_event["data"]["payload"] == payload
