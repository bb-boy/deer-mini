"""Exercise hook boundaries through the actual LeadAgent model/tool loop."""

import asyncio
from copy import deepcopy

import pytest

from app.agents.lead_agent import LeadAgent
from app.agents.middleware import AgentMiddleware
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


def make_run(tmp_path):
    events, snapshots = [], []

    async def record_event(kind, payload):
        events.append((kind, deepcopy(payload)))

    async def save_checkpoint(state):
        snapshots.append(deepcopy(state))

    state = ThreadState(thread_id="thread", user_id="alice",
                        messages=[Message(role="user", content="read")])
    context = RuntimeContext("alice", "thread", "run", str(tmp_path),
                             record_event, save_checkpoint)
    return state, context, events, snapshots


class RecordingMiddleware(AgentMiddleware):
    def __init__(self, name, steps):
        self.name, self.steps = name, steps

    def record(self, phase):
        self.steps.append(f"{self.name}.{phase}")

    async def before_agent(self, state, context):
        self.record("before_agent")

    async def before_model(self, state, context):
        self.record("before_model")

    async def prepare_model_messages(self, state, context, messages):
        self.record("prepare")
        return messages

    async def wrap_model_call(self, state, context, call_next):
        self.record("model.enter")
        try:
            return await call_next()
        finally:
            self.record("model.exit")

    async def after_model(self, state, context, message):
        assert state.messages[-1] is message
        self.record("after_model")

    async def wrap_tool_call(self, state, context, tool_call, call_next):
        self.record("tool.enter")
        try:
            return await call_next()
        finally:
            self.record("tool.exit")

    async def after_tool(self, state, context, tool_call, message):
        assert state.messages[-1] is message
        self.record("after_tool")

    async def after_agent(self, state, context, error):
        self.record("after_agent")


def test_real_agent_dispatches_all_hooks_in_each_model_tool_round(tmp_path):
    steps = []

    class EchoTool:
        definition = ToolDefinition("echo", "echo", {})

        async def execute(self, call, context):
            steps.append("tool")
            return ToolResult(call.id, call.name, "file content")

    class Model:
        calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            steps.append(f"model.{self.calls}")
            if self.calls == 1:
                await kwargs["on_text_delta"]("reading")
                return Message(role="assistant", content="", tool_calls=[
                    ToolCall("call-1", "echo", {}),
                ])
            assert kwargs["messages"][-1].content == "file content"
            return Message(role="assistant", content="done")

        async def close(self):
            steps.append("close")

    async def scenario():
        state, context, events, snapshots = make_run(tmp_path)
        registry = ToolRegistry()
        registry.register(EchoTool())
        agent = LeadAgent(Model(), registry, ToolExecutor(registry), middlewares=[
            RecordingMiddleware("A", steps), RecordingMiddleware("B", steps),
        ])
        result = await agent.run(state, context)
        assert result.messages[-1].content == "done"
        assert len(snapshots) == 3
        assert events[0][1]["message_id"] == result.messages[1].id

    asyncio.run(scenario())
    one_round = [
        "A.before_model", "B.before_model", "A.prepare", "B.prepare",
        "A.model.enter", "B.model.enter", "model.1",
        "B.model.exit", "A.model.exit", "B.after_model", "A.after_model",
    ]
    assert steps == [
        "A.before_agent", "B.before_agent", *one_round,
        "A.tool.enter", "B.tool.enter", "tool", "B.tool.exit", "A.tool.exit",
        "B.after_tool", "A.after_tool",
        *[step.replace("model.1", "model.2") for step in one_round],
        "B.after_agent", "A.after_agent", "close",
    ]


@pytest.mark.parametrize("cancel", [False, True])
def test_model_failure_or_cancellation_reaches_wrappers_and_cleanup(tmp_path, cancel):
    steps = []
    error = asyncio.CancelledError("cancel") if cancel else RuntimeError("provider failed")

    class Model:
        async def chat(self, **kwargs):
            steps.append("model")
            raise error

        async def close(self):
            steps.append("close")

    async def scenario():
        state, context, _, snapshots = make_run(tmp_path)
        registry = ToolRegistry()
        agent = LeadAgent(Model(), registry, ToolExecutor(registry), middlewares=[
            RecordingMiddleware("A", steps), RecordingMiddleware("B", steps),
        ])
        with pytest.raises(type(error)) as caught:
            await agent.run(state, context)
        assert caught.value is error
        assert len(state.messages) == 1 and not snapshots

    asyncio.run(scenario())
    assert steps[-7:] == [
        "B.model.enter", "model", "B.model.exit", "A.model.exit",
        "B.after_agent", "A.after_agent", "close",
    ]
    assert not any(step.endswith("after_model") for step in steps)


def test_model_wrapper_can_short_circuit_without_calling_provider(tmp_path):
    calls = []

    class Intercept(AgentMiddleware):
        async def wrap_model_call(self, state, context, call_next):
            return Message(role="assistant", content="cached answer")

    class Model:
        async def chat(self, **kwargs):
            calls.append("chat")
            return Message(role="assistant", content="provider answer")

        async def close(self):
            calls.append("close")

    async def scenario():
        state, context, _, _ = make_run(tmp_path)
        registry = ToolRegistry()
        result = await LeadAgent(Model(), registry, ToolExecutor(registry),
                                 middlewares=[Intercept()]).run(state, context)
        assert result.messages[-1].content == "cached answer"

    asyncio.run(scenario())
    assert calls == ["close"]
