"""持久化完整状态，分离 Thread 当前指针与每个 Run 的 execution 历史。"""

import json
import sqlite3

from app.domain.checkpoints import Checkpoint
from app.domain.threads import ThreadState
from app.infrastructure.database import connect


class CheckpointRepository:
    def save(self, checkpoint: Checkpoint, *, advance_head: bool = True) -> Checkpoint:
        """在一个事务内保存状态并推进当前指针；后台历史写入可禁用推进。"""
        with connect() as conn:
            self.save_in(conn, checkpoint, advance_head=advance_head)
        return checkpoint

    def save_in(self, conn: sqlite3.Connection, checkpoint: Checkpoint, *,
                advance_head: bool = True) -> Checkpoint:
        """供恢复事务组合使用；调用方负责提交，校验状态、Thread、Run 的共同归属。"""
        if checkpoint.thread_id != checkpoint.state.thread_id:
            raise ValueError('Checkpoint thread_id does not match state thread_id')
        owned = conn.execute(
            'SELECT 1 FROM threads t JOIN runs r ON r.thread_id=t.id '
            'WHERE t.id=? AND t.user_id=? AND r.id=? AND r.user_id=t.user_id',
            (checkpoint.thread_id, checkpoint.state.user_id, checkpoint.run_id),
        ).fetchone()
        if owned is None:
            raise ValueError('Checkpoint 不属于当前 Thread、Run 和用户')
        if checkpoint.kind not in {'execution', 'turn_start', 'restore'}:
            raise ValueError('不支持的 Checkpoint 类型')
        cursor = conn.execute(
            'INSERT INTO checkpoints(thread_id,run_id,step,state_json,created_at,kind) '
            'VALUES(?,?,?,?,?,?)',
            (checkpoint.thread_id, checkpoint.run_id, checkpoint.step,
             json.dumps(checkpoint.state.to_dict(), ensure_ascii=False),
             checkpoint.created_at, checkpoint.kind),
        )
        checkpoint.id = cursor.lastrowid
        if advance_head:
            conn.execute(
                'INSERT INTO thread_heads(thread_id,checkpoint_id,revision) VALUES(?,?,1) '
                'ON CONFLICT(thread_id) DO UPDATE SET checkpoint_id=excluded.checkpoint_id, '
                'revision=thread_heads.revision+1', (checkpoint.thread_id, checkpoint.id),
            )
        return checkpoint

    def latest(self, thread_id: str, user_id: str) -> Checkpoint | None:
        """读取当前指针；回滚后绝不按历史时间重新选择状态。"""
        with connect() as conn:
            row = conn.execute(
                'SELECT c.* FROM thread_heads h JOIN checkpoints c ON c.id=h.checkpoint_id '
                'JOIN threads t ON t.id=h.thread_id WHERE t.id=? AND t.user_id=?',
                (thread_id, user_id),
            ).fetchone()
        return None if row is None else self._row_to_checkpoint(row)

    def get(self, checkpoint_id: int, thread_id: str, user_id: str) -> Checkpoint | None:
        with connect() as conn:
            row = conn.execute(
                'SELECT c.* FROM checkpoints c JOIN threads t ON t.id=c.thread_id '
                'WHERE c.id=? AND c.thread_id=? AND t.user_id=?',
                (checkpoint_id, thread_id, user_id),
            ).fetchone()
        return None if row is None else self._row_to_checkpoint(row)

    def revision(self, thread_id: str, user_id: str) -> int:
        with connect() as conn:
            row = conn.execute(
                'SELECT h.revision FROM thread_heads h JOIN threads t ON t.id=h.thread_id '
                'WHERE t.id=? AND t.user_id=?', (thread_id, user_id),
            ).fetchone()
        return row['revision'] if row else 0

    def next_step(self, thread_id: str, run_id: str, *, initial: int = 1) -> int:
        with connect() as conn:
            row = conn.execute('SELECT MAX(step) AS step FROM checkpoints WHERE thread_id=? AND run_id=?',
                               (thread_id, run_id)).fetchone()
        return (row['step'] + 1) if row['step'] is not None else initial

    def history(self, thread_id: str, user_id: str, run_id: str) -> list[Checkpoint]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT c.* FROM checkpoints c JOIN threads t ON c.thread_id=t.id "
                "WHERE c.thread_id=? AND t.user_id=? AND c.run_id=? AND c.kind='execution' "
                'ORDER BY c.step,c.id', (thread_id, user_id, run_id),
            ).fetchall()
        return [self._row_to_checkpoint(row) for row in rows]

    def latest_for_run(self, thread_id: str, run_id: str, user_id: str) -> Checkpoint | None:
        """旧 Run 的 SSE 恢复只使用该 Run 的执行快照。"""
        with connect() as conn:
            row = conn.execute(
                "SELECT c.* FROM checkpoints c JOIN threads t ON t.id=c.thread_id "
                "WHERE c.thread_id=? AND c.run_id=? AND t.user_id=? AND c.kind='execution' "
                'ORDER BY c.step DESC,c.id DESC LIMIT 1', (thread_id, run_id, user_id),
            ).fetchone()
        return None if row is None else self._row_to_checkpoint(row)

    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
        return Checkpoint(id=row['id'], thread_id=row['thread_id'], run_id=row['run_id'],
                          step=row['step'], state=ThreadState.from_dict(json.loads(row['state_json'])),
                          created_at=row['created_at'], kind=row['kind'])
