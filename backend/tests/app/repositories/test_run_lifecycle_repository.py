"""Run 与 Thread 生命周期原子更新的集成测试。"""

import sqlite3

import pytest

from app.domain.threads import Thread
from app.infrastructure import database
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository
from app.services.run_service import RunService


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """每个测试使用独立 SQLite，绝不修改开发数据库。"""
    database_path = tmp_path / "deer_mini_test.db"
    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    database.initialize_database()


def create_run(*, user_id: str = "atomic-user"):
    """创建一组真实 Thread/Run 记录，供生命周期测试使用。"""
    thread = Thread(
        id=f"thread-{user_id}",
        user_id=user_id,
        workspace_path=f"/tmp/{user_id}",
        title="Atomic lifecycle test",
    )
    ThreadRepository().create(thread)
    run = RunService().create_run(user_id, thread.id, "test-model")
    return thread, run


def read_statuses(thread_id: str, run_id: str, user_id: str):
    run = RunRepository().get(run_id, user_id)
    thread = ThreadRepository().get(thread_id, user_id)
    assert run is not None
    assert thread is not None
    return run, thread


def reject_thread_status(target_status: str) -> None:
    """模拟事务第二步写 Thread 时数据库失败。"""
    with database.connect() as connection:
        connection.execute(
            f"""
            CREATE TRIGGER reject_thread_{target_status}
            BEFORE UPDATE OF status ON threads
            WHEN NEW.status = '{target_status}'
            BEGIN
                SELECT RAISE(ABORT, 'forced thread update failure');
            END;
            """
        )


def test_start_rolls_back_run_when_thread_update_fails():
    thread, run = create_run(user_id="start-rollback")
    reject_thread_status("running")

    with pytest.raises(sqlite3.IntegrityError, match="forced thread update failure"):
        RunService().start_run(run.id, run.user_id)

    stored_run, stored_thread = read_statuses(thread.id, run.id, run.user_id)
    assert stored_run.status == "pending"
    assert stored_run.started_at is None
    assert stored_thread.status == "idle"


def test_finish_rolls_back_run_when_thread_update_fails():
    thread, run = create_run(user_id="finish-rollback")
    service = RunService()
    assert service.start_run(run.id, run.user_id) is True
    reject_thread_status("idle")

    with pytest.raises(sqlite3.IntegrityError, match="forced thread update failure"):
        service.finish_run(run.id, run.user_id, "success")

    stored_run, stored_thread = read_statuses(thread.id, run.id, run.user_id)
    assert stored_run.status == "running"
    assert stored_run.finished_at is None
    assert stored_thread.status == "running"


@pytest.mark.parametrize("source_status", ["pending", "running"])
def test_interrupt_updates_run_and_thread_together(source_status):
    thread, run = create_run(user_id=f"interrupt-{source_status}")
    service = RunService()
    if source_status == "running":
        assert service.start_run(run.id, run.user_id) is True

    assert service.interrupt_run(run.id, run.user_id, "cancelled") is True

    stored_run, stored_thread = read_statuses(thread.id, run.id, run.user_id)
    assert stored_run.status == "interrupted"
    assert stored_run.error == "cancelled"
    assert stored_run.finished_at is not None
    assert stored_thread.status == "idle"


@pytest.mark.parametrize("source_status", ["pending", "running"])
def test_orphan_recovery_updates_run_and_thread_together(source_status):
    thread, run = create_run(user_id=f"recovery-{source_status}")
    service = RunService()
    if source_status == "running":
        assert service.start_run(run.id, run.user_id) is True

    assert service.recover_orphaned_run(run.id, run.user_id, "worker exited") is True

    stored_run, stored_thread = read_statuses(thread.id, run.id, run.user_id)
    assert stored_run.status == "error"
    assert stored_run.error == "worker exited"
    assert stored_run.finished_at is not None
    assert stored_thread.status == "idle"
