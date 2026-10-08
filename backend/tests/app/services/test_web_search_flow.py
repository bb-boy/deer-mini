"""验证搜索经由真实 Coordinator、Agent、SQLite 和 StreamBridge 形成闭环。"""

import asyncio
import json

import httpx

from app.domain.messages import Message, ToolCall
from app.infrastructure import database
from app.model.factory import ModelFactory
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.events_repository import EventRepository
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_coordinator import RunCoordinator
from app.services.thread_service import ThreadService


class SearchThenAnswerModel:
    """受控模型先调用搜索，再检查实际工具消息；不向模型服务发起付费请求。"""

    def __init__(self):
        self.calls = 0
        self.closed = False

    async def chat(self, messages, tools, on_text_delta=None, **kwargs):
        self.calls += 1
        assert "web_search" in [tool.name for tool in tools]
        assert "<citations>" in messages[0].content
        assert "test-search-key" not in messages[0].content
        if self.calls == 1:
            return Message(role="assistant", content="", tool_calls=[
                ToolCall(id="call-search", name="web_search", arguments={"query": "Python asyncio"}),
            ])
        assert messages[-1].role == "tool"
        assert messages[-1].tool_call_id == "call-search"
        sources = json.loads(messages[-1].content)
        assert sources[0]["snippet"] == "asyncio 官方说明"
        content = f"[citation:{sources[0]['title']}]({sources[0]['url']})"
        if on_text_delta is not None:
            await on_text_delta(content)
        return Message(role="assistant", content=content)

    async def close(self):
        self.closed = True


def test_search_result_returns_to_model_and_is_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "test-search-key")
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "deer_mini.db"))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "users"))
    database.initialize_database()
    model = SearchThenAnswerModel()
    monkeypatch.setattr(ModelFactory, "create_chat_model", lambda self: model)

    original_client = httpx.AsyncClient
    clients = []

    def create_client(*args, **kwargs):
        kwargs.pop("mounts", None)
        kwargs.update(trust_env=False, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": [{
                "title": "asyncio", "url": "https://docs.python.org/3/library/asyncio.html",
                "content": "asyncio 官方说明",
            }]}),
        ))
        client = original_client(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", create_client)
    thread = ThreadService().create_thread("search-user", "搜索闭环验证")

    async def execute():
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, run_timeout_seconds=5)
        try:
            run = await coordinator.create_and_start_run(
                user_id="search-user", thread_id=thread.id,
                message="搜索 asyncio 官方文档", model_name="test-model",
                thinking_enabled=False, reasoning_effort=None,
            )
            task = coordinator._tasks[run.id]
            async with asyncio.timeout(10):
                events = [event async for event in bridge.subscribe(run.id)]
                state = await task
            return run, state, events
        finally:
            await coordinator.shutdown()
            await bridge.close()

    run, state, streamed = asyncio.run(execute())
    assert model.calls == 2
    assert model.closed
    assert clients and all(client.is_closed for client in clients)
    assert RunRepository().get(run.id, "search-user").status == "success"
    assert ThreadRepository().get(thread.id, "search-user").status == "idle"
    messages = [message for message in state.messages if message.role != "system"]
    assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls[0].name == "web_search"
    assert messages[2].tool_call_id == messages[1].tool_calls[0].id
    assert "https://docs.python.org/3/library/asyncio.html" in messages[-1].content

    checkpoints = CheckpointRepository().history(thread.id, "search-user", run.id)
    assert [checkpoint.step for checkpoint in checkpoints] == list(range(1, 10))
    assert checkpoints[-1].state.compression.last_api_at is not None
    tool_snapshots = [cp for cp in checkpoints if cp.state.messages[-1].id == messages[2].id]
    assert tool_snapshots and all(cp.state.messages[-1] == messages[2] for cp in tool_snapshots)
    assert checkpoints[-1].state.messages[-1].content == messages[-1].content
    logs = EventRepository().list_for_run(thread.id, run.id, "search-user")
    assert [event.payload["phase"] for event in logs if event.event_type == "model.status"] == [
        "attempt", "complete", "attempt", "complete",
    ]
    logs = [event for event in logs if event.event_type != "model.status"]
    assert [event.event_type for event in logs] == [
        "run.start", "message.complete", "tool.start", "tool.end", "message.complete", "run.end",
    ]
    assert logs[3].payload["content"] == messages[2].content
    streamed_types = [event.data.get("event_type") for event in streamed]
    assert "tool.start" in streamed_types
    assert "tool.end" in streamed_types
    assert "text.delta" in streamed_types
    assert "run.end" in streamed_types
    assert any(event.event == "metadata" and event.data["run_id"] == run.id for event in streamed)
