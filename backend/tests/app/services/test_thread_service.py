"""Tests for backend/app/services/thread_service.py."""

import asyncio

# 在这里编写 pytest 的 test_* 函数。

from app.services.thread_service import (
    ThreadService,
    cleanup_pending_thread_deletions,
)



from app.domain.threads import Thread
from app.domain.common import new_id


class FakeThreadRepository:
    def __init__(self, thread: Thread, delete_error: Exception | None = None):
        self.thread = thread
        self.delete_error = delete_error

    def get(self, thread_id: str, user_id: str) -> Thread | None:
        if thread_id == self.thread.id and user_id == self.thread.user_id:
            return self.thread
        return None

    def delete(self, thread_id: str, user_id: str) -> Thread | None:
        if self.delete_error is not None:
            raise self.delete_error
        return self.get(thread_id, user_id)


class MissingThreadRepository:
    def get(self, thread_id: str, user_id: str) -> None:
        return None


def make_thread(tmp_path, monkeypatch) -> tuple[Thread, FakeThreadRepository]:
    data_root = tmp_path / "users"
    workspace = data_root / "alice" / "threads" / "thread-1" / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(data_root))
    thread = Thread(
        id="thread-1",
        user_id="alice",
        workspace_path=str(workspace),
        title="待删除对话",
    )
    return thread, FakeThreadRepository(thread)



def test_create_thread_service():

    """
    测试 ThreadService 的 create_thread 方法
    """
    # 创建一个 ThreadService 实例
    service = ThreadService()

    # 创建一个新的线程
    user_id = "bob"
    title = "Test Thread"
    
    thread = service.create_thread(user_id, title)

    # 验证返回的对象是否为 Thread 类型
    assert isinstance(thread, Thread)
    assert thread.user_id == user_id
    assert thread.status == "idle"

def test_create_thread_service_invalid_user_id():


    """
    测试 ThreadService 的 create_thread 方法对于非法 user_id 的处理"""
    service = ThreadService()

    # 测试非法的 user_id
    invalid_user_ids = ["", ".", "..", "user/../id", "user\\id"]
    for user_id in invalid_user_ids:
        try:
            service.create_thread(user_id, "Test Thread")
            assert False, f"Expected ValueError for user_id: {user_id}"
        except ValueError as e:
            assert str(e) == f"user_id 不能包含路径分隔符或 '..'"




def test_sqlite_integration():
    """
    测试 ThreadService 与 ThreadRepository 的集成
    """
    service = ThreadService()

    user_id = "alice"
    title = "Integration Test Thread"

    # 创建线程
    thread = service.create_thread(user_id, title)

    # 从数据库中获取线程
    retrieved_thread = service._threadrepo.get(thread.id, user_id)

    # 验证获取的对象是否与原始对象相同
    assert retrieved_thread is not None
    assert retrieved_thread.id == thread.id
    assert retrieved_thread.user_id == thread.user_id
    assert retrieved_thread.title == thread.title


def test_delete_thread_removes_record_and_workspace(tmp_path, monkeypatch):
    thread, repository = make_thread(tmp_path, monkeypatch)
    service = ThreadService(repository)

    deleted = asyncio.run(service.delete_thread(thread.id, thread.user_id))

    assert deleted == thread
    assert not (tmp_path / "users" / "alice" / "threads" / "thread-1").exists()


def test_delete_thread_restores_workspace_when_database_delete_fails(
    tmp_path, monkeypatch
):
    thread, _ = make_thread(tmp_path, monkeypatch)
    repository = FakeThreadRepository(thread, RuntimeError("database unavailable"))
    service = ThreadService(repository)

    try:
        asyncio.run(service.delete_thread(thread.id, thread.user_id))
        assert False, "Expected repository failure"
    except RuntimeError as error:
        assert str(error) == "database unavailable"

    assert (tmp_path / "users" / "alice" / "threads" / "thread-1").exists()
    assert list((tmp_path / "users" / "alice" / "threads").glob(".deleting-*")) == []


def test_delete_thread_keeps_cleanup_marker_when_rmtree_fails(
    tmp_path, monkeypatch
):
    thread, repository = make_thread(tmp_path, monkeypatch)
    service = ThreadService(repository)

    def fail_cleanup(path):
        raise OSError("filesystem busy")

    monkeypatch.setattr("app.services.thread_service.shutil.rmtree", fail_cleanup)

    deleted = asyncio.run(service.delete_thread(thread.id, thread.user_id))

    assert deleted == thread
    assert not (tmp_path / "users" / "alice" / "threads" / "thread-1").exists()
    assert len(list((tmp_path / "users" / "alice" / "threads").glob(".deleting-*"))) == 1


def test_startup_restores_marker_when_thread_still_exists(tmp_path, monkeypatch):
    thread, repository = make_thread(tmp_path, monkeypatch)
    thread_dir = tmp_path / "users" / "alice" / "threads" / "thread-1"
    marker = thread_dir.parent / ".deleting-thread-1.operation-1"
    thread_dir.replace(marker)

    asyncio.run(cleanup_pending_thread_deletions(repository))

    assert thread_dir.exists()
    assert not marker.exists()


def test_startup_removes_marker_when_thread_was_deleted(tmp_path, monkeypatch):
    thread, _ = make_thread(tmp_path, monkeypatch)
    thread_dir = tmp_path / "users" / "alice" / "threads" / thread.id
    marker = thread_dir.parent / f".deleting-{thread.id}.operation-1"
    thread_dir.replace(marker)

    asyncio.run(cleanup_pending_thread_deletions(MissingThreadRepository()))

    assert not marker.exists()
