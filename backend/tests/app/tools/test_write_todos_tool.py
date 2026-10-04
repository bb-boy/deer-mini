"""保存清单的真实边界：失败、取消、归属和同轮多次写入。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3

import pytest

from app.agents.middleware import MiddlewareManager
from app.agents.middleware_stack import build_runtime_middlewares
from app.agents.tool_calls import INTERRUPTED_RESULT, execute_tool_calls
from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.domain.todos import TodoItem
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.write_todos import WriteTodosTool


class TodoRun:
    def __init__(self):
        self.state = ThreadState(user_id="owner", thread_id="thread", workspace_path="/workspace")
        self.snapshots, self.events = [], []
        self.context = RuntimeContext("owner", "thread", "run", "/workspace", self.event, self.save)
        self.tool = WriteTodosTool()
        self.registry = ToolRegistry()
        self.registry.register(self.tool)

    async def save(self, state):
        self.snapshots.append(deepcopy(state))
        return Checkpoint(thread_id="thread", run_id="run", step=len(self.snapshots), state=deepcopy(state))

    async def event(self, kind, payload):
        event = RunEvent("run", "thread", kind, deepcopy(payload))
        self.events.append(event)
        return event

    def decide(self, todos, call_id="update"):
        call = ToolCall(call_id, "write_todos", {"todos": todos})
        self.state.messages.append(Message(role="assistant", content="", tool_calls=[call]))
        return call

    async def execute(self, call):
        return await MiddlewareManager(build_runtime_middlewares()).wrap_tool_call(
            self.state, self.context, call,
            lambda: ToolExecutor(self.registry).execute(call, self.context),
        )


def test_full_replacement_and_clear_preserve_earlier_snapshots():
    async def scenario():
        run = TodoRun()
        run.tool.bind(run.state, run.context)
        first = [{"content": "读取", "status": "in_progress"}, {"content": "总结", "status": "pending"}]
        call = run.decide(first, "first")
        result = await run.tool.execute(call, run.context)
        assert json.loads(result.content) == {"status": "saved", "run_id": "run", "tool_call_id": "first", "todos": first}
        assert result.tool_call_id == "first" and not result.is_error
        assert run.snapshots[0].messages[-1].tool_calls[0].id == "first"
        first[0]["content"] = "外部参数后来被修改"
        assert run.state.todos[0].content == "读取"
        second = run.decide([{"content": "读取", "status": "completed"}], "second")
        await run.tool.execute(second, run.context)
        assert len(run.state.todos) == 1 and run.state.todos[0].status == "completed"
        assert len(run.snapshots[0].todos) == 2 and run.snapshots[0].todos[0].status == "in_progress"
        cleared = await run.tool.execute(run.decide([], "clear"), run.context)
        assert run.state.todos == [] and run.state.todos_tool_call_id == "clear"
        assert json.loads(cleared.content)["todos"] == []
        assert [event.payload["tool_call_id"] for event in run.events] == ["first", "second", "clear"]
        assert all(event.payload["status_confirmed"] for event in run.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["user_id", "thread_id", "run_id", "workspace_path"])
def test_other_runtime_cannot_update_a_bound_plan(field):
    async def scenario():
        run = TodoRun()
        run.tool.bind(run.state, run.context)
        call = run.decide([])
        with pytest.raises(ValueError):
            await run.tool.execute(call, replace(run.context, **{field: "other"}))
        assert not run.snapshots and not run.events
        with pytest.raises(RuntimeError):
            run.tool.bind(run.state, run.context)

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [
    {}, {"todos": None}, {"todos": [] , "run_id": "chosen"},
    {"todos": [{"content": "a", "status": "broken"}]},
])
def test_invalid_arguments_are_tool_errors_without_saved_progress(arguments):
    async def scenario():
        run = TodoRun()
        run.tool.bind(run.state, run.context)
        call = run.decide([])
        call.arguments = arguments
        message = await run.execute(call)
        assert message.is_error and "出错" in message.content and message.tool_call_id == call.id
        assert not run.snapshots and not run.events

    asyncio.run(scenario())


def test_two_writes_in_the_same_model_response_both_fail_without_overwrite():
    async def scenario():
        run = TodoRun()
        run.tool.bind(run.state, run.context)
        calls = [ToolCall(str(i), "write_todos", {"todos": [{"content": str(i), "status": "pending"}]}) for i in range(2)]
        run.state.messages.append(Message(role="assistant", content="", tool_calls=calls))
        for call in calls:
            result = await run.execute(call)
            assert result.is_error and "每条模型回复只能调用一次" in result.content
        assert run.state.todos == [] and not run.snapshots and not run.events

    asyncio.run(scenario())


def test_save_failure_leaves_previous_plan_and_retains_original_error():
    async def scenario():
        run = TodoRun()
        run.state.todos = [TodoItem("原工作", "pending")]
        run.state.todos_run_id, run.state.todos_tool_call_id = "earlier", "old-call"
        original = sqlite3.OperationalError("disk failure")

        async def fail_save(candidate):
            assert candidate.todos[0].content == "新工作"
            assert run.state.todos[0].content == "原工作"
            raise original

        run.context = replace(run.context, save_checkpoint=fail_save)
        run.tool.bind(run.state, run.context)
        call = run.decide([{"content": "新工作", "status": "in_progress"}])
        with pytest.raises(StatePersistenceError) as caught:
            await ToolExecutor(run.registry).execute(call, run.context)
        assert caught.value.__cause__ is original
        assert run.state.todos[0].content == "原工作" and run.state.todos_run_id == "earlier"
        assert not run.events

    asyncio.run(scenario())


def test_notification_failure_does_not_undo_confirmed_plan(caplog):
    async def scenario():
        run = TodoRun()

        async def fail_event(kind, payload):
            assert run.snapshots and run.state.todos_tool_call_id == "update"
            raise OSError("stream unavailable")

        run.context = replace(run.context, record_event=fail_event)
        run.tool.bind(run.state, run.context)
        result = await run.tool.execute(run.decide([]), run.context)
        assert json.loads(result.content)["status"] == "saved"
        assert "清单已保存" in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_during_commit", [False, True])
def test_cancellation_waits_for_commit_and_repairs_tool_result(fail_during_commit):
    async def scenario():
        run = TodoRun()
        entered, release = asyncio.Event(), asyncio.Event()
        original = sqlite3.OperationalError("commit failed")
        first = True

        async def controlled_save(candidate):
            nonlocal first
            if first and candidate.todos_tool_call_id == "update":
                first = False
                assert run.state.todos == []  # 尚未确认时界面/父状态不能先变成成功。
                entered.set()
                await release.wait()
                if fail_during_commit:
                    raise original
            return await run.save(candidate)

        run.context = replace(run.context, save_checkpoint=controlled_save)
        run.tool.bind(run.state, run.context)
        call = run.decide([{"content": "资料", "status": "in_progress"}])
        task = asyncio.create_task(execute_tool_calls(
            state=run.state, context=run.context, calls=[call], round_number=1,
            executor=ToolExecutor(run.registry), middleware=MiddlewareManager(),
        ))
        async with asyncio.timeout(2):
            await entered.wait()
            task.cancel("user stop")
            await asyncio.sleep(0)
            task.cancel("second stop")
            assert not task.done()
            release.set()
            expected = StatePersistenceError if fail_during_commit else asyncio.CancelledError
            with pytest.raises(expected) as caught:
                await task
        message = run.state.messages[-1]
        assert message.role == "tool" and message.tool_call_id == "update"
        if fail_during_commit:
            assert caught.value.__cause__ is original
            assert run.state.todos == [] and message.content == INTERRUPTED_RESULT
        else:
            assert run.state.todos[0].content == "资料"
            assert json.loads(message.content)["status"] == "saved"
            assert json.loads(message.content)["tool_call_id"] == "update"
        assert run.snapshots[-1].to_dict() == run.state.to_dict()

    asyncio.run(scenario())
