


from typing import Any, Literal, List
from dataclasses import dataclass, field

from app.domain.messages import Message
from app.domain.common import new_id, utc_now




ThreadStatus = Literal["idle", "running"]


@dataclass   #@dataclass 中，必填字段永远放在可选字段前面，
class Thread:
    id: str
    user_id: str
    workspace_path: str #必填字段
    title: str | None = None
    status: ThreadStatus = "idle"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)



@dataclass
class ThreadState:
    thread_id: str
    user_id: str
    messages: List[Message] = field(default_factory=list)
    workspace_path: str | None = None



    #threadstate对象转化为字典，方便存储和传输
    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "messages": [message.to_dict() for message in self.messages],
            "workspace_path": self.workspace_path
        }

    @classmethod #调用这个函数时，请自动把 ThreadState 这个类放进第一个空位。
    def from_dict(cls, data: dict[str, Any]) -> "ThreadState":
        return cls(
            thread_id=data["thread_id"],
            user_id=data["user_id"],
            messages=[Message.from_dict(message) for message in data.get("messages", [])],
            workspace_path=data["workspace_path"],
        )