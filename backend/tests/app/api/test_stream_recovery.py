"""SSE 游标、状态恢复和独立终态通知，使用临时 SQLite。"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import router
from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.events_repository import EventRepository
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services import thread_service
from app.services.run_service import RunService


@pytest.fixture
def storage(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "stream.db")
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()
    thread = thread_service.ThreadService().create_thread("alice", "stream")
    run = RunService().create_run("alice", thread.id, "test")
    RunService().start_run(run.id, "alice")
    state = ThreadState(thread_id=thread.id, user_id="alice", messages=[
        Message(role="user", content="你好"),
        Message(role="assistant", content="完整回答", id="saved-answer"),
    ], workspace_path=thread.workspace_path)
    CheckpointRepository().save(Checkpoint(thread.id, run.id, 1, state))
    return thread, run


def frames(body):
    result = []
    for block in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "data" in fields:
            result.append((fields.get("event"), json.loads(fields["data"]), fields.get("id")))
    return result


async def request_stream(bridge, storage, cursor=None, user="alice"):
    thread, run = storage
    app = FastAPI()
    app.state.stream_bridge = bridge
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await asyncio.wait_for(client.get(
            f"/api/threads/{thread.id}/runs/{run.id}/events",
            params={"user_id": user},
            headers={"Last-Event-ID": cursor} if cursor else {},
        ), 3)


async def publish(bridge, storage, kind, payload):
    thread, run = storage
    event = RunEvent(run.id, thread.id, kind, payload)
    await bridge.publish(run.id, "run_event", event.to_dict())
    return event


def test_live_reconnect_uses_memory_cursor_without_reading_log_table(storage, monkeypatch):
    thread, run = storage

    def unexpected_read(*args):
        raise AssertionError("内存续传不应该扫描日志表")

    monkeypatch.setattr(EventRepository, "list_for_run", unexpected_read)

    async def scenario():
        bridge = MemoryStreamBridge()
        await publish(bridge, storage, "run.start", {})
        await publish(bridge, storage, "text.delta", {"text": "后半段", "message_id": "new-answer"})
        RunService().finish_run(run.id, "alice", "success")
        await publish(bridge, storage, "run.end", {"status": "success", "status_confirmed": True})
        await bridge.publish_end(run.id)
        existing = [item async for item in bridge.subscribe(run.id)]
        response = await request_stream(bridge, storage, existing[0].id)
        data = frames(response.text)
        assert response.status_code == 200
        assert [payload["event_type"] for event, payload, _ in data] == ["text.delta", "run.end"]
        assert [cursor for _, _, cursor in data] == [existing[1].id, existing[2].id]
        await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("logs_fail", [False, True])
def test_restart_recovers_checkpoint_and_success_even_with_no_terminal_log(storage, monkeypatch, logs_fail):
    thread, run = storage
    RunService().finish_run(run.id, "alice", "success")
    if logs_fail:
        def failure(*args):
            raise OSError("历史日志不可用")
        monkeypatch.setattr(EventRepository, "list_for_run", failure)

    async def scenario():
        bridge = MemoryStreamBridge()
        response = await request_stream(bridge, storage, "s:old-process:123")
        data = frames(response.text)
        assert data[0][0] == "stream.reset"
        assert data[0][1]["checkpoint"]["state"]["messages"][-1]["content"] == "完整回答"
        assert data[-1][1]["event_type"] == "run.end"
        assert data[-1][1]["payload"]["status_confirmed"] is True
        await bridge.close()

    asyncio.run(scenario())


def test_retention_gap_is_explicit_and_restores_saved_message_identity(storage):
    thread, run = storage

    async def scenario():
        bridge = MemoryStreamBridge(max_events=2)
        for n in range(4):
            await publish(bridge, storage, "text.delta", {"text": str(n), "message_id": "saved-answer"})
        RunService().finish_run(run.id, "alice", "success")
        await publish(bridge, storage, "run.end", {"status": "success"})
        await bridge.publish_end(run.id)
        response = await request_stream(bridge, storage, "s:unknown:1")
        data = frames(response.text)
        assert data[0][0] == "stream.reset"
        assert data[0][1]["checkpoint"]["state"]["messages"][-1]["id"] == "saved-answer"
        assert len([item for item in data if item[0] == "run_event"]) == 2
        await bridge.close()

    asyncio.run(scenario())


def test_retention_gap_restores_saved_tool_progress_before_live_tail(storage):
    thread, run = storage

    async def scenario():
        bridge = MemoryStreamBridge(max_events=2)
        tool_start = await publish(bridge, storage, "tool.start", {
            "tool_call_id": "call-1", "tool_name": "read_file", "arguments": {"path": "report.txt"},
        })
        tool_end = await publish(bridge, storage, "tool.end", {
            "tool_call_id": "call-1", "tool_name": "read_file", "content": "文件内容",
        })
        EventRepository().append_batch([tool_start, tool_end], "alice")
        subscription = bridge.subscribe(run.id)
        cursor = (await anext(subscription)).id
        await subscription.aclose()

        for n in range(3):
            await publish(bridge, storage, "text.delta", {"text": str(n), "message_id": "saved-answer"})
        RunService().finish_run(run.id, "alice", "success")
        await publish(bridge, storage, "run.end", {"status": "success", "status_confirmed": True})
        await bridge.publish_end(run.id)

        response = await request_stream(bridge, storage, cursor)
        data = frames(response.text)
        assert data[0][0] == "stream.reset"
        assert data[0][1]["reason"] == "buffer_expired"
        events = [payload for name, payload, _ in data if name == "run_event"]
        assert [event["event_type"] for event in events] == [
            "tool.start", "tool.end", "text.delta", "run.end",
        ]
        assert events[0]["id"] == tool_start.id
        assert events[1]["payload"]["content"] == "文件内容"
        await bridge.close()

    asyncio.run(scenario())


def test_history_deltas_and_old_terminal_logs_do_not_override_actual_status(storage):
    thread, run = storage
    EventRepository().append_batch([
        RunEvent(run.id, thread.id, "text.delta", {"text": "历史片段"}),
        RunEvent(run.id, thread.id, "run.error", {"message": "旧日志"}),
    ], "alice")
    RunService().finish_run(run.id, "alice", "success")

    async def scenario():
        bridge = MemoryStreamBridge()
        response = await request_stream(bridge, storage)
        data = frames(response.text)
        kinds = [payload.get("event_type") for event, payload, _ in data]
        assert "text.delta" not in kinds
        assert "run.error" not in kinds
        assert kinds[-1] == "run.end"
        await bridge.close()

    asyncio.run(scenario())


def test_closed_stream_with_unconfirmed_run_does_not_imply_success(storage):
    thread, run = storage

    async def scenario():
        bridge = MemoryStreamBridge()
        await bridge.publish_end(run.id)
        response = await request_stream(bridge, storage)
        data = frames(response.text)
        assert data[-1][0] == "stream.unconfirmed"
        assert not any(payload.get("event_type") == "run.end" for _, payload, _ in data)
        await bridge.close()

    asyncio.run(scenario())


def test_sse_checks_ownership_before_returning_snapshot(storage):
    async def scenario():
        bridge = MemoryStreamBridge()
        response = await request_stream(bridge, storage, user="mallory")
        assert response.status_code == 404
        assert "完整回答" not in response.text
        await bridge.close()

    asyncio.run(scenario())
