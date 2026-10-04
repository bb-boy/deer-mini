"""任务清单的数据形状；只校验和转换内存数据，不操作数据库或执行任务。"""

from dataclasses import dataclass
import json
from typing import Any, Literal


TodoStatus = Literal["pending", "in_progress", "completed"]


@dataclass
class TodoItem:
    """content 是用户能看懂的一项工作；status 表示待办、进行中或已完成。"""

    content: str
    status: TodoStatus

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("todo.content 必须是非空字符串")
        if not isinstance(self.status, str) or self.status not in {
            "pending", "in_progress", "completed",
        }:
            raise ValueError("todo.status 必须是 pending、in_progress 或 completed")

    def to_dict(self) -> dict[str, str]:
        self.__post_init__()
        return {"content": self.content, "status": self.status}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TodoItem":
        if not isinstance(data, dict) or set(data) != {"content", "status"}:
            raise ValueError("每项 todo 只接受 content 和 status")
        return cls(content=data["content"], status=data["status"])


def todo_result_content(todos: list[TodoItem], run_id: str, tool_call_id: str) -> str:
    """把已保存的清单及后台身份转成工具结果，供模型、界面和取消收尾共用。"""
    return json.dumps({
        "status": "saved", "run_id": run_id, "tool_call_id": tool_call_id,
        "todos": [todo.to_dict() for todo in todos],
    }, ensure_ascii=False)
