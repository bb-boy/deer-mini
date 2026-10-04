"""Tool failures remain identifiable across checkpoints and old histories."""

import asyncio

from app.api.schemas import MessageResponse

from app.domain.messages import Message, ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


def test_tool_error_flag_survives_serialization():
    message = Message(role="tool", content="failed", tool_call_id="call", is_error=True)
    assert Message.from_dict(message.to_dict()).is_error is True
    assert MessageResponse.model_validate(message).model_dump()["is_error"] is True


def test_old_message_without_error_flag_remains_readable():
    data = Message(role="assistant", content="old history").to_dict()
    data.pop("is_error", None)
    assert Message.from_dict(data).is_error is False
    assert MessageResponse.model_validate(data).is_error is False


def test_executor_preserves_tool_returned_error_flag():
    class Tool:
        definition = ToolDefinition("read_file", "read", {})

        async def execute(self, call, context):
            return ToolResult(call.id, call.name, "missing file", is_error=True)

    async def unused(*args):
        return None

    registry = ToolRegistry()
    registry.register(Tool())
    context = RuntimeContext("alice", "thread", "run", "/workspace", unused, unused)
    result = asyncio.run(ToolExecutor(registry).execute(ToolCall("call", "read_file", {}), context))
    assert result.is_error is True and result.tool_call_id == "call"
