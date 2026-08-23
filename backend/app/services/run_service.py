""""
处理一个具体的run的建立和持久化


"""




from pdb import run

from app.domain.common import new_id
from app.domain.runs import Run
from app.repositories.run_repository import RunRepository


from app.domain.threads import Thread
import sqlite3

from app.repositories.thread_repository import ThreadRepository


class RunService:
    def __init__(self, runrepo: RunRepository | None = None,threadrepo:ThreadRepository | None = None) -> None:
        self._runrepo = runrepo if runrepo is not None else RunRepository()  #实例化一个RunRepository对象
        self._threadrepo = threadrepo if threadrepo is not None else ThreadRepository()  #实例化一个ThreadRepository对象

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
    
        thread_id = run.thread_id
        #更新run的状态
        run_started = self._runrepo.start(run_id,user_id)
        if not run_started:
            return False
        thread_started = self._threadrepo.update_status(thread_id,user_id,"running")
        if not thread_started:
            return False
        return True


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

        thread_id = run.thread_id
        #更新run的状态
        run_finished = self._runrepo.finish(run_id, user_id, status, error)
        if not run_finished:
            return False
        thread_finished = self._threadrepo.update_status(thread_id, user_id, "idle")
        if not thread_finished:
            return False
        return True

    