"""Tests for backend/app/services/run_service.py."""

# 在这里编写 pytest 的 test_* 函数。







from app.domain.runs import Run
from app.domain.threads import Thread
from app.repositories.thread_repository import ThreadRepository
from app.services.run_service import RunService




from app.domain.common import new_id




def create_test_thread(user_id: str) -> Thread:
    thread = Thread(
        id=new_id(),
        user_id=user_id,
        workspace_path="/path/to/workspace",
        title="Test Thread",
    )

    ThreadRepository().create(thread)
    return thread



def test_create_run_service():
    """
    测试 RunService 的 create_run 方法
    """
    thread = create_test_thread("a")
    
    # 创建一个 RunService 实例
    service = RunService()

    # 创建一个新的 run
    user_id = "a"
    thread_id = thread.id  
    model_name = "test_model"
    
    


    run = service.create_run(user_id, thread_id, model_name)

    # 验证返回的对象是否为 Run 类型
    assert isinstance(run, Run)
    assert run.user_id == user_id
    assert run.thread_id == thread_id
    assert run.model_name == model_name

def test_create_run_service_invalid_thread():
    """
    测试 RunService 的 create_run 方法对于不存在的 thread_id 的处理
    """
    service = RunService()

    # 测试不存在的 thread_id
    invalid_thread_id = "non_existent_thread_id"
    user_id = "bob"
    model_name = "test_model"

    try:
        service.create_run(user_id, invalid_thread_id, model_name)
        assert False, f"Expected ValueError for thread_id: {invalid_thread_id}"
    except ValueError as e:
        assert str(e) == f"Thread with id {invalid_thread_id} and user_id {user_id} does not exist."



def test_create_run_service_sqlite_integration():
    """
    测试 RunService 与 RunRepository 的集成
    """
    service = RunService()
    thread = create_test_thread("b")

    user_id = "b"
    thread_id = thread.id
    model_name = "Integration Test Model"

    # 创建 run
    run = service.create_run(user_id, thread_id, model_name)

    # 验证返回的对象是否为 Run 类型
    assert isinstance(run, Run)
    assert run.user_id == user_id
    assert run.thread_id == thread_id
    assert run.model_name == model_name



def test_only_one_active_run_per_thread():

    """
    测试同一个 thread_id 只能有一个 active run (pending 或 running)
    """
    service = RunService()
    thread = create_test_thread("c")

    user_id = "c"
    thread_id = thread.id
    model_name = "Test Model"

    # 创建第一个 run
    run1 = service.create_run(user_id, thread_id, model_name)

    # 尝试创建第二个 active run
    try:
        run2 = service.create_run(user_id, thread_id, model_name)
        assert False, "Expected ValueError for creating a second active run for the same thread"
    except ValueError as e:
        assert "Failed to create run" in str(e)


def test_run_service_start_and_finish():
    """
    测试 RunService 的 start_run 和 finish_run 方法
    """
    service = RunService()
    thread = create_test_thread("d")

    user_id = "d"
    thread_id = thread.id
    model_name = "Test Model"

    # 创建 run
    run = service.create_run(user_id, thread_id, model_name)

    # 启动 run
    started = service.start_run(run.id, user_id)
    assert started is True
    assert service._runrepo.get(run.id, user_id).status == "running"    

    #thread的状态应该从 idle变为running
    thread_after_start = service._threadrepo.get(thread.id, user_id)
    assert thread_after_start.status == "running"

    #不能从running变为pending
    try:
        service.finish_run(run.id, user_id, "pending")
        assert False, "Expected ValueError for invalid status transition"
    except ValueError as e:
        assert "Invalid status" in str(e)


    # 完成 run
    finished = service.finish_run(run.id, user_id, "success")
    assert finished is True
    assert service._runrepo.get(run.id, user_id).status == "success"

    #thred状态应该从 running变为idle
    thread_after_finish = service._threadrepo.get(thread.id, user_id)   
    assert thread_after_finish.status == "idle"