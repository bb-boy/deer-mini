"""

一次 Run 创建一个 EventRecorder

EventRecorder 记住：
用户是谁
属于哪个 Thread
是哪一次 Run

Runtime 只需要说：
“记录 run.start”
“记录 text.delta”
“记录 tool.start”


"""

from dataclasses import dataclass, field
from typing import Any
from app.domain.events import RunEvent, RunEventType
from app.repositories.events_repository import EventRepository


class EventRecorder:
    def __init__(self, user_id: str, thread_id: str, run_id: str,event_repository: EventRepository | None = None,) -> None:
        self._user_id = user_id
        self._thread_id = thread_id
        self._run_id = run_id
        self._event_repository = event_repository or EventRepository()  # 如果没有传入 EventRepository 实例，则创建一个新的实例



    def record_event(self, event_type: RunEventType, payload: dict[str, Any]) -> RunEvent:
        """
        记录一个事件
        :param event_type: 事件类型，例如 "run.start", "text.delta", "tool.start" 等
        :param payload: 事件的负载数据，包含具体的事件信息
        """
        # 这里可以将事件存储到数据库或日志中
        # 例如：
        # event = RunEvent(run_id=self._run_id, thread_id=self._thread_id, type=event_type, payload=payload)
        # self._event_repository.create(event)

        event = RunEvent(
            run_id=self._run_id,
            thread_id=self._thread_id, 
            event_type=event_type,
            payload=payload,
        )
        
        return self._event_repository.append(event, self._user_id)