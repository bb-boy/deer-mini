"""
消息的定义和处理。

你：读取 report.txt
模型：我要调用 read_file，参数是 report.txt
工具：文件内容是……
模型：根据文件内容，回答你……
"""

from datetime import datetime, timezone
from typing import Any, List, Literal
from dataclasses import dataclass, field
import uuid

Role = Literal["user", "assistant", "system", "tool"]


@dataclass
class ToolCall:
    id: str                 # 工具调用的唯一标识符，一次run可能会有很多次同一个toolcall
    name: str              # 工具名称
    arguments: dict[str, Any]      #工具调用的参数

    def to_dict(self) -> dict[str, Any]: #类的实例方法
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCall": #调用的时候类还不存在，更适合写成classmethod
        return cls(
            id=data["id"],
            name=data["name"],
            arguments=data["arguments"]
        )



@dataclass
class Message:
    role: Role
    content: str
    id: str = field(default_factory=lambda: str(uuid.uuid4())) #每次创建一个实例，都默认生成一个新的uuid
    created_at: str = field(default_factory=lambda:datetime.now(timezone.utc).isoformat()) #每次创建一个实例，都默认生成一个新的时间戳
    tool_calls: List[ToolCall] = field(default_factory=list) #每次创建一个实例，都默认生成一个新的list
    tool_call_id: str | None = None
    reasoning_content: str | None = None #记录推理过程

    #把message对象转化为字典，方便存储和传输
    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "id": self.id,
            "created_at": self.created_at,
            "tool_calls": [tool_call.to_dict() for tool_call in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "reasoning_content": self.reasoning_content
        }



    #把字典转化为message对象，方便从存储和传输中恢复
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            role=data["role"],
            content=data["content"],
            id=data["id"],
            created_at=data["created_at"],
            tool_calls=[ToolCall.from_dict(tc) for tc in data["tool_calls"]],
            tool_call_id=data["tool_call_id"],
            reasoning_content=data.get("reasoning_content")
        )
