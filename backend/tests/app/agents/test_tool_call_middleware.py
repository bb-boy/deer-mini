"""Show that agent middleware surrounds a real tool call."""

import asyncio
import logging

from app.agents.middleware import AgentMiddleware, MiddlewareManager
from app.agents.tool_calls import execute_tool_calls
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


def test_tool_middlewares_wrap_execution_in_registration_order(tmp_path):
    steps: list[str] = []

    class EchoTool:
        definition = ToolDefinition("echo", "Return text", {})

        async def execute(self, call, context):
            steps.append("tool")
            return ToolResult(call.id, "echo", call.arguments["text"])

    class RecordingMiddleware(AgentMiddleware):
        def __init__(self, name):
            self.name = name

        async def wrap_tool_call(self, state, context, call, call_next):
            steps.append(f"{self.name}.before")
            result = await call_next()
            steps.append(f"{self.name}.after")
            return result

    async def scenario():
        async def record_event(event_type, payload):
            return None

        async def save_checkpoint(state):
            return None

        registry = ToolRegistry()
        registry.register(EchoTool())
        call = ToolCall("call-1", "echo", {"text": "hello"})
        state = ThreadState(thread_id="thread-1", user_id="user-1")
        context = RuntimeContext(
            "user-1", "thread-1", "run-1", str(tmp_path),
            record_event, save_checkpoint,
        )
        await execute_tool_calls(
            state=state, context=context, calls=[call], round_number=1,
            executor=ToolExecutor(registry),
            middleware=MiddlewareManager([
                RecordingMiddleware("outer"), RecordingMiddleware("inner"),
            ]),
        )
        assert state.messages[-1].content == "hello"

    asyncio.run(scenario())
    assert steps == [
        "outer.before", "inner.before", "tool", "inner.after", "outer.after",
    ]


def test_logging_middleware_records_before_and_after_without_arguments(tmp_path, caplog):
    from app.agents.tool_call_logging_middleware import ToolCallLoggingMiddleware

    steps: list[str] = []

    async def record_event(event_type, payload):
        return None

    async def save_checkpoint(state):
        return None

    async def call_next():
        steps.append("tool")
        return Message(role="tool", content="tool result", tool_call_id="call-2")

    state = ThreadState(thread_id="thread-1", user_id="user-1")
    context = RuntimeContext(
        "user-1", "thread-1", "run-1", str(tmp_path),
        record_event, save_checkpoint,
    )
    call = ToolCall("call-2", "echo", {"secret": "DO_NOT_LOG_ME"})

    async def scenario():
        return await ToolCallLoggingMiddleware().wrap_tool_call(
            state, context, call, call_next,
        )

    with caplog.at_level(logging.INFO):
        result = asyncio.run(scenario())

    assert result.content == "tool result"
    assert steps == ["tool"]
    assert "工具调用前 name=echo id=call-2" in caplog.text
    assert "工具调用后 name=echo id=call-2" in caplog.text
    assert "DO_NOT_LOG_ME" not in caplog.text
