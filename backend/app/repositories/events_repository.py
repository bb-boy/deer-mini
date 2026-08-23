

"""
先检查这个user是否有这个thread_id,同时检查下有没有这个run，
然后拿到数据库中的最大的sequence
然后插入这个事件
最后MAX(sequence)  +1   给这个事件分配一个sequence

"""




import json

from app.domain.events import RunEvent, RunEventType
from app.infrastructure.database import connect




class EventRepository:


    def append(self, event: RunEvent,user_id: str) -> RunEvent:

        """
        将一个RunEvent对象存储到sqlite数据库中
        :param event: RunEvent对象
        :return: None
        """
        with connect() as conn:
            # 检查是否存在对应的thread_id和run_id

            conn.execute("BEGIN IMMEDIATE")
            run_row = conn.execute(
                """
                SELECT r.id 
                FROM runs r
                JOIN threads t ON r.thread_id = t.id
                WHERE r.id = ? AND t.id = ? AND t.user_id = ?
                """,
                (event.run_id, event.thread_id, user_id),
            ).fetchone()
            if run_row is None:
                raise ValueError("Thread or Run does not exist for the given thread_id and run_id")

            # 获取当前最大的sequence值
            row = conn.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) 
                AS max_sequence
                FROM run_events 
                WHERE run_id = ?
                """,
                (event.run_id,),
            ).fetchone()
            next_sequence = row["max_sequence"] +1

            # 插入新的事件，sequence为max_sequence + 1
            conn.execute(
                """
                INSERT INTO run_events (id, run_id, thread_id, event_type, payload_json, sequence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.run_id,
                    event.thread_id,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False),
                    next_sequence,
                    event.created_at,
                ),
            )

            event.sequence = next_sequence #更新python内存中的对象，此时数据库中的对象已经更新了
            return event


    def list_for_run(
        self,
         thread_id: str,
            run_id: str,
         user_id: str, ) -> list[RunEvent]:

        """
        从数据库中读取某个run的所有事件，并按sequence升序排列
        :param thread_id: 线程ID
        :param run_id: 运行ID
        :param user_id: 用户ID
        :return: RunEvent对象列表

        """
         
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT e.id, e.run_id, e.thread_id, e.sequence,
                    e.event_type, e.payload_json, e.created_at
                FROM run_events e
                JOIN threads t ON e.thread_id = t.id
                WHERE e.thread_id = ?
                AND e.run_id = ?
                AND t.user_id = ?
                ORDER BY e.sequence ASC
                """,
                (thread_id, run_id, user_id),
            ).fetchall()

        return [self._row_to_event(row) for row in rows]


    def _row_to_event(self, row) -> RunEvent:
        """
        把 SQLite 的一行事件记录恢复为 RunEvent 对象。
        """
        return RunEvent(
            id=row["id"],
            run_id=row["run_id"],
            thread_id=row["thread_id"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            sequence=row["sequence"],
            created_at=row["created_at"],
        )