"""串行读取并精确替换文本；歧义/缺失匹配不写入，保留编码和原有换行。"""
from difflib import unified_diff
from io import StringIO
from itertools import chain

from pydantic import Field, ValidationError

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.async_io import run_sync
from app.runtime.context import RuntimeContext
from app.storage.errors import StorageError, StorageLimitError
from app.tools.file_access import (
    Encoding, MAX_FILE_BYTES, NativeFileError, PathArgs, error_result, json_result, mutate_file,
)


class EditFileArgs(PathArgs):
    old_string: str = Field(min_length=1, description="需要精确匹配的原文，不能为空")
    new_string: str = Field(description="替换后的文本，可以为空")
    replace_all: bool = Field(default=False, description="允许替换全部非重叠匹配")
    encoding: Encoding = "utf-8"



MAX_CONTEXT_DIFF_CHARS = 128 * 1024
MAX_CONTEXT_DIFF_LINES = 2000


def _diff(original: str, updated: str, path: str) -> tuple[str, str]:
    """小文件用上下文 diff；大文件线性生成完整单 hunk，避免二次复杂度。"""
    if original == updated:
        return "", "context"
    old_count = original.count("\n") + bool(original and not original.endswith("\n"))
    new_count = updated.count("\n") + bool(updated and not updated.endswith("\n"))
    if max(len(original), len(updated)) <= MAX_CONTEXT_DIFF_CHARS and max(old_count, new_count) <= MAX_CONTEXT_DIFF_LINES:
        lines = unified_diff(list(StringIO(original)), list(StringIO(updated)), fromfile=path, tofile=path)
        diff_format = "context"
    else:
        old_range = f"1,{old_count}" if old_count else "0,0"
        new_range = f"1,{new_count}" if new_count else "0,0"
        lines = chain(
            [f"--- {path}\n", f"+++ {path}\n", f"@@ -{old_range} +{new_range} @@\n"],
            ("-" + line for line in StringIO(original)),
            ("+" + line for line in StringIO(updated)),
        )
        diff_format = "whole_file"
    output = StringIO()
    for line in lines:
        output.write(line)
        if not line.endswith("\n"):
            output.write("\n\\ No newline at end of file\n")
    return output.getvalue(), diff_format


def _edit(args: EditFileArgs, context: RuntimeContext) -> dict:
    def transform(raw: bytes | None, path: str) -> tuple[bytes, dict]:
        assert raw is not None
        if args.encoding == "utf-16" and raw[:2] not in (b"\xfe\xff", b"\xff\xfe"):
            raise UnicodeError("UTF-16 文件需要 BOM")
        original = raw.decode(args.encoding)
        count = original.count(args.old_string)
        if count == 0:
            raise NativeFileError("missing_match")
        if count > 1 and not args.replace_all:
            raise NativeFileError("ambiguous_match")
        # 替换前估算字符长度，避免短模式乘超大 replacement 造成无界分配。
        if len(original) + count * (len(args.new_string) - len(args.old_string)) > MAX_FILE_BYTES:
            raise StorageLimitError("替换结果超过上限")
        updated = original.replace(args.old_string, args.new_string)
        encoding = args.encoding
        prefix = b""
        if encoding == "utf-16":
            # Python 默认 utf-16 编码使用本机字节序；显式沿用原文件 BOM 的字节序。
            prefix = raw[:2]
            encoding = "utf-16-be" if prefix == b"\xfe\xff" else "utf-16-le"
        content = prefix + updated.encode(encoding)
        if len(content) > MAX_FILE_BYTES:
            raise StorageLimitError("替换结果超过上限")
        diff, diff_format = _diff(original, updated, path)
        return content, {"replacements": count, "diff": diff, "diff_format": diff_format}
    return mutate_file(context, args.path, overwrite=True, require_existing=True, transform=transform)


class EditFileTool:
    definition = ToolDefinition(
        name="edit_file", description="精确替换文件文本并返回 diff；默认要求唯一匹配，支持 replace_all",
        parameters=EditFileArgs.model_json_schema(),
    )

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        try:
            args = EditFileArgs.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.id, self.definition.name, "edit_file 参数格式错误", is_error=True)
        try:
            return json_result(call, await run_sync(_edit, args, context))
        except (NativeFileError, StorageError, StorageLimitError, UnicodeError, OSError) as error:
            return error_result(call, error)
