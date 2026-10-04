"""记录一个子任务的身份、独立消息、状态和结果。


输入：后台提供 task_id、tool_call_id、user_id、thread_id、run_id；主 Agent
提供 description、prompt、subagent_type；执行器以后更新状态、消息和结果。
输出：SubagentTask 对象，或用于 Checkpoint JSON 的普通字典。
副作用：本模块只处理内存数据，不调用模型、工具、数据库或实时流。
"""

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from app.domain.common import new_id
from app.domain.messages import Message


# 它与主 Run 的状态分别记录。
SubagentStatus = Literal[
    "pending", "running", "completed", "failed", "cancelled", "timed_out"
]


@dataclass
class SubagentTask:
    """一张子任务工作单；创建对象不会启动子 Agent。

    tool_call_id 关联主模型发出的 task 调用；task_id 是后台自己的执行编号。
    即使不同 Run 的模型返回相同 tool_call_id，task_id 也应各自独立。
    user_id/thread_id/run_id 指明这是谁、哪段对话、哪次主任务的委派。
    description 用于简短展示；prompt 是交给子 Agent 的完整任务和背景；
    subagent_type 指定后续执行器选择哪份 Agent 配置。
    messages 保存子 Agent 自己的完整消息与工具过程，result/error 保存结果。
    """

    tool_call_id: str
    user_id: str
    thread_id: str
    run_id: str
    description: str
    prompt: str
    subagent_type: str = "general-purpose"
    task_id: str = field(default_factory=new_id)
    status: SubagentStatus = "pending"
    # 每张工作单创建自己的列表，多个子任务不会共用同一份消息。
    messages: list[Message] = field(default_factory=list)
    result: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        """对象建立后校验字段；只检查数据，不执行状态迁移。"""
        self._validate()

    def _validate(self) -> None:
        """检查当前工作单，错误时抛出 ValueError，阻止保存损坏的记录。"""
        for name in (
            "task_id", "tool_call_id", "user_id", "thread_id", "run_id",
            "description", "prompt", "subagent_type",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"子任务 {name} 必须是非空字符串")
        # get_args 取出上面 Literal 中允许的六个状态值。
        if self.status not in get_args(SubagentStatus):
            raise ValueError(f"无效的子任务状态：{self.status!r}")
        if not isinstance(self.messages, list) or any(
            not isinstance(message, Message) for message in self.messages
        ):
            raise ValueError("子任务 messages 必须是 Message 对象列表")
        for name in ("result", "error"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"子任务 {name} 必须是字符串或 None")

    def to_dict(self) -> dict[str, Any]:
        """把当前工作单转成可存入 JSON 的字典；不直接写数据库。"""
        # 工作单会在执行中更新，所以保存前再次校验，而不只检查初始值。
        self._validate()
        return {
            "task_id": self.task_id,
            "tool_call_id": self.tool_call_id,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "description": self.description,
            "prompt": self.prompt,
            "subagent_type": self.subagent_type,
            "status": self.status,
            "messages": [message.to_dict() for message in self.messages],
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubagentTask":
        """从 Checkpoint 字典恢复工作单；保留原任务编号和完整消息身份。"""
        if not isinstance(data, dict):
            raise ValueError("子任务记录必须是字典")
        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise ValueError("子任务 messages 必须是列表")
        return cls(
            task_id=data["task_id"],
            tool_call_id=data["tool_call_id"],
            user_id=data["user_id"],
            thread_id=data["thread_id"],
            run_id=data["run_id"],
            description=data["description"],
            prompt=data["prompt"],
            subagent_type=data.get("subagent_type", "general-purpose"),
            status=data.get("status", "pending"),
            messages=[Message.from_dict(message) for message in raw_messages],
            result=data.get("result"),
            error=data.get("error"),
        )
