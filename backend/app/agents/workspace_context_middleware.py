"""把本次 Run 的 workspace 和工具信息注入模型上下文。"""

from app.agents.middleware import AgentMiddleware
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.filesystem.thread_paths import VIRTUAL_WORKSPACE
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
        bash_hint = (
            f"bash 默认从 {VIRTUAL_WORKSPACE} 执行。\n"
            "上传资料位于 ../uploads，交付文件应保存到 ../outputs。\n"
            "每次命令都从默认工作目录开始，前一次 cd 不会自动延续。\n"
            if "bash" in tool_names
            else ""
        )
        system_message = Message(
            role="system",
            content=(
                f"{RUNTIME_CONTEXT_PREFIX}\n"
                f"当前用户：{context.user_id}\n"
                f"当前 Thread：{context.thread_id}\n"
                f"当前 Run：{context.run_id}\n"
                f"服务端工作目录：{context.workspace_path}\n"
                f"可用工具：{available_tools}\n"
                "read_file 可使用 /mnt/user-data/workspace/、"
                "/mnt/user-data/uploads/、/mnt/user-data/outputs/ 下的文件路径。\n"
                "read_file 的相对路径仅按 workspace 解析。\n"
                f"{bash_hint}"
                "需要读取文件时必须调用工具，不能猜测文件内容。"
            ),
        )
        state.messages.insert(0, system_message)
