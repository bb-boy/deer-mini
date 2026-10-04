


from typing import Any, Literal, List
from dataclasses import dataclass, field

from app.domain.messages import Message
from app.domain.subagents import SubagentTask
from app.domain.todos import TodoItem
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

    #这个会话所有的消息列表
    messages: List[Message] = field(default_factory=list)
    workspace_path: str | None = None
    # 主对话与各子任务分别保存消息；整个字典随父对话的 Checkpoint 保存。
    subtasks: dict[str, SubagentTask] = field(default_factory=dict)
    # 清单和最后一次写入的身份随完整快照保存；身份由后台填写，模型不能指定。
    todos: list[TodoItem] = field(default_factory=list)
    todos_run_id: str | None = None
    todos_tool_call_id: str | None = None

    def __post_init__(self) -> None:
        self._validate_subtasks()
        self._validate_todos()

    def _validate_todos(self) -> None:
        """避免损坏的快照被当作有效计划；旧快照允许没有清单和所属身份。"""
        if not isinstance(self.todos, list):
            raise ValueError("ThreadState.todos 必须是列表")
        for todo in self.todos:
            if not isinstance(todo, TodoItem):
                raise ValueError("todos 的值必须是 TodoItem 对象")
            todo.to_dict()
        identities = (self.todos_run_id, self.todos_tool_call_id)
        if identities == (None, None):
            if self.todos:
                raise ValueError("非空清单必须有所属 Run 和工具调用编号")
        elif any(not isinstance(value, str) or not value.strip() for value in identities):
            raise ValueError("todos_run_id 和 todos_tool_call_id 必须同时为非空字符串")

    def _validate_subtasks(self) -> None:
        """检查子任务编号和归属；不查询数据库，也不改变任务状态。"""
        if not isinstance(self.subtasks, dict):
            raise ValueError("ThreadState.subtasks 必须是字典")
        for task_id, task in self.subtasks.items():
            if not isinstance(task, SubagentTask):
                raise ValueError("subtasks 的值必须是 SubagentTask 对象")
            if task_id != task.task_id:
                raise ValueError("subtasks 的字典键必须与 task_id 一致")
            if task.thread_id != self.thread_id or task.user_id != self.user_id:
                raise ValueError("子任务必须属于当前用户和 Thread")



    #threadstate对象转化为字典，方便存储和传输
    def to_dict(self) -> dict[str, Any]:
        self._validate_subtasks()
        self._validate_todos()
        return {
            "thread_id": self.thread_id,
            "user_id": self.user_id,
            "messages": [message.to_dict() for message in self.messages],
            "workspace_path": self.workspace_path,
            "subtasks": {
                task_id: task.to_dict() for task_id, task in self.subtasks.items()
            },
            "todos": [todo.to_dict() for todo in self.todos],
            "todos_run_id": self.todos_run_id,
            "todos_tool_call_id": self.todos_tool_call_id,
        }

    @classmethod #调用这个函数时，请自动把 ThreadState 这个类放进第一个空位。
    def from_dict(cls, data: dict[str, Any]) -> "ThreadState":
        # 已有 Checkpoint 没有 subtasks 字段，按空字典恢复即可继续使用。
        raw_subtasks = data.get("subtasks", {})
        if not isinstance(raw_subtasks, dict):
            raise ValueError("ThreadState.subtasks 必须是字典")
        raw_todos = data.get("todos", [])
        if not isinstance(raw_todos, list):
            raise ValueError("ThreadState.todos 必须是列表")
        return cls(
            thread_id=data["thread_id"],
            user_id=data["user_id"],
            messages=[Message.from_dict(message) for message in data.get("messages", [])],
            workspace_path=data["workspace_path"],
            subtasks={
                task_id: SubagentTask.from_dict(task)
                for task_id, task in raw_subtasks.items()
            },
            todos=[TodoItem.from_dict(todo) for todo in raw_todos],
            todos_run_id=data.get("todos_run_id"),
            todos_tool_call_id=data.get("todos_tool_call_id"),
        )
