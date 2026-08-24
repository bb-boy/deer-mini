"""Tests for CheckpointRepository."""

from pathlib import Path

import pytest

from app.domain.checkpoints import Checkpoint
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.services import thread_service
from app.services.run_service import RunService
from app.services.thread_service import ThreadService


@pytest.fixture
def isolated_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """让每个测试使用独立的 SQLite 文件和工作目录。"""
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "deer_mini.db")
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()


def create_thread_and_run(user_id: str):
    """创建一组满足外键约束的真实 Thread 和 Run。"""
    thread = ThreadService().create_thread(user_id, "checkpoint test")
    run = RunService().create_run(user_id, thread.id, "test-model")
    return thread, run


def test_save_and_read_latest_checkpoint(isolated_storage) -> None:
    user_id = "alice"
    thread, run = create_thread_and_run(user_id)
    state = ThreadState(
        thread_id=thread.id,
        user_id=user_id,
        workspace_path=thread.workspace_path,
        messages=[Message(role="user", content="读取 report.txt")],
    )
    checkpoint = Checkpoint(
        thread_id=thread.id,
        run_id=run.id,
        step=1,
        state=state,
    )

    saved = CheckpointRepository().save(checkpoint)
    restored = CheckpointRepository().latest(thread.id, user_id)

    assert isinstance(saved.id, int)
    assert restored is not None
    assert restored.id == saved.id
    assert restored.run_id == run.id
    assert restored.step == 1
    assert restored.state.messages[0].content == "读取 report.txt"


def test_history_returns_one_run_in_step_order(isolated_storage) -> None:
    user_id = "alice"
    thread, run = create_thread_and_run(user_id)
    repository = CheckpointRepository()

    for step in range(1, 4):
        state = ThreadState(
            thread_id=thread.id,
            user_id=user_id,
            workspace_path=thread.workspace_path,
            messages=[Message(role="user", content=f"message {step}")],
        )
        repository.save(
            Checkpoint(
                thread_id=thread.id,
                run_id=run.id,
                step=step,
                state=state,
            )
        )

    history = repository.history(thread.id, user_id, run.id)

    assert [checkpoint.step for checkpoint in history] == [1, 2, 3]
    assert [checkpoint.state.messages[0].content for checkpoint in history] == [
        "message 1",
        "message 2",
        "message 3",
    ]


def test_checkpoint_reads_do_not_leak_to_another_user(isolated_storage) -> None:
    owner_id = "alice"
    thread, run = create_thread_and_run(owner_id)
    checkpoint = Checkpoint(
        thread_id=thread.id,
        run_id=run.id,
        step=1,
        state=ThreadState(
            thread_id=thread.id,
            user_id=owner_id,
            workspace_path=thread.workspace_path,
            messages=[Message(role="user", content="private message")],
        ),
    )
    repository = CheckpointRepository()
    repository.save(checkpoint)

    assert repository.latest(thread.id, "bob") is None
    assert repository.history(thread.id, "bob", run.id) == []
