"""把本次 Run 的 workspace 和工具信息注入模型上下文。"""

from app.agents.middleware import AgentMiddleware
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext
from app.tools.registry import ToolRegistry


RUNTIME_CONTEXT_PREFIX = "[deer_mini runtime context]"


class WorkspaceContextMiddleware(AgentMiddleware):
    """在 Agent 开始时加入一条当前 Run 专属的 system 消息。"""

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self._tool_registry = tool_registry

    async def before_agent(
        self,
        state: ThreadState,
        context: RuntimeContext,
    ) -> None:
        # 最新 Checkpoint 可能带有上一次 Run 的上下文；先移除再写入本次信息。
        state.messages = [
            message
            for message in state.messages
            if not (
                message.role == "system"
                and message.content.startswith(RUNTIME_CONTEXT_PREFIX)
            )
        ]

        tool_names = [
            definition.name
            for definition in self._tool_registry.definitions()
        ]
        available_tools = ", ".join(tool_names) if tool_names else "无"
        system_message = Message(
            role="system",
            content=(
                f"{RUNTIME_CONTEXT_PREFIX}\n"
                f"当前用户：{context.user_id}\n"
                f"当前 Thread：{context.thread_id}\n"
                f"当前 Run：{context.run_id}\n"
                f"当前工作目录：{context.workspace_path}\n"
                f"可用工具：{available_tools}\n"
                "工具路径必须使用相对于当前工作目录的相对路径。"
                "需要读取文件时必须调用工具，不能猜测文件内容。"
            ),
        )
        state.messages.insert(0, system_message)
