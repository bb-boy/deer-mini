"""SQLite 记忆任务账本：原子登记、按项结果、用户范围管理及历史清理。

文件提交不在本事务内。running/verifying 项的复制内容只有核实后才清除。
登记和 Thread 删除通过同一 SQLite 写事务串行，防止删除后晚到登记。
"""

import json
import sqlite3
from contextlib import contextmanager, AbstractContextManager
from collections.abc import Callable, Sequence, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from app.infrastructure.database import connect

if TYPE_CHECKING:
    from app.memory.operations import PreparedMemoryOperation

RESOLVED = frozenset({"success", "cancelled"})
ACTIVE = frozenset({"pending", "running", "waiting", "verifying"})
SAFE_ERROR_KEYS = frozenset({"backend", "operation", "category", "stage", "commit_state", "recovery"})


def _safe_error(error: dict[str, str] | None) -> str | None:
    return json.dumps({k: v for k, v in (error or {}).items() if k in SAFE_ERROR_KEYS}) if error else None


def _summarize(conn: sqlite3.Connection, task_id: str, now: str) -> None:
    statuses = [row[0] for row in conn.execute(
        "SELECT status FROM memory_save_items WHERE task_id=?", (task_id,))]
    if all(s == "success" for s in statuses):
        status = "success"
    elif all(s == "cancelled" for s in statuses):
        status = "cancelled"
    elif "success" in statuses:
        status = "partial"
    elif any(s in ACTIVE for s in statuses):
        status = "pending"
    elif "conflict" in statuses:
        status = "conflict"
    else:
        status = "failed"
    closed = now if all(s in RESOLVED for s in statuses) else None
    conn.execute("UPDATE memory_save_tasks SET status=?, closed_at=COALESCE(closed_at,?) WHERE id=?",
                 (status, closed, task_id))


def cancel_source_tasks(conn: sqlite3.Connection, user_id: str, thread_id: str, now: str) -> None:
    """在删除 Thread 的当前事务中取消未来保存；在途项保留给提交方核实。"""
    tasks = conn.execute("SELECT id FROM memory_save_tasks WHERE user_id=? AND source_thread_id=? AND closed_at IS NULL",
                         (user_id, thread_id)).fetchall()
    for task in tasks:
        _cancel(conn, task[0], now)


