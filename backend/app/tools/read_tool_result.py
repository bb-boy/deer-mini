"""按字符读取当前对话中已保存的工具结果。"""

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.filesystem.tool_result_store import read_chars
from app.runtime.async_io import run_sync
from app.runtime.context import RuntimeContext


class ReadToolResultArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, description="工具结果返回的 .tool-results 虚拟文件路径")
    offset: int = Field(default=0, ge=0, description="从第几个字符开始，0 表示开头")
    limit: int = Field(default=2000, ge=1, le=50_000, description="读取字符数，最大 50000")


class ReadToolResultTool:
    definition = ToolDefinition(
        name="read_tool_result",
        description="按字符分段读取已落盘工具结果；offset 从 0 开始，可读取长单行文本",
        parameters=ReadToolResultArgs.model_json_schema(),
    )

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        try:
            args = ReadToolResultArgs.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.id, self.definition.name, "read_tool_result 参数格式错误", is_error=True)
        try:
            content = await run_sync(read_chars, context.workspace_path, args.path, args.offset, args.limit)
        except (ValueError, OSError, RuntimeError) as error:
            return ToolResult(call.id, self.definition.name, f"读取工具结果失败：{error}", is_error=True)
        return ToolResult(call.id, self.definition.name, content)
