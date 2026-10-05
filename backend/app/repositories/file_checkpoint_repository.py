"""文件恢复点与操作记录；将消息指针、清单引用和提交状态放在同一事务。"""
import json
import sqlite3
from copy import deepcopy
from app.domain.checkpoints import Checkpoint
from app.domain.common import new_id, utc_now
from app.domain.threads import Thread
from app.infrastructure.database import connect
from app.repositories.checkpoint_repository import CheckpointRepository


class FileCheckpointRepository:
    def threads(self) -> list[Thread]:
        """启动恢复内部使用的 Thread 路径索引，不作为对外查询。"""
        with connect() as conn:
            return [Thread(**dict(row)) for row in conn.execute('SELECT id,user_id,workspace_path FROM threads')]

    def points(self, thread: Thread) -> list[dict]:
        with connect() as conn:
            rows = conn.execute(
                'SELECT p.*,s.manifest_json FROM file_restore_points p '
                'JOIN file_snapshots s ON s.id=p.file_snapshot_id '
                'JOIN threads t ON t.id=p.thread_id WHERE t.id=? AND t.user_id=? '
                'ORDER BY p.created_at DESC,p.rowid DESC', (thread.id,thread.user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def point(self, thread: Thread, point_id: str) -> dict:
        for point in self.points(thread):
            if point['id'] == point_id:
                return point
        raise LookupError('恢复点不存在或不属于当前对话')

    def add_point(self, thread: Thread, checkpoint: Checkpoint, manifest: dict,
                  kind: str, message: str, evict: list[str], *, advance_head: bool) -> dict:
        point_id, snapshot_id, now = new_id(), new_id(), utc_now()
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            CheckpointRepository().save_in(conn,checkpoint,advance_head=advance_head)
            conn.execute('INSERT INTO file_snapshots VALUES(?,?,?,?,?)',
                (snapshot_id,thread.id,json.dumps(manifest,ensure_ascii=False,separators=(',',':'),sort_keys=True),
                 sum(e.get('size',0) for e in manifest['entries']),now))
            conn.execute('INSERT INTO file_restore_points VALUES(?,?,?,?,?,?,?,?)',
                (point_id,thread.id,checkpoint.run_id if kind=='turn_start' else None,
                 checkpoint.id,snapshot_id,kind,message,now))
            for old_id in evict:
                old = conn.execute('SELECT file_snapshot_id FROM file_restore_points WHERE id=? AND thread_id=?',
                                   (old_id,thread.id)).fetchone()
                if old is not None:
                    conn.execute('DELETE FROM file_restore_points WHERE id=?',(old_id,))
                    conn.execute('DELETE FROM file_snapshots WHERE id=?',(old['file_snapshot_id'],))
        return self.point(thread,point_id)

    def operations(self, thread: Thread | None = None, *, pending_only: bool = False) -> list[dict]:
        query = ('SELECT o.*,t.user_id,t.workspace_path FROM file_restore_operations o '
                 'JOIN threads t ON t.id=o.thread_id WHERE 1=1')
        args: list = []
        if thread is not None:
            query += ' AND t.id=? AND t.user_id=?'
            args.extend([thread.id,thread.user_id])
        if pending_only:
            query += ' AND o.cleaned=0'
        query += ' ORDER BY o.created_at DESC,o.rowid DESC'
        with connect() as conn:
            return [dict(row) for row in conn.execute(query,args)]

    def operation(self, thread: Thread, operation_id: str) -> dict | None:
        with connect() as conn:
            row = conn.execute(
                'SELECT o.* FROM file_restore_operations o JOIN threads t ON t.id=o.thread_id '
                'WHERE o.operation_id=? AND t.id=? AND t.user_id=?',
                (operation_id,thread.id,thread.user_id),
            ).fetchone()
        return dict(row) if row else None

    def prepare(self, thread: Thread, operation_id: str, target: str, recovery: str,
                revision: int, fingerprint: str) -> None:
        # 目标和备份的归属都必须一致，操作编号全局唯一，不能覆盖其他用户记录。
        self.point(thread,target)
        self.point(thread,recovery)
        now = utc_now()
        try:
            with connect() as conn:
                conn.execute('INSERT INTO file_restore_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (operation_id,thread.id,target,recovery,revision,fingerprint,'prepared',None,0,now,now))
        except sqlite3.IntegrityError as error:
            raise ValueError('恢复操作编号已经使用') from error

    def update(self, thread: Thread, operation_id: str, status: str, *,
               error: str | None = None, cleaned: bool = False) -> None:
        with connect() as conn:
            cursor = conn.execute(
                'UPDATE file_restore_operations SET status=?,error=?,cleaned=?,updated_at=? '
                'WHERE operation_id=? AND thread_id=? '
                'AND EXISTS(SELECT 1 FROM threads WHERE id=? AND user_id=?)',
                (status,error,int(cleaned),utc_now(),operation_id,thread.id,thread.id,thread.user_id),
            )
            if cursor.rowcount != 1:
                raise LookupError('恢复操作不存在')

    def commit_restore(self, thread: Thread, operation_id: str, target: dict, revision: int) -> None:
        repo = CheckpointRepository()
        state = repo.get(target['state_checkpoint_id'],thread.id,thread.user_id)
        if state is None:
            raise LookupError('目标消息状态不存在')
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            head = conn.execute('SELECT revision FROM thread_heads WHERE thread_id=?',(thread.id,)).fetchone()
            if (head['revision'] if head else 0) != revision:
                raise RuntimeError('消息状态已变化，请重新预览')
            step = conn.execute('SELECT COALESCE(MAX(step),0)+1 FROM checkpoints WHERE thread_id=? AND run_id=?',
                                (thread.id,state.run_id)).fetchone()[0]
            repo.save_in(conn,Checkpoint(thread.id,state.run_id,step,deepcopy(state.state),kind='restore'))
            cursor = conn.execute(
                "UPDATE file_restore_operations SET status='committed',updated_at=? "
                "WHERE operation_id=? AND thread_id=? AND status='applying'",
                (utc_now(),operation_id,thread.id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError('恢复操作状态无法提交')
