

"""
输入给 Agent：
- 这是谁的任务：user_id
- 属于哪个对话：thread_id
- 属于哪次执行：run_id
- 能操作哪个工作目录：workspace_path
- 能记录事件：record_event
- 能保存状态：save_checkpoint
"""








from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any
from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent, RunEventType
from app.domain.threads import ThreadState

#定义一个接受 RunEventType 和 dict[str, any] 参数，并返回 RunEvent 的可调用类型
RecordEvent = Callable[[RunEventType, dict[str, Any]], Awaitable[RunEvent]]


#定义一个输入是user_id,thread_id,workspace_path,和一个消息列表的threadstate，输出是checkpoint的可调用类型
SaveCheckpoint = Callable[[ThreadState], Checkpoint]


@dataclass(frozen =True)
class RuntimeContext:
    user_id: str
    thread_id: str
    run_id: str
    workspace_path: str

    record_event: RecordEvent

    save_checkpoint: SaveCheckpoint