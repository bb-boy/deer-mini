"""工具执行器传播异常，由 Agent 中间件决定是否转为工具消息。"""

import asyncio

import pytest

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


@pytest.mark.parametrize("critical", [False, True])
def test_tool_executor_preserves_original_errors(critical):
    error = StatePersistenceError("Checkpoint 未保存") if critical else ValueError("普通参数错误")

    class Tool:
        definition = ToolDefinition("test_tool", "test", {})

        async def execute(self, call, context):
            raise error

    async def unused(*args):
        raise AssertionError("工具执行器不直接操作数据库或日志")

    async def scenario():
        registry = ToolRegistry()
        registry.register(Tool())
        context = RuntimeContext("alice", "thread", "run", "/workspace", unused, unused)
        call = ToolCall("call", "test_tool", {})
        with pytest.raises(type(error)) as caught:
            await ToolExecutor(registry).execute(call, context)
        assert caught.value is error

    asyncio.run(scenario())
