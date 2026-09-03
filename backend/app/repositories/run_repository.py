"""
创建并读取 Run,Run是一个对话的一次运行，包含了用户的输入和模型的输出，以及运行状态等信息。

class Run:
    id: str
    thread_id: str
    user_id: str
    status: RunStatus = "pending"
    model_name: str | None = None
    thinking_enabled: bool = True
    reasoning_effort: str | None = None
    error:str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


实现create()方法，往数据库中插入一个run
get()方法，获取一个run对象,从数据库中，把数据库中的run row转化为一个run类对象

"""



import sqlite3
from app.domain.common import utc_now
from app.domain.runs import Run
from app.infrastructure.database import connect 




class RunRepository:
    
    def create(self, run: Run) -> None:
        """
        将一个Run对象存储到sqlite数据库中
        :param run: Run对象
        :return: None
        """
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, thread_id, user_id, status, model_name, thinking_enabled, reasoning_effort, error, started_at, finished_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) 
                
                """,
                (
                    run.id,
                    run.thread_id,
                    run.user_id,
                    run.status,
                    run.model_name,
                    int(run.thinking_enabled),
                    run.reasoning_effort,
                    run.error,
                    run.started_at,
                    run.finished_at,
                    run.created_at,
                    run.updated_at,
                ),
            )

    def get(self, run_id: str, user_id: str) -> Run | None:
        """
        从sqlite数据库中获取一个Run对象
        :param run_id: Run对象的id
        :param user_id: Run对象的user_id
        :return: Run对象或None
        """
        with connect() as conn:
            row = conn.execute(
                """
                SELECT id, thread_id, user_id, status, model_name, thinking_enabled, reasoning_effort, error, started_at, finished_at, created_at, updated_at
                FROM runs
                WHERE id = ? AND user_id = ?
                """,
                (run_id, user_id),
            ).fetchone()
        
        if row is None:
            return None
        return self._row_to_run(row)

    def list_for_thread(
        self,
        thread_id: str,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        """读取指定用户在一个 Thread 中最近创建的 Run。"""
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, thread_id, user_id, status, model_name,
                       thinking_enabled, reasoning_effort, error,
                       started_at, finished_at, created_at, updated_at
                FROM runs
                WHERE thread_id = ? AND user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (thread_id, user_id, limit, offset),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def list_inflight(self) -> list[Run]:
        """
        读取所有仍标记为 pending/running 的 Run。

        单进程版在应用启动时调用它；此时内存里还没有任何新任务，
        因而这些记录都是上一次进程未能完成的孤儿 Run。
        """
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, thread_id, user_id, status, model_name,
                       thinking_enabled, reasoning_effort, error,
                       started_at, finished_at, created_at, updated_at
                FROM runs
                WHERE status IN ('pending', 'running')
                ORDER BY created_at ASC
                """
            ).fetchall()

        return [self._row_to_run(row) for row in rows]
        

    def _row_to_run(self, row: sqlite3.Row) -> Run:
        """
        将sqlite3.Row对象转换为Run对象
        :param row: sqlite3.Row对象
        :return: Run对象
        """
        return Run(
            id=row["id"],
            thread_id=row["thread_id"],
            user_id=row["user_id"],
            status=row["status"],
            model_name=row["model_name"],
            thinking_enabled=bool(row["thinking_enabled"]),
            reasoning_effort=row["reasoning_effort"],
            error=row["error"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    


    ##run的状态从pending到running
    def start(self, run_id: str, user_id: str) -> bool:
        """
        将Run对象的状态从pending更新为running
        :param run_id: Run对象的id
        :param user_id: Run对象的user_id
        :return: None
        """

        now = utc_now()
        with connect() as conn:
            
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = 'running', started_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'pending'
                """,
                (now, now, run_id, user_id),
            )

        return cursor.rowcount == 1  # 如果更新了1行，说明状态更新成功，否则说明状态更新失败



    #run的状态转变为sucess error timeout
    def finish(self,run_id:str,user_id:str,status:str,error:str | None = None) ->bool:
        """
        将Run对象的状态从running更新为success、error或timeout
        :param run_id: Run对象的id
        :param user_id: Run对象的user_id
        :param status: 要设置的状态
        :param error: 错误信息
        :return: None
        """
        now = utc_now()
        teminat_status = {"success","error","timeout"}

        if status not in teminat_status:
            raise ValueError(f"Invalid status: {status}. Must be one of {teminat_status}")
        with connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = ?,error = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'running'
                """,
                (status, error, now, now, run_id, user_id),
            )

        return cursor.rowcount == 1  # 如果更新了1行，说明状态更新成功，否则说明状态更新失败

    def interrupt(
        self,
        run_id: str,
        user_id: str,
        error: str | None = None,
    ) -> bool:
        """
        将尚未结束的 Run 更新为 interrupted。

        同时接受 pending 和 running，是为了处理用户在后台任务真正启动前
        就点击“停止”的竞态情况。
        """
        now = utc_now()
        with connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = 'interrupted',
                    error = ?,
                    finished_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status IN ('pending', 'running')
                """,
                (error, now, now, run_id, user_id),
            )

        return cursor.rowcount == 1

    def recover_orphan(
        self,
        run_id: str,
        user_id: str,
        error: str,
    ) -> bool:
        """
        将失去执行进程的 pending/running Run 原子更新为 error。

        WHERE 中再次检查状态，保证重复启动恢复不会重复修改终态 Run。
        """
        now = utc_now()
        with connect() as conn:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = 'error',
                    error = ?,
                    finished_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status IN ('pending', 'running')
                """,
                (error, now, now, run_id, user_id),
            )

        return cursor.rowcount == 1

