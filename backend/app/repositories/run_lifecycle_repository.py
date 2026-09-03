"""在一个 SQLite 事务中同步修改 Run 与 Thread 的生命周期状态。"""

import sqlite3

from app.domain.common import utc_now
from app.infrastructure.database import connect


class RunLifecycleRepository:
    """
    负责必须一起成功或一起失败的 Run/Thread 状态转换。

    普通的 Run 创建和读取仍由 RunRepository 负责；这个类只处理跨表事务，
    避免上层 Service 分两次提交数据库后留下互相矛盾的状态。
    """

    def start(self, run_id: str, user_id: str) -> bool:
        """将 pending Run 与所属 Thread 一起改为 running。"""
        now = utc_now()
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            thread_id = self._find_thread_id(connection, run_id, user_id)
            if thread_id is None:
                return False

            cursor = connection.execute(
                """
                UPDATE runs
                SET status = 'running', started_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'pending'
                """,
                (now, now, run_id, user_id),
            )
            if cursor.rowcount != 1:
                return False

            self._update_thread_status(
                connection,
                thread_id=thread_id,
                user_id=user_id,
                status="running",
                updated_at=now,
            )
            return True

    def finish(
        self,
        run_id: str,
        user_id: str,
        status: str,
        error: str | None = None,
    ) -> bool:
        """将 running Run 改为终态，并让所属 Thread 同时恢复 idle。"""
        terminal_statuses = {"success", "error", "timeout"}
        if status not in terminal_statuses:
            raise ValueError(
                f"Invalid status: {status}. Must be one of {terminal_statuses}"
            )

        now = utc_now()
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            thread_id = self._find_thread_id(connection, run_id, user_id)
            if thread_id is None:
                return False

            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, error = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'running'
                """,
                (status, error, now, now, run_id, user_id),
            )
            if cursor.rowcount != 1:
                return False

            self._update_thread_status(
                connection,
                thread_id=thread_id,
                user_id=user_id,
                status="idle",
                updated_at=now,
            )
            return True

    def interrupt(
        self,
        run_id: str,
        user_id: str,
        error: str | None = None,
    ) -> bool:
        """中断 pending/running Run，并让所属 Thread 同时恢复 idle。"""
        now = utc_now()
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            thread_id = self._find_thread_id(connection, run_id, user_id)
            if thread_id is None:
                return False

            cursor = connection.execute(
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
            if cursor.rowcount != 1:
                return False

            self._update_thread_status(
                connection,
                thread_id=thread_id,
                user_id=user_id,
                status="idle",
                updated_at=now,
            )
            return True

    def recover_orphan(
        self,
        run_id: str,
        user_id: str,
        error: str,
    ) -> bool:
        """把孤儿 pending/running Run 与所属 Thread 原子恢复为 error/idle。"""
        now = utc_now()
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            thread_id = self._find_thread_id(connection, run_id, user_id)
            if thread_id is None:
                return False

            cursor = connection.execute(
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
            if cursor.rowcount != 1:
                return False

            self._update_thread_status(
                connection,
                thread_id=thread_id,
                user_id=user_id,
                status="idle",
                updated_at=now,
            )
            return True

    @staticmethod
    def _find_thread_id(
        connection: sqlite3.Connection,
        run_id: str,
        user_id: str,
    ) -> str | None:
        """在当前事务中找到 Run 所属 Thread，不另开数据库连接。"""
        row = connection.execute(
            """
            SELECT thread_id
            FROM runs
            WHERE id = ? AND user_id = ?
            """,
            (run_id, user_id),
        ).fetchone()
        return None if row is None else str(row["thread_id"])

    @staticmethod
    def _update_thread_status(
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        user_id: str,
        status: str,
        updated_at: str,
    ) -> None:
        """使用当前事务更新 Thread；异常或缺失都会使整个事务回滚。"""
        cursor = connection.execute(
            """
            UPDATE threads
            SET status = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (status, updated_at, thread_id, user_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Run 对应的 Thread 不存在: thread_id={thread_id}, user_id={user_id}"
            )
