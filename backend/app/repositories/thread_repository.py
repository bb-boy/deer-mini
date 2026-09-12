"""

由 ThreadRepository来读写数据库
create(thread)把一个python的thread对象存储到sqlite数据库中，使用sqlite3模块来操作sqlite数据库
get(thread_id,user_id)从sqlite数据库中获取一个thread对象
"""




import sqlite3

from app.domain.common import utc_now
from app.domain.threads import Thread
from app.infrastructure.database import connect



class ThreadRepository:


    def create(self, thread: Thread) -> None:

        """
        将一个Thread对象存储到sqlite数据库中
        :param thread: Thread对象
        :return: None
        """
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO threads (id, user_id, workspace_path, title, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread.id,
                    thread.user_id,
                    thread.workspace_path,
                    thread.title,
                    thread.status,
                    thread.created_at,
                    thread.updated_at,
                ),
            ) 



    def get(self, thread_id: str, user_id: str) -> Thread | None:
            
        """
        从sqlite数据库中获取一个Thread对象
        :param thread_id: Thread对象的id
        :param user_id: Thread对象的user_id
        :return: Thread对象或None
        """
        with connect() as conn:
            cursor = conn.execute(
                """
                SELECT id, user_id, workspace_path, title, status, created_at, updated_at
                FROM threads
                WHERE id = ? AND user_id = ?
                """,
                (thread_id, user_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_thread(row)

    def list_for_user(
        self,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Thread]:
        """读取一个用户最近更新的 Thread，供浏览器恢复对话列表。"""
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, workspace_path, title, status,
                       created_at, updated_at
                FROM threads
                WHERE user_id = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            ).fetchall()
        return [self._row_to_thread(row) for row in rows]

    def _row_to_thread(self, row: sqlite3.Row) -> Thread:
            """
            将sqlite3.Row对象转换为Thread对象
            :param row: sqlite3.Row对象
            :return: Thread对象
            """
            return Thread(
                id=row["id"],
                user_id=row["user_id"],
                workspace_path=row["workspace_path"],
                title=row["title"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
    

    #更新thread的状态
    def update_status(self, thread_id: str, user_id: str, new_status: str) -> bool:
        """
        更新Thread对象的状态
        :param thread_id: Thread对象的id
        :param user_id: Thread对象的user_id
        :param new_status: 新的状态
        :return: None
        """

        allow_status = {"idle", "running"}
        if new_status not in allow_status:
            raise ValueError(f"Invalid status: {new_status}. Must be one of {allow_status}")

        now = utc_now()
        with connect() as conn:
            cursor = conn.execute(
                """
                UPDATE threads
                SET status = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (new_status, now, thread_id, user_id),
            )
        return cursor.rowcount == 1  # 如果更新了至少一行，返回True，否则返回False

    def update_title(self, thread_id: str, user_id: str, title: str) -> Thread | None:
        """更新 Thread 标题并返回最新记录。"""
        with connect() as conn:
            cursor = conn.execute(
                """
                UPDATE threads
                SET title = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (title, utc_now(), thread_id, user_id),
            )
        if cursor.rowcount != 1:
            return None
        return self.get(thread_id, user_id)

    def delete(self, thread_id: str, user_id: str) -> Thread | None:
        """删除属于当前用户的 Thread，并依赖外键级联清理运行记录。"""
        thread = self.get(thread_id, user_id)
        if thread is None:
            return None
        with connect() as conn:
            conn.execute(
                "DELETE FROM threads WHERE id = ? AND user_id = ?",
                (thread_id, user_id),
            )
        return thread
