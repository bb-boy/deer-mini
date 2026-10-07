"""创建或显式覆盖 Thread 文本文件，成功仅在原子写入及 fsync 完成后返回。"""
from pydantic import Field, ValidationError

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.async_io import run_sync
from app.runtime.context import RuntimeContext
from app.storage.errors import StorageError, StorageLimitError
from app.tools.file_access import (
    Encoding, MAX_FILE_BYTES, NativeFileError, PathArgs, error_result, json_result, mutate_file,
)


class WriteFileArgs(PathArgs):
    content: str = Field(description="完整文本内容")
    overwrite: bool = Field(default=False, description="必须显式开启才能覆盖已存在文件")
    encoding: Encoding = "utf-8"


def _write(args: WriteFileArgs, context: RuntimeContext) -> dict:
    content = args.content.encode(args.encoding)
    if len(content) > MAX_FILE_BYTES:
        raise StorageLimitError("文件超过 8 MiB 上限")
    return mutate_file(
        context, args.path, overwrite=args.overwrite, require_existing=False,
        transform=lambda old, path: (content, {"bytes_written": len(content)}),
    )


class WriteFileTool:
    definition = ToolDefinition(
        name="write_file", description="创建文本文件及父目录；覆盖已有文件必须设置 overwrite=true",
        parameters=WriteFileArgs.model_json_schema(),
    )

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        try:
            args = WriteFileArgs.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.id, self.definition.name, "write_file 参数格式错误", is_error=True)
        try:
            return json_result(call, await run_sync(_write, args, context))
        except (NativeFileError, StorageError, StorageLimitError, UnicodeError, OSError) as error:
            return error_result(call, error)
