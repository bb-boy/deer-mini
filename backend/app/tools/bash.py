"""模型可调用的 Bash 工具；真正执行由 CommandRunner 负责。"""

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext
from app.sandbox.base import CommandRunner


class BashTool:
    """在当前 Thread Workspace 的隔离环境中执行一条命令。"""

    definition = ToolDefinition(
        name="bash",
        description=(
            "在当前 Thread 的隔离 Workspace 中执行 Bash 命令。"
            "适合运行脚本、处理文件和检查结果；不要启动长期前台服务。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "用简短文字说明为什么要执行这条命令。",
                },
                "command": {
                    "type": "string",
                    "description": "要在当前 Workspace 中执行的 Bash 命令。",
                },
            },
            "required": ["description", "command"],
            "additionalProperties": False,
        },
    )

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    async def execute(
        self,
        call: ToolCall,
        context: RuntimeContext,
    ) -> ToolResult:
        description = call.arguments.get("description")
        if not isinstance(description, str):
            return self._error(call, "执行说明必须是字符串")
        if not description.strip():
            return self._error(call, "执行说明不能为空")

        command = call.arguments.get("command")
        if not isinstance(command, str):
            return self._error(call, "Bash 命令必须是字符串")
        if not command.strip():
            return self._error(call, "Bash 命令不能为空")

        try:
            result = await self._runner.run(
                command=command,
                workspace_path=context.workspace_path,
                user_id=context.user_id,
                thread_id=context.thread_id,
                run_id=context.run_id,
                tool_call_id=call.id,
            )
        except Exception as error:
            return self._error(call, f"Bash 执行失败：{error}")

        output = result.output or "(no output)"
        if result.timed_out:
            return self._error(
                call,
                f"{output}\n[命令执行超时，容器已停止]",
            )
        if result.exit_code != 0:
            return self._error(
                call,
                f"{output}\n[Exit Code: {result.exit_code}]",
            )
        return ToolResult(
            tool_call_id=call.id,
            name=self.definition.name,
            content=output,
        )

    @classmethod
    def _error(cls, call: ToolCall, message: str) -> ToolResult:
        return ToolResult(
            tool_call_id=call.id,
            name=cls.definition.name,
            content=message,
            is_error=True,
        )
