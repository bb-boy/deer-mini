"""Thread、Run 和 Checkpoint 查询接口测试。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import router
from app.domain.checkpoints import Checkpoint
from app.domain.messages import Message, ToolCall
from app.domain.runs import Run
from app.domain.threads import Thread, ThreadState
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """使用隔离 SQLite；不加载 main.lifespan，避免执行孤儿恢复。"""
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "query_api.db")
    database.initialize_database()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


def save_thread(
    thread_id: str,
    user_id: str,
    *,
    created_at: str,
    updated_at: str,
) -> Thread:
    thread = Thread(
        id=thread_id,
        user_id=user_id,
        workspace_path=f"/tmp/{user_id}/{thread_id}/workspace",
        title=thread_id,
        created_at=created_at,
        updated_at=updated_at,
    )
    ThreadRepository().create(thread)
    return thread


def save_run(
    run_id: str,
    thread: Thread,
    *,
    created_at: str,
    status: str = "success",
) -> Run:
    run = Run(
        id=run_id,
        thread_id=thread.id,
        user_id=thread.user_id,
        status=status,
        model_name="test-model",
        created_at=created_at,
        updated_at=created_at,
    )
    RunRepository().create(run)
    return run


def test_list_threads_is_user_scoped_sorted_and_paginated(client):
    save_thread(
        "thread-old",
        "alice",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    save_thread(
        "thread-new",
        "alice",
        created_at="2026-01-02T00:00:00+00:00",
        updated_at="2026-01-03T00:00:00+00:00",
    )
    save_thread(
        "thread-bob",
        "bob",
        created_at="2026-01-04T00:00:00+00:00",
        updated_at="2026-01-04T00:00:00+00:00",
    )

    first_page = client.get(
        "/api/threads",
        params={"user_id": "alice", "limit": 1, "offset": 0},
    )
    second_page = client.get(
        "/api/threads",
        params={"user_id": "alice", "limit": 1, "offset": 1},
    )

    assert first_page.status_code == 200
    assert [item["id"] for item in first_page.json()] == ["thread-new"]
    assert [item["id"] for item in second_page.json()] == ["thread-old"]


def test_get_thread_checks_user_ownership(client):
    save_thread(
        "owned-thread",
        "alice",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    response = client.get(
        "/api/threads/owned-thread",
        params={"user_id": "alice"},
    )
    forbidden = client.get(
        "/api/threads/owned-thread",
        params={"user_id": "bob"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "owned-thread"
    assert forbidden.status_code == 404


def test_list_runs_is_thread_scoped_and_newest_first(client):
    thread = save_thread(
        "run-thread",
        "alice",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    save_run(
        "run-old",
        thread,
        created_at="2026-01-01T01:00:00+00:00",
    )
    save_run(
        "run-new",
        thread,
        created_at="2026-01-01T02:00:00+00:00",
        status="error",
    )

    response = client.get(
        "/api/threads/run-thread/runs",
        params={"user_id": "alice"},
    )
    forbidden = client.get(
        "/api/threads/run-thread/runs",
        params={"user_id": "bob"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["run-new", "run-old"]
    assert forbidden.status_code == 404


def test_latest_state_and_checkpoint_history_return_complete_messages(client):
    thread = save_thread(
        "state-thread",
        "alice",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    run = save_run(
        "state-run",
        thread,
        created_at="2026-01-01T01:00:00+00:00",
    )
    first_state = ThreadState(
        thread_id=thread.id,
        user_id=thread.user_id,
        workspace_path=thread.workspace_path,
        messages=[Message(role="user", content="读取 report.txt")],
    )
    final_state = ThreadState.from_dict(first_state.to_dict())
    final_state.messages.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="read_file",
                    arguments={"path": "report.txt"},
                )
            ],
        )
    )
    CheckpointRepository().save(
        Checkpoint(thread_id=thread.id, run_id=run.id, step=1, state=first_state)
    )
    CheckpointRepository().save(
        Checkpoint(thread_id=thread.id, run_id=run.id, step=2, state=final_state)
    )

    latest = client.get(
        "/api/threads/state-thread/state",
        params={"user_id": "alice"},
    )
    history = client.get(
        "/api/threads/state-thread/runs/state-run/checkpoints",
        params={"user_id": "alice"},
    )

    assert latest.status_code == 200
    assert latest.json()["step"] == 2
    assert latest.json()["state"]["messages"][1]["tool_calls"][0] == {
        "id": "call-1",
        "name": "read_file",
        "arguments": {"path": "report.txt"},
    }
    assert history.status_code == 200
    assert [item["step"] for item in history.json()] == [1, 2]


def test_latest_state_returns_404_before_first_checkpoint(client):
    save_thread(
        "empty-thread",
        "alice",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )

    response = client.get(
        "/api/threads/empty-thread/state",
        params={"user_id": "alice"},
    )

    assert response.status_code == 404
