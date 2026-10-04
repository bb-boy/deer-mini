"""大结果按字符分页读取，访问范围限定在当前对话落盘目录。"""

import asyncio

import pytest

from app.domain.messages import Message, ToolCall
from app.api.schemas import MessageResponse
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext
from app.agents.middleware import MiddlewareManager
from app.agents.middleware_stack import build_runtime_middlewares


def context_for(tmp_path):
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir(exist_ok=True)
    return RuntimeContext("user", "thread", "run", str(tmp_path / "workspace"), None, None)


def test_readback_can_page_a_long_single_line_and_preserves_unicode(tmp_path):
    from app.tools.read_tool_result import ReadToolResultTool
    context = context_for(tmp_path)
    text = "".join(str(i % 10) for i in range(2000)) + "汉🙂" * 30_000
    message = Message(role="tool", content=text, tool_call_id="large")

    async def get():
        return message

    async def scenario():
        result = await MiddlewareManager(build_runtime_middlewares()).wrap_tool_call(
            ThreadState("thread", "user"), context, ToolCall("large", "text", {}), get,
        )
        tool = ReadToolResultTool()
        page = await tool.execute(ToolCall("page", "read_tool_result", {
            "path": result.tool_result_file, "offset": 2000, "limit": 4000,
        }), context)
        assert not page.is_error and page.tool_call_id == "page"
        assert page.content == text[2000:6000]
        last = await tool.execute(ToolCall("end", "read_tool_result", {
            "path": result.tool_result_file, "offset": len(text), "limit": 100,
        }), context)
        assert last.content == "" and not last.is_error

    asyncio.run(scenario())


@pytest.mark.parametrize("args", [
    {"path": "/etc/passwd"}, {"path": "uploads/notes.txt"},
    {"path": "workspace/.tool-results/../secret.txt"},
    {"path": "workspace/.tool-results/missing.txt"},
    {"path": "workspace/.tool-results/a.txt", "offset": -1},
    {"path": "workspace/.tool-results/a.txt", "limit": True},
    {"path": "workspace/.tool-results/a.txt", "limit": 50_001},
])
def test_readback_rejects_foreign_paths_and_invalid_pagination(tmp_path, args):
    from app.tools.read_tool_result import ReadToolResultTool
    result = asyncio.run(ReadToolResultTool().execute(
        ToolCall("read", "read_tool_result", args), context_for(tmp_path),
    ))
    assert result.is_error and result.tool_call_id == "read"


def test_result_file_metadata_roundtrips_and_old_messages_default_to_none():
    message = Message(role="tool", content="preview", tool_call_id="call",
                      tool_result_file="/mnt/user-data/workspace/.tool-results/a.txt",
                      tool_result_chars=80_000)
    data = message.to_dict()
    assert Message.from_dict(data).to_dict() == data
    assert MessageResponse.model_validate(message).model_dump()["tool_result_file"] == message.tool_result_file
    data.pop("tool_result_file")
    data.pop("tool_result_chars")
    old = Message.from_dict(data)
    assert old.tool_result_file is None and old.tool_result_chars is None
