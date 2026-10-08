"""按相对模式列出当前 Thread 文件，不依赖系统搜索程序。"""
from contextlib import closing
import json

from pydantic import Field, ValidationError, field_validator

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.async_io import run_sync
from app.runtime.context import RuntimeContext
from app.storage.errors import StorageError
from app.storage.file_io import open_regular_file
from app.tools.file_access import (
    NativeFileError, PathArgs, ScanReport, error_result, json_result,
    matches_glob, resolve_target, validate_pattern, walk_files,
)


MAX_GLOB_OUTPUT_BYTES = 4 * 1024 * 1024


class GlobArgs(PathArgs):
    path: str = Field(default="workspace", min_length=1, max_length=4096)
    pattern: str = Field(min_length=1, max_length=4096, description="相对 glob 模式，例如 **/*.py")
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("pattern")
    @classmethod
    def safe_pattern(cls, value: str) -> str:
        return validate_pattern(value)


def _glob(args: GlobArgs, context: RuntimeContext) -> dict:
    report = ScanReport()
    files: list[str] = []
    output_bytes = 2
    with closing(walk_files(resolve_target(context, args.path), report)) as candidates:
        for fd, name, path, relative in candidates:
            if matches_glob(relative, args.pattern):
                # 重新用 NOFOLLOW 打开；扫描后被替换为链接的入口也不能返回为普通文件。
                try:
                    with open_regular_file(fd, name):
                        pass
                except StorageError as error:
                    report.skip(path, error.category)
                    continue
                if len(files) >= args.limit:
                    report.truncated = True
                    break
                path_bytes = len(json.dumps(path, ensure_ascii=False).encode("utf-8")) + (2 if files else 0)
                if output_bytes + path_bytes > MAX_GLOB_OUTPUT_BYTES:
                    report.skip(path, "output_limit", stop=True)
                    break
                output_bytes += path_bytes
                files.append(path)
    return {"files": sorted(files), **report.metadata()}


class GlobTool:
    definition = ToolDefinition(
        name="glob", description="在当前 Thread 中按 glob 模式匹配文件，** 可递归匹配并包含顶层文件",
        parameters=GlobArgs.model_json_schema(),
    )

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        try:
            args = GlobArgs.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.id, self.definition.name, "glob 参数格式错误", is_error=True)
        try:
            return json_result(call, await run_sync(_glob, args, context))
        except (NativeFileError, StorageError, OSError) as error:
            return error_result(call, error)
