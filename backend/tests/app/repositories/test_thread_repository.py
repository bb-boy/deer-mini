"""Tests for backend/app/repositories/thread_repository.py."""

# 在这里编写 pytest 的 test_* 函数。


from app.repositories.thread_repository import ThreadRepository
from app.domain.threads import Thread
from app.repositories.thread_repository import ThreadRepository


from app.domain.common import new_id

def test_create_and_get_thread():
    

    # 创建一个 ThreadRepository 实例
    repo = ThreadRepository()

    # 创建一个新的 Thread 对象
    thread_id = new_id()
    user_id = "bob"
    thread = Thread(
        id=thread_id,
        user_id=user_id,
        workspace_path="/path/to/workspace",
        title="Test Thread",
        status="idle"
    )
    thread_id2 = new_id()
    user_id2 = "alice"
    thread2 = Thread(
        id=thread_id2,
        user_id=user_id2,
        workspace_path="/path/to/workspace",
        title="Test Thread",
        status="idle"
    )

    # 将 Thread 对象存储到数据库中
    repo.create(thread)
    repo.create(thread2)

    # 从数据库中获取 Thread 对象
    retrieved_thread = repo.get(thread_id, user_id)
    retrieved_thread2 = repo.get(thread_id2, user_id2)

    #用户隔离
    retrieved_thread_none = repo.get(thread_id, user_id2)

    # 验证获取的对象是否与原始对象相同
    assert retrieved_thread is not None
    assert retrieved_thread.id == thread.id
    assert retrieved_thread.user_id == thread.user_id
    assert retrieved_thread.workspace_path == thread.workspace_path
    assert retrieved_thread.title == thread.title
    assert retrieved_thread.status == thread.status

    assert retrieved_thread2 is not None
    assert retrieved_thread2.id == thread2.id
    assert retrieved_thread2.user_id == thread2.user_id
    assert retrieved_thread2.workspace_path == thread2.workspace_path
    assert retrieved_thread2.title == thread2.title
    assert retrieved_thread2.status == thread2.status

    assert retrieved_thread_none is None