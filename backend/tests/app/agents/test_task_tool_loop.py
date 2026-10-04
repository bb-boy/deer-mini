"""验证 task 批次的并发、顺序屏障和中断后的工具协议完整性。"""

import asyncio
from copy import deepcopy

import pytest

from app.agents.lead_agent import LeadAgent
from app.agents.middleware import AgentMiddleware, MiddlewareManager
from app.agents.tool_calls import INTERRUPTED_RESULT, execute_tool_calls, repair_interrupted_tool_history
from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent
from app.domain.messages import Message, ToolCall
from app.domain.subagents import SubagentTask
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


class Loop:
    def __init__(self, calls):
        self.calls, self.events, self.snapshots = calls, [], []
        self.state = ThreadState(user_id="owner", thread_id="thread", messages=[
            Message(role="user", content="run"), Message(role="assistant", content="", tool_calls=calls),
        ])
        self.registry = ToolRegistry()
        self.context = RuntimeContext("owner", "thread", "run", "/tmp/workspace", self.event, self.save)

    async def event(self, kind, payload):
        event = RunEvent("run", "thread", kind, deepcopy(payload))
        self.events.append(event)
        return event

    async def save(self, state):
        self.snapshots.append(deepcopy(state))
        return Checkpoint(thread_id="thread", run_id="run", step=len(self.snapshots), state=deepcopy(state))

    async def run(self, middleware=None):
        await execute_tool_calls(state=self.state, context=self.context, calls=self.calls, round_number=1,
                                 executor=ToolExecutor(self.registry), middleware=MiddlewareManager(middleware))


def test_task_batches_overlap_and_ordinary_tools_wait_for_the_whole_batch():
    async def scenario():
        calls = [ToolCall(name + str(i), name, {}) for i, name in enumerate([
            "ordinary", "task", "task", "ordinary", "task", "task", "ordinary",
        ])]
        loop = Loop(calls)
        active = 0
        trace = []
        gates = [asyncio.Event(), asyncio.Event()]
        started = [0, 0]

        class Task:
            definition = ToolDefinition("task", "test", {})

            async def execute(self, call, context):
                nonlocal active
                batch = 0 if call.id in {"task1", "task2"} else 1
                active += 1
                started[batch] += 1
                if started[batch] == 2:
                    gates[batch].set()
                await gates[batch].wait()
                if call.id in {"task1", "task4"}:
                    await asyncio.sleep(0.01)
                active -= 1
                trace.append(call.id)
                return ToolResult(call.id, "task", call.id)

        class Ordinary:
            definition = ToolDefinition("ordinary", "test", {})

            async def execute(self, call, context):
                assert active == 0
                trace.append(call.id)
                return ToolResult(call.id, "ordinary", call.id)

        class CheckAfterTool(AgentMiddleware):
            async def after_tool(self, state, context, tool_call, tool_message):
                assert active == 0

        loop.registry.register(Task())
        loop.registry.register(Ordinary())
        async with asyncio.timeout(2):
            await loop.run([CheckAfterTool()])
        assert trace == ["ordinary0", "task2", "task1", "ordinary3", "task5", "task4", "ordinary6"]
        assert [message.tool_call_id for message in loop.state.messages[2:]] == [call.id for call in calls]
        assert len(loop.snapshots) == 7

    asyncio.run(scenario())