def _cancel(conn: sqlite3.Connection, task_id: str, now: str) -> None:
    conn.execute("UPDATE memory_save_tasks SET cancel_requested=1 WHERE id=?", (task_id,))
    conn.execute("""UPDATE memory_save_items SET status='cancelled', payload_json=NULL,
                 next_attempt_at=NULL WHERE task_id=? AND status NOT IN ('success','running','verifying')""",
                 (task_id,))
    conn.execute("UPDATE memory_save_items SET next_attempt_at=? WHERE task_id=? AND status='verifying'",
                 (now, task_id))
    _summarize(conn, task_id, now)


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    """SQLite 的事务上下文本身不关闭连接；账本每次操作显式释放句柄。"""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class MemoryTaskRepository:
    """输入已验证操作；只有事务退出确认提交后才返回可恢复任务 ID。"""

    def __init__(self, *, clock: Callable[[], datetime] | None = None,
                 window_seconds: float = 86400, connection_factory: Callable[[], AbstractContextManager[sqlite3.Connection]] = _connection) -> None:
        import math
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("恢复窗口必须是有限正数")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.window_seconds = window_seconds
        self._connect = connection_factory

    def now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("任务时钟必须有时区")
        return now.astimezone(timezone.utc)

    def register(self, user_id: str, thread_id: str, run_id: str,
                 operations: Sequence['PreparedMemoryOperation']) -> str:
        if not operations or len(operations) > 10:
            raise ValueError("保存任务必须包含1到10个已验证操作")
        task_id = uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM threads WHERE id=? AND user_id=?", (thread_id, user_id)).fetchone() is None:
                raise ValueError("来源对话不存在或不属于用户")
            now = self.now()
            conn.execute("""INSERT INTO memory_save_tasks
                (id,user_id,source_thread_id,source_run_id,registered_at,deadline_at)
                VALUES (?,?,?,?,?,?)""", (task_id, user_id, thread_id, run_id, now.isoformat(),
                                          (now + timedelta(seconds=self.window_seconds)).isoformat()))
            for position, operation in enumerate(operations):
                conn.execute("""INSERT INTO memory_save_items
                    (operation_id,task_id,position,memory_id,expected_version,result_digest,payload_json,next_attempt_at)
                    VALUES (?,?,?,?,?,?,?,?)""", (operation.operation_id, task_id, position, operation.memory_id,
                    operation.expected_version, operation.result_digest,
                    json.dumps(operation.to_dict(), ensure_ascii=False), now.isoformat()))
        return task_id

    def list_tasks(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("查询数量必须为1到1000")
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM memory_save_tasks WHERE user_id=? ORDER BY registered_at DESC,id LIMIT ?", (user_id, limit))]

    def get(self, user_id: str, task_id: str, *, include_content: bool = False) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memory_save_tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone()
            if row is None:
                return None
            result = dict(row)
            items = []
            for row in conn.execute("SELECT * FROM memory_save_items WHERE task_id=? ORDER BY position", (task_id,)):
                item = dict(row)
                raw = item.pop("payload_json")
                if include_content:
                    item["payload"] = json.loads(raw) if raw else None
                item["error"] = json.loads(item.pop("error_json") or "null")
                items.append(item)
            result["items"] = items
            result["history"] = []
            for row in conn.execute("SELECT * FROM memory_save_attempts WHERE task_id=? ORDER BY id", (task_id,)):
                attempt = dict(row)
                attempt["error"] = json.loads(attempt.pop("error_json") or "null")
                result["history"].append(attempt)
            return result

    @staticmethod
    def _owned(conn: sqlite3.Connection, user_id: str, task_id: str) -> sqlite3.Row:
        task = conn.execute("SELECT * FROM memory_save_tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone()
        if task is None:
            raise ValueError("任务不存在或不属于用户")
        return task

    def recover_inflight(self) -> None:
        """仅启动时调用：上次在途项必须先核实，不能推断正文尚未写入。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""UPDATE memory_save_attempts SET status='unconfirmed',phase='verify'
                WHERE status='running' AND operation_id IN
                (SELECT operation_id FROM memory_save_items WHERE status='running')""")
            conn.execute("UPDATE memory_save_items SET status='verifying',next_attempt_at=? WHERE status='running'", (self.now().isoformat(),))

    def due(self, *, limit: int = 100) -> list[dict[str, Any]]:
        now = self.now().isoformat()
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("""SELECT i.operation_id,i.task_id,t.user_id
                FROM memory_save_items i JOIN memory_save_tasks t ON t.id=i.task_id
                WHERE i.status IN ('pending','waiting','verifying') AND i.next_attempt_at IS NOT NULL
                AND (i.next_attempt_at<=? OR t.deadline_at<=?)
                ORDER BY i.next_attempt_at,t.registered_at,i.position LIMIT ?""", (now, now, limit))]

    def claim(self, user_id: str, task_id: str, operation_id: str) -> dict[str, Any] | None:
        """把一个到期项标为在途；过期或取消只允许核实既有结果。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._owned(conn, user_id, task_id)
            row = conn.execute("SELECT * FROM memory_save_items WHERE task_id=? AND operation_id=?", (task_id, operation_id)).fetchone()
            if row is None or row["status"] not in {"pending", "waiting", "verifying"}:
                return None
            now = self.now()
            expired = now >= datetime.fromisoformat(task["deadline_at"])
            verify = row["status"] == "verifying"
            if (task["cancel_requested"] or expired) and not verify:
                status = "cancelled" if task["cancel_requested"] else "failed"
                self._finish(conn, task, operation_id, status, "window_closed", None, None, now.isoformat())
                return None
            if row["next_attempt_at"] is None or (row["next_attempt_at"] > now.isoformat() and not expired):
                return None
            conn.execute("UPDATE memory_save_items SET status='running',attempts=attempts+1 WHERE operation_id=?", (operation_id,))
            conn.execute("""INSERT INTO memory_save_attempts
                (task_id,operation_id,attempted_at,window,status,phase) VALUES (?,?,?,?,?,?)""",
                (task_id, operation_id, now.isoformat(), task["window"], "running", "verify" if verify else "save"))
            result = dict(row)
            result.update(user_id=user_id, deadline_at=task["deadline_at"], window=task["window"],
                          cancel_requested=bool(task["cancel_requested"]), verify_first=verify,
                          allow_write=not expired and not task["cancel_requested"], attempts=row["attempts"] + 1)
            result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def finish(self, user_id: str, task_id: str, operation_id: str, *, status: str,
               phase: str, error: dict[str, str] | None = None,
               next_attempt_at: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._owned(conn, user_id, task_id)
            if task["cancel_requested"] and status not in {"success", "verifying"}:
                status, next_attempt_at = "cancelled", None
            self._finish(conn, task, operation_id, status, phase, error,
                         next_attempt_at.isoformat() if next_attempt_at else None, self.now().isoformat())

    @staticmethod
    def _finish(conn: sqlite3.Connection, task: sqlite3.Row, operation_id: str, status: str,
                phase: str, error: dict[str, str] | None, next_at: str | None, now: str) -> None:
        if status not in {*ACTIVE, "success", "failed", "conflict", "cancelled"}:
            raise ValueError("无效保存项状态")
        changed = conn.execute("""UPDATE memory_save_items SET status=?,phase=?,error_json=?,next_attempt_at=?,
            payload_json=CASE WHEN ? IN ('success','cancelled') THEN NULL ELSE payload_json END
            WHERE task_id=? AND operation_id=?""", (status, phase, _safe_error(error), next_at, status, task["id"], operation_id))
        if changed.rowcount != 1:
            raise ValueError("保存项不存在")
        completed = conn.execute("""UPDATE memory_save_attempts SET status=?,phase=?,error_json=?
            WHERE id=(SELECT MAX(id) FROM memory_save_attempts WHERE task_id=? AND operation_id=?
                      AND window=? AND status='running')""",
            (status, phase, _safe_error(error), task["id"], operation_id, task["window"]))
        if completed.rowcount == 0:
            conn.execute("""INSERT INTO memory_save_attempts
                (task_id,operation_id,attempted_at,window,status,phase,error_json) VALUES (?,?,?,?,?,?,?)""",
                (task["id"], operation_id, now, task["window"], status, phase, _safe_error(error)))
        _summarize(conn, task["id"], now)

    def cancel(self, user_id: str, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._owned(conn, user_id, task_id)
            _cancel(conn, task_id, self.now().isoformat())

    def retry(self, user_id: str, task_id: str) -> None:
        """仅重新打开普通失败的保存项；冲突、非法访问和来源已删除不可重试。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._owned(conn, user_id, task_id)
            if task["cancel_requested"] or conn.execute("SELECT 1 FROM threads WHERE id=? AND user_id=?",
                (task["source_thread_id"], user_id)).fetchone() is None:
                raise ValueError("已取消或来源已删除的任务不能重试")
            rows = conn.execute("SELECT * FROM memory_save_items WHERE task_id=?", (task_id,)).fetchall()
            if any(row["status"] in {"running", "pending", "waiting"} for row in rows):
                raise ValueError("任务仍有进行中的保存项")
            candidates = [row for row in rows if row["status"] in {"failed", "verifying"}
                          and (json.loads(row["error_json"] or "{}").get("recovery") != "manual")]
            if not candidates:
                raise ValueError("没有可重试的普通失败项")
            now = self.now()
            conn.execute("UPDATE memory_save_tasks SET deadline_at=?,window=window+1,closed_at=NULL,status='pending' WHERE id=?",
                         ((now + timedelta(seconds=self.window_seconds)).isoformat(), task_id))
            for row in candidates:
                conn.execute("UPDATE memory_save_items SET status=?,next_attempt_at=?,attempts=0 WHERE operation_id=?",
                             ("verifying" if row["status"] == "verifying" else "pending", now.isoformat(), row["operation_id"]))

    def resolve_conflict(self, user_id: str, task_id: str, operation_id: str) -> None:
        """保留当前记忆，只取消指定旧冲突项；不提供覆盖写入。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._owned(conn, user_id, task_id)
            row = conn.execute("SELECT status FROM memory_save_items WHERE task_id=? AND operation_id=?", (task_id, operation_id)).fetchone()
            if row is None or row[0] != "conflict":
                raise ValueError("指定保存项不是冲突状态")
            self._finish(conn, task, operation_id, "cancelled", "keep_current", None, None, self.now().isoformat())

    def cleanup(self, user_id: str) -> int:
        """仅删除已解决且关闭30天的任务历史，不读取或删除任何记忆文件。"""
        threshold = (self.now() - timedelta(days=30)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute("""DELETE FROM memory_save_tasks WHERE user_id=? AND closed_at<=?
                AND NOT EXISTS (SELECT 1 FROM memory_save_items i WHERE i.task_id=memory_save_tasks.id
                AND i.status NOT IN ('success','cancelled'))""", (user_id, threshold))
            return cursor.rowcount

    def cleanup_expired_history(self) -> int:
        """后台维护：清理已解决的到期历史，保留任何未解决项及真实文件。"""
        threshold = (self.now() - timedelta(days=30)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute("""DELETE FROM memory_save_tasks WHERE closed_at<=?
                AND NOT EXISTS (SELECT 1 FROM memory_save_items i WHERE i.task_id=memory_save_tasks.id
                AND i.status NOT IN ('success','cancelled'))""", (threshold,))
            return cursor.rowcount
