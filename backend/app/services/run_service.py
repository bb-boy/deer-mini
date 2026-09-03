"""处理一个具体 Run 的创建、查询和生命周期。"""

from app.domain.common import new_id
from app.domain.runs import Run
from app.repositories.run_lifecycle_repository import RunLifecycleRepository
from app.repositories.run_repository import RunRepository
import sqlite3

from app.repositories.thread_repository import ThreadRepository


class RunService:
    def __init__(
        self,
        runrepo: RunRepository | None = None,
        threadrepo: ThreadRepository | None = None,
        lifecycle_repo: RunLifecycleRepository | None = None,
    ) -> None:
        self._runrepo = runrepo if runrepo is not None else RunRepository()  #实例化一个RunRepository对象
        self._threadrepo = threadrepo if threadrepo is not None else ThreadRepository()  #实例化一个ThreadRepository对象
        self._lifecycle_repo = (
            lifecycle_repo
            if lifecycle_repo is not None
            else RunLifecycleRepository()
        )

    def get_run(self, run_id: str, user_id: str) -> Run | None:
        """读取属于指定用户的一次 Run。"""
        return self._runrepo.get(run_id, user_id)

    def list_inflight_runs(self) -> list[Run]:
        """读取应用启动前遗留的 pending/running Run。"""
        return self._runrepo.list_inflight()

    def create_run(self, 
                   user_id: str,
                   
                   thread_id: str,
                   model_name: str,
                   thinking_enabled: bool =True,
                   reasoning_effort: str | None = None) -> Run:
        """
        创建一个新的run，并将其存储到数据库中
        :param thread: Thread对象
        :param run: Run对象
        :return: 创建的Run对象
        """
        thread = self._threadrepo.get(thread_id, user_id)  #从数据库中获取一个thread对象
        if thread is None:
            raise ValueError(f"Thread with id {thread_id} and user_id {user_id} does not exist.")
        run = Run(
            id=new_id(),
            user_id=user_id,
            thread_id=thread_id,
            model_name=model_name,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort
        )
        

        try:
            self._runrepo.create(run)  #将run对象存储到数据库中
        except sqlite3.IntegrityError as error: #完整性错误，可能是因为违反了唯一约束或外键约束

            raise ValueError(f"Failed to create run: {error}") from error #
            #把原来的异常包装在一个新的ValueError中，并提供一个更有意义的错误消息。这样做的好处是，调用者可以捕获这个ValueError并处理它，而不需要关心底层的sqlite3异常类型，同时继续传播
        return run



    #start run的同时更新thread的状态从 idle变为running

    def start_run(self,run_id:str,user_id:str) -> bool:
        """
        启动一个run并更新其关联thread的状态
        :param run_id: Run对象的id
        :param user_id: Run对象的user_id
        :return: True表示成功，False表示失败
        """
    
        run = self._runrepo.get(run_id,user_id)
        if run is None:
            raise ValueError(f"Run with id {run_id} and user_id {user_id} does not exist.")
    
        return self._lifecycle_repo.start(run_id, user_id)


    def finish_run(self, run_id: str, user_id: str, status: str, error: str | None = None) -> bool:
        """
        完成一个run并更新其关联thread的状态
        :param run_id: Run对象的id
        :param user_id: Run对象的user_id
        :param status: 要设置的状态
        :param error: 错误信息
        :return: True表示成功，False表示失败
        """
        run = self._runrepo.get(run_id, user_id)
        if run is None:
            raise ValueError(f"Run with id {run_id} and user_id {user_id} does not exist.")

        return self._lifecycle_repo.finish(run_id, user_id, status, error)

    def interrupt_run(
        self,
        run_id: str,
        user_id: str,
        reason: str = "cancelled_by_user",
    ) -> bool:
        """
        停止一个 pending/running Run，并让所属 Thread 恢复为 idle。

        返回 False 表示 Run 已经结束，调用者不需要再次修改它。
        """
        run = self._runrepo.get(run_id, user_id)
        if run is None:
            raise ValueError(
                f"Run with id {run_id} and user_id {user_id} does not exist."
            )

        return self._lifecycle_repo.interrupt(run_id, user_id, reason)

    def recover_orphaned_run(
        self,
        run_id: str,
        user_id: str,
        error: str,
    ) -> bool:
        """
        将孤儿 Run 标记为 error，并让所属 Thread 恢复 idle。

        返回 False 说明它已被其他收尾流程改成终态。
        """
        run = self._runrepo.get(run_id, user_id)
        if run is None:
            return False

        return self._lifecycle_repo.recover_orphan(
            run_id,
            user_id,
            error,
        )
