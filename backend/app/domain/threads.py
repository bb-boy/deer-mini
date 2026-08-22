


from typing import Literal, List
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