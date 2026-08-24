



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
    
    
    # E.G:
    """
    RunEvent(
    event_type="run.start",
    payload={
        "model_name": "deepseek-chat",
        "thinking_enabled": True,
    },
)
    """

    id: str = field(default_factory=new_id)  # 每次创建一个实例，都默认生成一个新的uuid
    sequence: int | None = None  # 一个run内事件的顺序号，None表示未分配顺序号，一个 Run 内，从 1 开始递增的事件编号

    """
    Run r-001
    sequence=1   run.start
    sequence=2   text.delta：我来读取文件……
    sequence=3   tool.start：read_file
    sequence=4   tool.end：read_file 成功
    sequence=5   text.delta：文件内容是……
    sequence=6   run.end
    """

    created_at: str = field(default_factory=utc_now)


    def to_dict(self) -> dict[str, Any]:
        """
        将 RunEvent 对象转换为字典形式，方便存储或传输
        """
        return {
            "id": self.id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "sequence": self.sequence,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunEvent":
        """
        从字典形式创建 RunEvent 对象
        """
        return cls(
            id=data["id"],
            run_id=data["run_id"],
            thread_id=data["thread_id"],
            event_type=data["event_type"],
            payload=data["payload"],
            sequence=data["sequence"],
            created_at=data["created_at"],
        )

