"""快照查询不能丢弃子任务记录，也不能把它返回给其他用户。"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import router
from app.context_compression.state import CompressionState
from app.domain.checkpoints import Checkpoint
from app.domain.messages import Message, ToolCall
from app.domain.subagents import SubagentTask
from app.domain.threads import ThreadState
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services import thread_service
from app.services.run_coordinator import RunCoordinator
from app.services.run_service import RunService
from app.services.thread_service import ThreadService


@pytest.fixture
def storage_and_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "api-subtasks.db"))
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()
    thread = ThreadService().create_thread("alice", "子任务查询")
    run = RunService().create_run(thread.user_id, thread.id, "test-model")
    app = FastAPI()
    app.state.run_coordinator = RunCoordinator(MemoryStreamBridge(), bash_runner=None)
    app.include_router(router)
    with TestClient(app) as client:
        yield thread, run, client


def test_latest_and_history_return_complete_child_records(storage_and_client):
    thread, run, client = storage_and_client
    state = ThreadState(
        thread_id=thread.id, user_id=thread.user_id, workspace_path=thread.workspace_path,
        messages=[Message(role="user", content="查阅资料")],
    )
    repository = CheckpointRepository()
    repository.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=1, state=state))
    child = SubagentTask(
        tool_call_id="parent-task-call", user_id=thread.user_id,
        thread_id=thread.id, run_id=run.id, description="读取网页", prompt="读取官方来源",
        status="completed", result="结论与来源",
        messages=[
            Message(role="user", content="读取官方来源"),
            Message(role="assistant", content="", reasoning_content="先查阅正文",
                    tool_calls=[ToolCall(id="child-read", name="web_fetch",
                                         arguments={"url": "https://example.org/docs"})]),
            Message(role="tool", content="资料正文", tool_call_id="child-read"),
            Message(role="assistant", content="结论与来源"),
        ],
    )
    state.subtasks[child.task_id] = child
    state.messages.append(Message(role="user", content="继续查阅"))
    state.compression = CompressionState(snipped_ids=[state.messages[0].id])
    child.compression = CompressionState(growth_tokens=123, last_api_at=456.0)
    repository.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=2, state=state))

    latest_path = f"/api/threads/{thread.id}/state"
    history_path = f"/api/threads/{thread.id}/runs/{run.id}/checkpoints"
    latest = client.get(latest_path, params={"user_id": "alice"})
    history = client.get(history_path, params={"user_id": "alice"})

    assert latest.status_code == history.status_code == 200
    saved_child = latest.json()["state"]["subtasks"][child.task_id]
    expected_child = child.to_dict()
    expected_child.pop("compression")  # 内部上下文元数据不改变成功 HTTP schema。
    assert saved_child == expected_child
    assert "compression" not in latest.json()["state"]
    assert saved_child["tool_call_id"] == "parent-task-call"
    assert saved_child["messages"][2]["tool_call_id"] == "child-read"
    assert latest.json()["state"]["messages"] == [message.to_dict() for message in state.messages]
    assert history.json()[0]["state"]["subtasks"] == {}
    assert history.json()[1]["state"]["subtasks"][child.task_id] == saved_child
    assert client.get(latest_path, params={"user_id": "bob"}).status_code == 404
    assert client.get(history_path, params={"user_id": "bob"}).status_code == 404


def test_legacy_checkpoint_queries_include_an_empty_subtasks_field(storage_and_client):
    thread, run, client = storage_and_client
    state = ThreadState(thread_id=thread.id, user_id=thread.user_id,
                        workspace_path=thread.workspace_path,
                        messages=[Message(role="user", content="旧消息")])
    checkpoint = CheckpointRepository().save(Checkpoint(
        thread_id=thread.id, run_id=run.id, step=1, state=state,
    ))
    old_payload = state.to_dict()
    old_payload.pop("subtasks")
    with database.connect() as connection:
        connection.execute("UPDATE checkpoints SET state_json = ? WHERE id = ?",
                           (json.dumps(old_payload), checkpoint.id))

    response = client.get(f"/api/threads/{thread.id}/state", params={"user_id": "alice"})
    assert response.status_code == 200
    assert response.json()["state"]["subtasks"] == {}
    assert response.json()["state"]["messages"][0]["content"] == "旧消息"
