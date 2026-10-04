"""The staged read_file implementation, one behavior at a time."""

import asyncio

from app.domain.messages import ToolCall
from app.runtime.context import RuntimeContext
from app.tools.read_file_rewrite import ReadFileTool


def test_missing_or_invalid_path_is_rejected_before_reading(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(tmp_path),
        record_event, save_checkpoint,
    )
    tool = ReadFileTool()

    for arguments in ({}, {"path": ""}, {"path": "   "}, {"path": 123}, {"path": "ok.txt", "extra": 1}):
        result = asyncio.run(tool.execute(ToolCall("call-1", "read_file", arguments), context))
        assert result.is_error is True
        assert result.content == "read_file 参数格式错误"


def test_path_cannot_escape_the_current_thread(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    workspace = tmp_path / "workspace"
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir()
    (workspace / "outside_link").symlink_to(tmp_path.parent)
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(workspace),
        record_event, save_checkpoint,
    )

    for path in ("/etc/passwd", "workspace/../outputs/a.txt", "workspace/outside_link/file.txt"):
        result = asyncio.run(ReadFileTool().execute(
            ToolCall("call-1", "read_file", {"path": path}), context,
        ))
        assert result.is_error is True
        assert result.content.startswith("文件路径无效：")


def test_missing_file_and_directory_are_rejected(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    workspace = tmp_path / "workspace"
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir()
    (workspace / "folder").mkdir()
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(workspace),
        record_event, save_checkpoint,
    )

    cases = (
        ("workspace/missing.txt", "文件 workspace/missing.txt 不存在"),
        ("workspace/folder", "workspace/folder 是一个目录，不能读取"),
    )
    for path, expected in cases:
        result = asyncio.run(ReadFileTool().execute(
            ToolCall("call-1", "read_file", {"path": path}), context,
        ))
        assert result.is_error is True
        assert result.content == expected


def test_existing_utf8_file_returns_its_text(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    workspace = tmp_path / "workspace"
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir()
    (workspace / "hello.txt").write_text("你好，世界", encoding="utf-8")
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(workspace),
        record_event, save_checkpoint,
    )

    result = asyncio.run(ReadFileTool().execute(
        ToolCall("call-1", "read_file", {"path": "workspace/hello.txt"}), context,
    ))

    assert result.is_error is False
    assert result.tool_call_id == "call-1"
    assert result.content == "你好，世界"


def test_non_utf8_file_returns_an_error(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    workspace = tmp_path / "workspace"
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir()
    (workspace / "binary.bin").write_bytes(b"\xff")
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(workspace),
        record_event, save_checkpoint,
    )

    result = asyncio.run(ReadFileTool().execute(
        ToolCall("call-2", "read_file", {"path": "workspace/binary.bin"}), context,
    ))

    assert result.is_error is True
    assert result.content == "文件 workspace/binary.bin 不是 UTF-8 文本，无法读取"


def test_gb18030_file_can_be_read_with_explicit_encoding(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    workspace = tmp_path / "workspace"
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir()
    (workspace / "chinese.txt").write_bytes("你好".encode("gb18030"))
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(workspace),
        record_event, save_checkpoint,
    )

    result = asyncio.run(ReadFileTool().execute(
        ToolCall("call-3", "read_file", {
            "path": "workspace/chinese.txt", "encoding": "gb18030",
        }), context,
    ))

    assert result.is_error is False
    assert result.content == "你好"


def test_unknown_encoding_is_rejected_as_invalid_arguments(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(tmp_path),
        record_event, save_checkpoint,
    )
    result = asyncio.run(ReadFileTool().execute(
        ToolCall("call-4", "read_file", {
            "path": "workspace/chinese.txt", "encoding": "not-a-real-encoding",
        }), context,
    ))

    assert result.is_error is True
    assert result.content == "read_file 参数格式错误"


def test_base_and_offset_select_a_line_range(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    workspace = tmp_path / "workspace"
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir()
    (workspace / "lines.txt").write_text(
        "".join(f"第 {number} 行\n" for number in range(1, 21)), encoding="utf-8"
    )
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(workspace),
        record_event, save_checkpoint,
    )

    result = asyncio.run(ReadFileTool().execute(
        ToolCall("call-5", "read_file", {
            "path": "workspace/lines.txt", "base": 10, "offset": 5,
        }), context,
    ))

    assert result.is_error is False
    assert result.content == "".join(f"第 {number} 行\n" for number in range(10, 15))


def test_base_without_offset_reads_to_end(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    workspace = tmp_path / "workspace"
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir()
    (workspace / "lines.txt").write_text("一\n二\n三\n", encoding="utf-8")
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(workspace),
        record_event, save_checkpoint,
    )

    result = asyncio.run(ReadFileTool().execute(
        ToolCall("call-6", "read_file", {
            "path": "workspace/lines.txt", "base": 2,
        }), context,
    ))

    assert result.is_error is False
    assert result.content == "二\n三\n"


def test_base_and_offset_must_be_positive_integers(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(tmp_path),
        record_event, save_checkpoint,
    )
    for extra in ({"base": 0}, {"base": True}, {"offset": 0}, {"offset": "5"}):
        result = asyncio.run(ReadFileTool().execute(
            ToolCall("call-7", "read_file", {"path": "workspace/lines.txt", **extra}), context,
        ))
        assert result.is_error is True
        assert result.content == "read_file 参数格式错误"


def test_long_tool_result_is_complete_for_storage_middleware(tmp_path):
    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    workspace = tmp_path / "workspace"
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir()
    (workspace / "large.txt").write_text("x" * 10001, encoding="utf-8")
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(workspace),
        record_event, save_checkpoint,
    )

    result = asyncio.run(ReadFileTool().execute(
        ToolCall("call-8", "read_file", {"path": "workspace/large.txt"}), context,
    ))

    assert result.is_error is False
    assert result.content == "x" * 10001
