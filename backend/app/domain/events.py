



from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, List

from app.domain.common import new_id, utc_now



RunEventType = Literal[
    "run.start",
    "text.delta",
    "tool.start",
    "tool.end",
    "run.end",
    "run.error",
    ]



@dataclass
class RunEvent:
    run_id: str
    thread_id: str
    event_type: RunEventType
    payload: dict[str, Any]   ## = field(default_factory=dict)不这样写的原因是，每次记录事件时，都必须明确写出这条事件携带什么信息。如果这样写了就会默认生成一个空字典，会导致一个空事件
    

    id: str = field(default_factory=new_id)  # 每次创建一个实例，都默认生成一个新的uuid
    sequence: int | None = None  # 事件的顺序号，None表示未分配顺序号
    created_at: str = field(default_factory=utc_now)