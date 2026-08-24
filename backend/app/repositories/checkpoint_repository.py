"""
save(checkpoint)
→ 把 Agent 当前完整状态存为一份快照。

latest(thread_id, user_id)
→ 只读取该用户该对话最新的一份快照。

保存和恢复过程：
ThreadState
→ to_dict()
→ json.dumps()
→ checkpoints.state_json

state_json
→ json.loads()
→ ThreadState.from_dict()
→ 恢复 ThreadState


DeerFlow 中由 CheckpointStateAccessor 读取状态，再交给 LangGraph 的 SQLite 保存器持久化；

"""


import json
import  sqlite3
from app.domain.checkpoints import Checkpoint
from app.domain.threads import ThreadState
from app.infrastructure.database import connect


class CheckpointRepository:
    def save(self, checkpoint:Checkpoint) -> Checkpoint:
        """
        将一个Checkpoint对象存储到sqlite数据库中
        :param checkpoint: Checkpoint对象
        :return: Checkpoint对象
        """

        if checkpoint.thread_id != checkpoint.state.thread_id:
            raise ValueError("Checkpoint thread_id does not match state thread_id")
        #快照的 thread_id 必须和 ThreadState 的 thread_id 一致，否则抛出 ValueError 异常
        

        #先把checkpoint转化为json
        state_json = json.dumps(checkpoint.state.to_dict(),
                                ensure_ascii=False)  # 将 ThreadState 对象转换为字典，再转换为 JSON 字符串
        #把checkpoint存入数据库
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO checkpoints (thread_id, run_id, step, state_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.thread_id,
                    checkpoint.run_id,
                    checkpoint.step,
                    state_json,
                    checkpoint.created_at,
                ),
            )
        checkpoint.id = cursor.lastrowid # 获取插入的行的 ID，并将其赋值给 checkpoint.id
        return checkpoint  
    

    def latest(self, thread_id: str, user_id: str) -> Checkpoint | None:
        """
        从sqlite数据库中获取一个Thread对象的最新Checkpoint对象
        :param thread_id: Thread对象的id
        :param user_id: Thread对象的user_id
        :return: Checkpoint对象或None
        """
        with connect() as conn:
            row = conn.execute(
                """
                SELECT c.id, c.thread_id, c.run_id, c.step, c.state_json, c.created_at
                FROM checkpoints c
                JOIN threads t ON 
                    c.thread_id = t.id
                WHERE 
                    c.thread_id = ?
                    AND t.user_id = ?
                ORDER BY 
                    c.created_at DESC,
                    c.id DESC

                LIMIT 1
                """,
                (thread_id, user_id),
            ).fetchone()

            if row is None:
                return None

            return self._row_to_checkpoint(row)


    def history(self, thread_id: str, user_id: str,run_id: str) -> list[Checkpoint]:

        """
        cong sqlite 数据库中获取一个 Thread 对象的一次run的所有 Checkpoint 对象  
        """

        with connect() as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.thread_id, c.run_id, c.step, c.state_json, c.created_at
                FROM checkpoints c
                JOIN threads t ON 
                    c.thread_id = t.id
                WHERE 
                    c.thread_id = ?
                    AND t.user_id = ?
                    AND c.run_id = ?
                ORDER BY 
                    c.step ASC,
                    c.created_at ASC,
                    c.id ASC
                """,
                (thread_id, user_id, run_id),
            ).fetchall()

            return [self._row_to_checkpoint(row) for row in rows]
        


    def _row_to_checkpoint(self, row: sqlite3.Row) -> Checkpoint:
        """
        将sqlite3.Row对象转换为Checkpoint对象
        :param row: sqlite3.Row对象
        :return: Checkpoint对象
        """
        state_dict = json.loads(row["state_json"])  # 将 JSON 字符串转换为字典，我们取出来的是一行，行中有这样一个字段
        state = ThreadState.from_dict(state_dict)  # 将字典转换为 ThreadState 对象

        return Checkpoint(
            id=row["id"],
            thread_id=row["thread_id"],
            run_id=row["run_id"],
            step=row["step"],
            state=state,
            created_at=row["created_at"],
        )