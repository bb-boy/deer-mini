"""
功能：执行一次 ToolCall，并把 ToolResult 转成 Message(role="tool")。
输入：ToolCall、RuntimeContext。
输出：一条工具消息，交回模型。
副作用：ReadFileTool 本身会读文件；Executor 不直接写 SQLite。
"""







from app.runtime.context import RuntimeContext
from app.tools.registry import ToolRegistry

from app.domain.messages import ToolCall, Message



class ToolExecutor:

    def __init__(self, tool_registry: ToolRegistry):
        self.tool_registry = tool_registry


    async def execute(self, call: ToolCall, context: RuntimeContext) -> Message:
        """
        执行一次 ToolCall，并把 ToolResult 转成 Message(role="tool")。
        :param call: 一个工具调用，例如 {"name": "read_file", "arguments": {"path": "report.txt"}}。
        :param context: 当前 Run 的上下文；其中的 workspace_path 表示这个工具唯一允许操作的 Thread 工作目录。
        :return: 一条工具消息，交回模型。
        """
        #1 根据工具名从注册表中取出对应的工具，返回一个 Tool 对象
        tool = self.tool_registry.get(call.name)

        if not tool:
            return Message(
                role="tool",
                content=f"找不到工具 {call.name}。",
                tool_call_id=call.id,
            )

        #2 执行工具的 execute 方法，得到 ToolResult
        try:
            result = await tool.execute(call, context)
        except Exception as e:
            return Message(
                role="tool",
                content=f"执行工具 {call.name} 时出错：{str(e)}。",
                tool_call_id=call.id,
            )

        #3 把 ToolResult 转成 Message(role="tool")
        message = Message(
            role="tool",
            content=result.content,
            tool_call_id=result.tool_call_id,
        )

        return message