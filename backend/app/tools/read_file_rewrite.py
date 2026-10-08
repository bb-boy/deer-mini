"""严格校验、受限安全 IO、编码与按行读取；完整选中结果交公共落盘中间件。"""
from itertools import islice
from io import StringIO

from pydantic import Field, ValidationError

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.async_io import run_sync
from app.runtime.context import RuntimeContext
from app.storage.errors import StorageError, StorageLimitError
from app.tools.file_access import (
    Encoding, NativeFileError, PathArgs, error_result, read_bytes, resolve_target,
)


class ReadFileArgs(PathArgs):
    encoding: Encoding = Field(default="utf-8", description="支持 UTF-8、GB18030、UTF-16")
    base: int = Field(default=1, ge=1, description="起始行号，从 1 开始")
    offset: int | None = Field(default=None, ge=1, description="读取行数；省略则读取到末尾")
    line_numbers: bool = Field(default=False, description="为选中内容添加真实行号")
    preserve_newlines: bool = Field(
        default=False, description="保留原始 CRLF/CR 换行，供 edit_file 精确匹配；默认规范化为 LF"
    )


def _read(args: ReadFileArgs, context: RuntimeContext) -> str:
    content = read_bytes(resolve_target(context, args.path)).decode(args.encoding)
    end = None if args.offset is None else args.base - 1 + args.offset
    lines = islice(StringIO(content, newline="" if args.preserve_newlines else None), args.base - 1, end)
    if args.line_numbers:
        return "".join(f"{number}: {line}" for number, line in enumerate(lines, args.base))
    return "".join(lines)


class ReadFileTool:
    definition = ToolDefinition(
        name="read_file",
        description="读取当前对话 workspace、uploads 或 outputs 文本，可按行范围读取及显示行号（单文件上限 8 MiB）",
        parameters=ReadFileArgs.model_json_schema(),
    )

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        try:
            args = ReadFileArgs.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.id, self.definition.name, "read_file 参数格式错误", is_error=True)
        try:
            return ToolResult(call.id, self.definition.name, await run_sync(_read, args, context))
        except UnicodeDecodeError:
            message = f"文件 {args.path} 不是 {args.encoding.upper()} 文本，无法读取"
        except NativeFileError as error:
            message = {
                "not_found": f"文件 {args.path} 不存在",
                "directory": f"{args.path} 是一个目录，不能读取",
                "unsafe_file": f"{args.path} 不是普通文件，不能读取",
                "invalid_path": "文件路径无效：只能访问当前 Thread 的普通文件，禁止链接和内部路径",
            }.get(str(error), "读取文件失败")
        except StorageError as error:
            if error.category == "not_found":
                message = f"文件 {args.path} 不存在"
            else:
                return error_result(call, error)
        except (StorageLimitError, OSError) as error:
            return error_result(call, error)
        return ToolResult(call.id, self.definition.name, message, is_error=True)
