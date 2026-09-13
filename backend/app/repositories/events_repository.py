"""按 Run 顺序保存辅助日志；一次事务可以提交多条事件。"""

import json
from contextlib import closing
from dataclasses import replace

from app.domain.events import RunEvent
from app.infrastructure.database import connect


class EventRepository:
    def append(self, event: RunEvent, user_id: str) -> RunEvent:
        """保留单条保存入口；只有提交成功才给原对象分配 sequence。"""
        saved = self.append_batch([event], user_id)[0]
        event.sequence = saved.sequence
        return event

    def append_batch(self, events: list[RunEvent], user_id: str) -> list[RunEvent]:
        """保存同一次 Run 的一批日志，返回带数据库顺序号的副本。

        events 是按发生顺序排列的事件，user_id 用于检查所属用户。
        整批提交或回滚；重试必须使用原来的事件 id，避免重复插入。
        此方法是同步数据库操作，由 EventRecorder 的工作线程调用。
        """
        if not events:
            return []
        run_id, thread_id = events[0].run_id, events[0].thread_id
        if any((event.run_id, event.thread_id) != (run_id, thread_id) for event in events):
            raise ValueError("一次日志批次必须属于同一个 Thread 和 Run")

        saved: list[RunEvent] = []
        with closing(connect()) as conn, conn:
            # 辅助日志遇到锁竞争时尽快交还，稍后重试，不长时间抢占写库。
            conn.execute("PRAGMA busy_timeout = 250")
            conn.execute("BEGIN IMMEDIATE")
            owned = conn.execute(
                """SELECT r.id FROM runs r JOIN threads t ON r.thread_id = t.id
                   WHERE r.id = ? AND t.id = ? AND t.user_id = ? AND r.user_id = ?""",
                (run_id, thread_id, user_id, user_id),
            ).fetchone()
            if owned is None:
                raise ValueError("Thread or Run does not exist for the given user")

            next_sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            for event in events:
                payload_json = json.dumps(event.payload, ensure_ascii=False)
                existing = conn.execute(
                    "SELECT * FROM run_events WHERE id = ?", (event.id,),
                ).fetchone()
                if existing is not None:
                    # 提交成功但调用方没收到结果时，重试相同 UUID 仍只保存一次。
                    if (
                        existing["run_id"] != run_id
                        or existing["thread_id"] != thread_id
                        or existing["event_type"] != event.event_type
                        or json.loads(existing["payload_json"]) != event.payload
                        or existing["created_at"] != event.created_at
                    ):
                        raise ValueError("事件 id 已被其他内容使用")
                    saved.append(replace(event, sequence=existing["sequence"]))
                    continue

                conn.execute(
                    """INSERT INTO run_events
                       (id, run_id, thread_id, event_type, payload_json, sequence, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (event.id, run_id, thread_id, event.event_type, payload_json,
                     next_sequence, event.created_at),
                )
                saved.append(replace(event, sequence=next_sequence))
                next_sequence += 1
        # 离开事务上下文后才返回，提交失败不会把 sequence 泄漏给调用方。
        return saved

    def list_for_run(self, thread_id: str, run_id: str, user_id: str) -> list[RunEvent]:
        """读取这个用户的一次 Run 实际保存成功的日志。"""
        with closing(connect()) as conn:
            rows = conn.execute(
                """SELECT e.* FROM run_events e
                   JOIN threads t ON e.thread_id = t.id
                   WHERE e.thread_id = ? AND e.run_id = ? AND t.user_id = ?
                   ORDER BY e.sequence ASC""",
                (thread_id, run_id, user_id),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_event(row) -> RunEvent:
        return RunEvent(
            id=row["id"], run_id=row["run_id"], thread_id=row["thread_id"],
            event_type=row["event_type"], payload=json.loads(row["payload_json"]),
            sequence=row["sequence"], created_at=row["created_at"],
        )