def test_critical_failure_waits_for_sibling_cleanup_and_preserves_all_call_replies():
    async def scenario():
        calls = [ToolCall(name, "task" if name != "later" else "ordinary", {}) for name in ("fast", "broken", "slow", "later")]
        loop = Loop(calls)
        slow_started, slow_stopped, fast_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = StatePersistenceError("checkpoint disk failed")

        class Task:
            definition = ToolDefinition("task", "test", {})

            async def execute(self, call, context):
                if call.id == "fast":
                    fast_done.set()
                    return ToolResult(call.id, "task", "already confirmed")
                if call.id == "broken":
                    await slow_started.wait()
                    await fast_done.wait()
                    raise original
                try:
                    slow_started.set()
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.01)
                    slow_stopped.set()

        class Ordinary:
            definition = ToolDefinition("ordinary", "test", {})

            async def execute(self, call, context):
                raise AssertionError("不能开始后续有副作用的工具")

        loop.registry.register(Task())
        loop.registry.register(Ordinary())
        with pytest.raises(StatePersistenceError) as caught:
            await loop.run()
        assert caught.value is original and slow_stopped.is_set()
        replies = loop.state.messages[2:]
        assert [message.tool_call_id for message in replies] == [call.id for call in calls]
        assert replies[0].content == "already confirmed"
        assert replies[1].content == replies[2].content == INTERRUPTED_RESULT
        assert "尚未执行" in replies[3].content
        assert loop.snapshots[-1].to_dict() == loop.state.to_dict()
        assert sum(event.event_type == "tool.end" for event in loop.events) == 4

    asyncio.run(scenario())


def test_parent_cancellation_joins_tools_and_retains_confirmed_subtask_result():
    async def scenario():
        calls = [ToolCall("a", "task", {}), ToolCall("b", "task", {})]
        loop = Loop(calls)
        entered = asyncio.Event()
        stopped = []
        confirmed = SubagentTask("a", "owner", "thread", "run", "task", "task", status="completed", result="saved child answer")
        loop.state.subtasks[confirmed.task_id] = confirmed

        class Task:
            definition = ToolDefinition("task", "test", {})

            async def execute(self, call, context):
                try:
                    if call.id == "b":
                        entered.set()
                    await asyncio.Event().wait()
                finally:
                    stopped.append(call.id)

        loop.registry.register(Task())
        worker = asyncio.create_task(loop.run())
        await entered.wait()
        worker.cancel("user cancelled")
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert set(stopped) == {"a", "b"}
        assert [message.content for message in loop.state.messages[2:]] == ["saved child answer", INTERRUPTED_RESULT]
        assert loop.snapshots[-1].to_dict() == loop.state.to_dict()

    asyncio.run(scenario())


def test_historical_missing_replies_are_inserted_before_new_user_without_guessing_old_results():
    calls = [ToolCall("duplicate", "task", {}), ToolCall("known", "read_file", {})]
    state = ThreadState(user_id="owner", thread_id="thread", messages=[
        Message(role="assistant", content="", tool_calls=calls),
        Message(role="tool", content="known content", tool_call_id="known"),
        Message(role="user", content="下一轮继续"),
    ])
    old = SubagentTask("duplicate", "owner", "thread", "unrelated-old-run", "old", "old", status="completed", result="wrong historical result")
    state.subtasks[old.task_id] = old
    assert repair_interrupted_tool_history(state)
    assert [message.role for message in state.messages] == ["assistant", "tool", "tool", "user"]
    assert state.messages[1].content == "known content"
    assert state.messages[2].content == INTERRUPTED_RESULT and state.messages[2].tool_call_id == "duplicate"
    assert not repair_interrupted_tool_history(state)


def test_lead_agent_saves_repaired_history_before_requesting_the_model():
    async def scenario():
        loop = Loop([ToolCall("lost", "task", {})])
        loop.state.messages.append(Message(role="user", content="继续"))

        class Model:
            closed = False

            async def chat(self, messages, **kwargs):
                assert messages[-2].role == "tool" and messages[-2].tool_call_id == "lost"
                assert messages[-1].role == "user"
                assert loop.snapshots[0].messages[-2].role == "tool"
                return Message(role="assistant", content="已继续")

            async def close(self):
                self.closed = True

        model = Model()
        await LeadAgent(model, loop.registry, ToolExecutor(loop.registry)).run(loop.state, loop.context)
        assert model.closed and loop.state.messages[-1].content == "已继续"

    asyncio.run(scenario())
