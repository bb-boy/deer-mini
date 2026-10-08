"""验证搜索→选网址→读取正文→继续回答，及其 SQLite/实时流记录。"""

import asyncio
import json

import httpx
import pytest

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


PAGE_URL = "https://docs.python.org/3/library/asyncio-task.html"
PAGE_BODY = "正文独有信息：TaskGroup 会等待组内所有任务结束。"


class SearchReadAnswerModel:
    """受控模型验证数据流；Tavily SDK、工具、Agent 和 Runtime 都使用真实实现。"""

    def __init__(self, fetch_fails):
        self.fetch_fails = fetch_fails
        self.calls = 0
        self.closed = False

    async def chat(self, messages, tools, on_text_delta=None, **kwargs):
        self.calls += 1
        assert {"web_search", "web_fetch"} <= {tool.name for tool in tools}
        assert "MANDATORY after web_search, web_fetch" in messages[0].content
        if self.calls == 1:
            return Message(role="assistant", content="", tool_calls=[
                ToolCall(id="call-search", name="web_search", arguments={"query": "Python TaskGroup 官方文档"}),
            ])
        assert messages[-1].role == "tool"
        if self.calls == 2:
            assert messages[-1].tool_call_id == "call-search"
            assert PAGE_BODY not in messages[-1].content
            source = json.loads(messages[-1].content)[0]
            return Message(role="assistant", content="", tool_calls=[
                ToolCall(id="call-fetch", name="web_fetch", arguments={"url": source["url"]}),
            ])
        assert self.calls == 3
        assert messages[-1].tool_call_id == "call-fetch"
        if self.fetch_fails:
            assert messages[-1].content == "Error: Page blocked"
            content = "网页读取失败，请换一个可访问的链接。"
        else:
            assert messages[-1].content == "# TaskGroup 文档\n\n" + PAGE_BODY
            content = f"{PAGE_BODY} [citation:TaskGroup 文档]({PAGE_URL})"
        if on_text_delta:
            await on_text_delta(content)
        return Message(role="assistant", content=content)

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("fetch_fails", [False, True])
def test_search_then_fetch_returns_body_or_error_to_model(monkeypatch, tmp_path, fetch_fails):
    monkeypatch.setenv("TAVILY_API_KEY", "test-fetch-key")
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "deer_mini.db"))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "users"))
    database.initialize_database()
    model = SearchReadAnswerModel(fetch_fails)
    monkeypatch.setattr(ModelFactory, "create_chat_model", lambda self: model)
    requests = []
    clients = []
    original_client = httpx.AsyncClient

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{
                "title": "TaskGroup 文档", "url": PAGE_URL, "content": "这里只是搜索摘要",
            }]})
        assert request.url.path == "/extract"
        assert json.loads(request.content)["urls"] == [PAGE_URL]
        if fetch_fails:
            return httpx.Response(200, json={"results": [], "failed_results": [{"error": "Page blocked"}]})
        return httpx.Response(200, json={"results": [{
            "title": "TaskGroup 文档", "url": PAGE_URL, "raw_content": PAGE_BODY,
        }]})

    def create_client(*args, **kwargs):
        kwargs.pop("mounts", None)
        kwargs.update(trust_env=False, transport=httpx.MockTransport(respond))
        client = original_client(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", create_client)
    thread = ThreadService().create_thread("fetch-user", "网页读取闭环验证")

    async def execute():
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, run_timeout_seconds=5)
        try:
            run = await coordinator.create_and_start_run(
                user_id="fetch-user", thread_id=thread.id, message="搜索并阅读 TaskGroup 官方文档",
                model_name="test-model", thinking_enabled=False, reasoning_effort=None,
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
    assert requests == ["/search", "/extract"]
    assert model.calls == 3 and model.closed
    assert len(clients) == 2 and all(client.is_closed for client in clients)
    assert RunRepository().get(run.id, "fetch-user").status == "success"
    assert ThreadRepository().get(thread.id, "fetch-user").status == "idle"
    messages = [message for message in state.messages if message.role != "system"]
    assert [message.role for message in messages] == [
        "user", "assistant", "tool", "assistant", "tool", "assistant",
    ]
    assert messages[3].tool_calls[0].arguments["url"] == PAGE_URL
    assert messages[4].tool_call_id == "call-fetch"
    checkpoints = CheckpointRepository().history(thread.id, "fetch-user", run.id)
    assert [checkpoint.step for checkpoint in checkpoints] == list(range(1, 14))
    assert checkpoints[-1].state.compression.last_api_at is not None
    fetch_snapshots = [cp for cp in checkpoints if cp.state.messages[-1].id == messages[4].id]
    assert fetch_snapshots and all(cp.state.messages[-1] == messages[4] for cp in fetch_snapshots)
    assert checkpoints[-1].state.messages[-1].content == messages[-1].content
    logs = EventRepository().list_for_run(thread.id, run.id, "fetch-user")
    tool_ends = [event for event in logs if event.event_type == "tool.end"]
    assert [event.payload["tool_name"] for event in tool_ends] == ["web_search", "web_fetch"]
    assert tool_ends[1].payload["content"] == messages[4].content
    assert any(event.data.get("event_type") == "text.delta" for event in streamed)
    assert any(event.data.get("event_type") == "run.end" for event in streamed)
