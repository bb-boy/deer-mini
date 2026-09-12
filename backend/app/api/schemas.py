"""HTTP 请求和响应的数据结构。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.messages import Role
from app.domain.runs import RunStatus
from app.domain.threads import ThreadStatus
from app.model.config import DEFAULT_MODEL_NAME


class CreateThreadRequest(BaseModel):
    """创建一段新对话所需的用户信息。"""

    user_id: str = Field(min_length=1)
    title: str | None = None


class UpdateThreadRequest(BaseModel):
    """更新 Thread 展示信息。"""

    title: str = Field(min_length=1, max_length=200)


class ThreadResponse(BaseModel):
    """返回给浏览器的 Thread 信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    workspace_path: str
    title: str | None
    status: ThreadStatus
    created_at: str
    updated_at: str


class CreateRunRequest(BaseModel):
    """启动一次 Agent 执行所需的真实输入。"""

    user_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    model_name: str = DEFAULT_MODEL_NAME
    thinking_enabled: bool = False
    reasoning_effort: str | None = None


class RunResponse(BaseModel):
    """返回给浏览器的 Run 状态。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    thread_id: str
    user_id: str
    status: RunStatus
    model_name: str | None
    thinking_enabled: bool
    reasoning_effort: str | None
    error: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str
    updated_at: str


class ToolCallResponse(BaseModel):
    """Checkpoint 中模型发起的一次工具调用。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    arguments: dict[str, Any]


class MessageResponse(BaseModel):
    """Checkpoint 中的一条用户、模型或工具消息。"""

    model_config = ConfigDict(from_attributes=True)

    role: Role
    content: str
    id: str
    created_at: str
    tool_calls: list[ToolCallResponse]
    tool_call_id: str | None
    reasoning_content: str | None


class ThreadStateResponse(BaseModel):
    """某个关键步骤保存的完整对话状态。"""

    model_config = ConfigDict(from_attributes=True)

    thread_id: str
    user_id: str
    messages: list[MessageResponse]
    workspace_path: str | None


class CheckpointResponse(BaseModel):
    """一次 Agent 关键步骤及当时的完整 ThreadState。"""

    model_config = ConfigDict(from_attributes=True)

    id: int | None
    thread_id: str
    run_id: str
    step: int
    state: ThreadStateResponse
    created_at: str


class WorkspaceFileResponse(BaseModel):
    """浏览器展示和下载 Workspace 文件所需的元数据。"""

    model_config = ConfigDict(from_attributes=True)

    relative_path: str
    name: str
    size: int
    modified_at: str
