"""Verify common middleware policies and fatal/ordinary error boundaries."""

import asyncio
import logging
from copy import deepcopy
from dataclasses import replace

import pytest

from app.agents.lead_agent import LeadAgent
from app.agents.middleware import AgentMiddleware, MiddlewareManager
from app.agents.tool_calls import repair_interrupted_tool_history
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


def make_context(tmp_path):
    async def unused(*args):
        return None

    return RuntimeContext("alice", "thread", "run", str(tmp_path), unused, unused)


@pytest.mark.parametrize("cancel", [False, True])
def test_model_logging_preserves_error_and_does_not_log_content(tmp_path, caplog, cancel):
    from app.agents.model_call_logging_middleware import ModelCallLoggingMiddleware

    error = asyncio.CancelledError("DO_NOT_LOG_ME") if cancel else ValueError("DO_NOT_LOG_ME")

    async def call_model():
        raise error

    async def scenario():
        with pytest.raises(type(error)) as caught:
            await ModelCallLoggingMiddleware().wrap_model_call(
                ThreadState("thread", "alice"), make_context(tmp_path), call_model,
            )
        assert caught.value is error

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "模型调用前" in caplog.text and "模型调用后" in caplog.text
    assert "DO_NOT_LOG_ME" not in caplog.text


def test_model_logging_returns_original_message(tmp_path, caplog):
    from app.agents.model_call_logging_middleware import ModelCallLoggingMiddleware

    message = Message(role="assistant", content="DO_NOT_LOG_ME")

    async def call_model():
        return message

    with caplog.at_level(logging.INFO):
        result = asyncio.run(ModelCallLoggingMiddleware().wrap_model_call(
            ThreadState("thread", "alice"), make_context(tmp_path), call_model,
        ))
    assert result is message
    assert "DO_NOT_LOG_ME" not in caplog.text


@pytest.mark.parametrize("error", [StatePersistenceError("checkpoint"), asyncio.CancelledError("cancel")])
def test_tool_error_policy_preserves_fatal_errors(tmp_path, error):
    from app.agents.tool_error_handling_middleware import ToolErrorHandlingMiddleware

    async def fail():
        raise error

    async def scenario():
        with pytest.raises(type(error)) as caught:
            await ToolErrorHandlingMiddleware().wrap_tool_call(
                ThreadState("thread", "alice"), make_context(tmp_path),
                ToolCall("call", "read_file", {}), fail,
            )
        assert caught.value is error

    asyncio.run(scenario())


def test_tool_error_policy_turns_ordinary_exception_into_correlated_message(tmp_path):
    from app.agents.tool_error_handling_middleware import ToolErrorHandlingMiddleware

    async def fail():
        raise ValueError("invalid path")

    result = asyncio.run(ToolErrorHandlingMiddleware().wrap_tool_call(
        ThreadState("thread", "alice"), make_context(tmp_path),
        ToolCall("call", "read_file", {}), fail,
    ))
    assert result.role == "tool" and result.tool_call_id == "call"
    assert result.is_error and "invalid path" in result.content


def test_default_stack_keeps_tool_error_in_model_loop(tmp_path):
    class BrokenTool:
        definition = ToolDefinition("broken", "broken", {})

        async def execute(self, call, context):
            raise ValueError("ordinary failure")

    class Model:
        calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Message(role="assistant", content="", tool_calls=[ToolCall("call", "broken", {})])
            result = kwargs["messages"][-1]
            assert result.role == "tool" and result.tool_call_id == "call"
            assert result.is_error and "ordinary failure" in result.content
            return Message(role="assistant", content="recovered")

        async def close(self):
            pass

    async def scenario():
        registry = ToolRegistry()
        registry.register(BrokenTool())
        state = ThreadState("thread", "alice", messages=[Message(role="user", content="test")])
        events, snapshots = [], []

        async def event(kind, payload):
            events.append((kind, payload))

        async def save(candidate):
            snapshots.append(deepcopy(candidate))

        context = replace(make_context(tmp_path), record_event=event, save_checkpoint=save)
        result = await LeadAgent(Model(), registry, ToolExecutor(registry)).run(state, context)
        assert result.messages[-1].content == "recovered"
        tool_end = next(payload for kind, payload in events if kind == "tool.end")
        assert tool_end["is_error"] is True and tool_end["tool_call_id"] == "call"
        assert snapshots[1].messages[-1].is_error is True

    asyncio.run(scenario())


def test_stack_builds_fresh_instances_and_keeps_extra_middleware_inside_logging(tmp_path):
    from app.agents.middleware_stack import build_runtime_middlewares

    steps = []

    class Extra(AgentMiddleware):
        async def wrap_tool_call(self, state, context, call, call_next):
            steps.append("extra")
            result = await call_next()
            assert result.is_error
            return result

    async def fail():
        steps.append("tool")
        raise ValueError("bad")

    first = build_runtime_middlewares([Extra()])
    second = build_runtime_middlewares()
    assert not {id(m) for m in first}.intersection(id(m) for m in second)
    result = asyncio.run(MiddlewareManager(first).wrap_tool_call(
        ThreadState("thread", "alice"), make_context(tmp_path),
        ToolCall("call", "echo", {}), fail,
    ))
    assert result.is_error and steps == ["extra", "tool"]


def test_repaired_interrupted_tool_message_is_marked_as_error():
    state = ThreadState("thread", "alice", messages=[
        Message(role="assistant", content="", tool_calls=[ToolCall("call", "read_file", {})]),
    ])
    assert repair_interrupted_tool_history(state)
    assert state.messages[-1].tool_call_id == "call" and state.messages[-1].is_error is True
