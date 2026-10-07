"""有预算的 UTF-8 文本检索：逐行正则超时、总扫描预算和显式不完整报告。"""
from contextlib import closing
from io import StringIO
import json
import time

from pydantic import Field, ValidationError, field_validator
import regex

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.async_io import run_sync
from app.runtime.context import RuntimeContext
from app.storage.errors import StorageError, StorageLimitError
from app.storage.file_io import bounded_read
from app.tools.file_access import (
    NativeFileError, PathArgs, ScanReport, error_result, file_stat, json_result,
    matches_glob, resolve_target, validate_pattern, walk_files,
)

MAX_GREP_FILE_BYTES = 1024 * 1024
MAX_GREP_TOTAL_BYTES = 32 * 1024 * 1024
MATCH_SECONDS = 0.02
MAX_GREP_OUTPUT_BYTES = 4 * 1024 * 1024


class GrepArgs(PathArgs):
    path: str = Field(default="workspace", min_length=1, max_length=4096)
    pattern: str = Field(min_length=1, max_length=4096, description="逐行正则表达式或字面文本")
    glob: str = Field(default="**/*", min_length=1, max_length=4096)
    literal: bool = False
    case_sensitive: bool = True
    context_lines: int = Field(default=0, ge=0, le=5)
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("glob")
    @classmethod
    def safe_glob(cls, value: str) -> str:
        return validate_pattern(value)


def _grep(args: GrepArgs, context: RuntimeContext) -> dict:
    report = ScanReport()
    try:
        expression = regex.compile(regex.escape(args.pattern) if args.literal else args.pattern,
                                   0 if args.case_sensitive else regex.IGNORECASE)
    except (regex.error, RecursionError) as error:
        raise NativeFileError("invalid_regex") from error
    matches: list[dict] = []
    total_bytes = 0
    output_bytes = 2
    stop = False
    with closing(walk_files(resolve_target(context, args.path), report)) as candidates:
        for fd, name, path, relative in candidates:
            if report.expired(path):
                break
            if not matches_glob(relative, args.glob):
                continue
            remaining = MAX_GREP_TOTAL_BYTES - total_bytes
            if remaining <= 0:
                report.skip(path, "total_bytes_limit", stop=True)
                break
            try:
                value = file_stat(fd, name)
                if value is not None and value.st_size > MAX_GREP_FILE_BYTES:
                    report.skip(path, "file_size_limit")
                    continue
                if value is not None and value.st_size > remaining:
                    report.skip(path, "total_bytes_limit", stop=True)
                    break
                raw = bounded_read(fd, name, min(MAX_GREP_FILE_BYTES, remaining))
                if raw is None:
                    report.skip(path, "not_found")
                    continue
            except StorageLimitError:
                reason = "file_size_limit" if remaining >= MAX_GREP_FILE_BYTES else "total_bytes_limit"
                report.skip(path, reason, stop=reason == "total_bytes_limit")
                if report.truncated:
                    break
                continue
            except StorageError as error:
                report.skip(path, error.category)
                continue
            total_bytes += len(raw)
            if b"\x00" in raw:
                report.skip(path, "binary")
                continue
            try:
                lines = [line.removesuffix("\n") for line in StringIO(raw.decode("utf-8"), newline=None)]
            except UnicodeDecodeError:
                report.skip(path, "encoding")
                continue
            for index, line in enumerate(lines):
                if report.expired(path):
                    stop = True
                    break
                try:
                    matched = expression.search(line, timeout=min(MATCH_SECONDS, max(0.000001, report.deadline - time.monotonic())))
                except TimeoutError:
                    report.skip(path, "regex_timeout")
                    break
                if matched is None:
                    continue
                if len(matches) >= args.limit:
                    report.truncated = True
                    stop = True
                    break
                context_lines = [
                    {"line": other + 1, "text": lines[other]}
                    for other in range(max(0, index - args.context_lines),
                                       min(len(lines), index + args.context_lines + 1))
                    if other != index
                ]
                candidate = {"path": path, "line": index + 1, "text": line, "context": context_lines}
                candidate_bytes = len(json.dumps(candidate, ensure_ascii=False).encode("utf-8"))
                if output_bytes + candidate_bytes + (2 if matches else 0) > MAX_GREP_OUTPUT_BYTES:
                    report.skip(path, "output_limit", stop=True)
                    stop = True
                    break
                output_bytes += candidate_bytes + (2 if matches else 0)
                matches.append(candidate)
            if stop:
                break
    return {"matches": matches, "partial": report.skipped_count > 0 or report.truncated,
            **report.metadata()}


class GrepTool:
    definition = ToolDefinition(
        name="grep",
        description="在 Thread 文件中逐行搜索 UTF-8 文本，支持正则/字面量、上下文；跳过项目会明确报告",
        parameters=GrepArgs.model_json_schema(),
    )

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        try:
            args = GrepArgs.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.id, self.definition.name, "grep 参数格式错误", is_error=True)
        try:
            return json_result(call, await run_sync(_grep, args, context))
        except (NativeFileError, StorageError, OSError) as error:
            return error_result(call, error)
