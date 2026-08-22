"""Tests for backend/app/services/thread_service.py."""

# 在这里编写 pytest 的 test_* 函数。

from app.services.thread_service import ThreadService



from app.domain.threads import Thread
from app.domain.common import new_id



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