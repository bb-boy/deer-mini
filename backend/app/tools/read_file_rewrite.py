"""Pydantic 校验、编码和按行读取；完整结果交给公共落盘中间件。"""

import asyncio
from itertools import islice
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.filesystem.thread_paths import ThreadPaths
from app.runtime.context import RuntimeContext


class ReadFileArgs(BaseModel):
    """模型传给 read_file 的参数格式。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, description="文件路径，例如 workspace/notes.txt 或 uploads/data.txt")
    encoding: Literal["utf-8", "gb18030", "utf-16"] = Field(
        default="utf-8", description="文本编码；默认 UTF-8，也支持 GB18030、UTF-16"
    )
    base: int = Field(default=1, ge=1, description="起始行号，从 1 开始")
    offset: int | None = Field(default=None, ge=1, description="读取的行数；省略则读到文件末尾")

    @field_validator("path")
    @classmethod
    def reject_blank_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path 不能为空白")
        return value


def _read_lines(path: Path, encoding: str, base: int, offset: int | None) -> str:
    with path.open("r", encoding=encoding) as file:
        end = None if offset is None else base - 1 + offset
        return "".join(islice(file, base - 1, end))


class ReadFileTool:
    # 第一段：告诉模型工具叫什么、做什么、需要什么参数。
    definition = ToolDefinition(
        name="read_file",
        description="读取当前对话 workspace、uploads 或 outputs 中的文本文件，可指定编码",
        parameters=ReadFileArgs.model_json_schema(),
    )

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        # 第二段：Pydantic 检查参数，得到已验证的 path。
        try:
            args = ReadFileArgs.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content="read_file 参数格式错误",
                is_error=True,
            )

        # 第三段：把模型的虚拟路径限制在当前对话的三个目录中。
        try:
            thread_dir = Path(context.workspace_path).parent
            paths = ThreadPaths(thread_dir)
            target = paths.resolve_agent_path(args.path)
        except (ValueError, OSError, RuntimeError) as error:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"文件路径无效：{error}",
                is_error=True,
            )

        # 第四段：路径合法以后，还要确认目标真的存在且是文件。
        if not target.exists():
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"文件 {args.path} 不存在",
                is_error=True,
            )
        if target.is_dir():
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"{args.path} 是一个目录，不能读取",
                is_error=True,
            )
        if not target.is_file():
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"{args.path} 不是普通文件，不能读取",
                is_error=True,
            )

        # 第五段：文件系统读取会等待磁盘，交给线程以免卡住 Agent 的事件循环。
        try:
            content = await asyncio.to_thread(_read_lines, target, args.encoding, args.base, args.offset)
        except UnicodeDecodeError:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"文件 {args.path} 不是 {args.encoding.upper()} 文本，无法读取",
                is_error=True,
            )
        except OSError as error:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"读取文件 {args.path} 失败：{error}",
                is_error=True,
            )

        # 返回完整选中内容；公共中间件负责预览与可恢复的全文落盘。
        return ToolResult(
            tool_call_id=call.id,
            name=self.definition.name,
            content=content,
        )
