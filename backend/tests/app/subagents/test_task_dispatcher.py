"""验证真实 Dispatcher/Executor 的额度、共享快照、排队取消与保存失败。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
import sqlite3

import pytest

from app.agents.middleware import MiddlewareManager
from app.agents.middleware_stack import build_runtime_middlewares

from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent
from app.domain.messages import Message, ToolCall
from app.domain.subagents import SubagentTask
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.subagents.dispatcher import SubagentDispatcher
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.task import TaskTool


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


class TestRun:
    __test__ = False

    def __init__(self, tmp_path, **options):
        self.state = ThreadState(user_id="owner", thread_id="thread", workspace_path=str(tmp_path),
                                 messages=[Message(role="user", content="父对话原始背景")])
        self.snapshots, self.events, self.models = [], [], []
        self.save_hook = None
        self.gate = asyncio.Event()
        self.gate.set()
        self.active = self.peak = 0
        self.context = RuntimeContext("owner", "thread", "run", str(tmp_path), self.event, self.save)
        self.registry = ToolRegistry()
        self.tool = TaskTool()
        self.registry.register(self.tool)
        self.dispatcher = SubagentDispatcher(parent_state=self.state, context=self.context,
            tool_registry=self.registry, model_factory=self.new_model, **options)
        self.tool.bind(self.dispatcher)

    async def event(self, kind, payload):
        event = RunEvent("run", "thread", kind, deepcopy(payload))
        self.events.append(event)
        return event

    async def save(self, state):
        snapshot = deepcopy(state)
        if self.save_hook:
            await self.save_hook(snapshot)
        # 人为让出一次执行机会，覆盖“读取旧状态后，另一协程更新状态”的竞争。
        await asyncio.sleep(0)
        self.snapshots.append(snapshot)
        return Checkpoint(thread_id="thread", run_id="run", step=len(self.snapshots), state=snapshot)

    def new_model(self):
        owner = self

        class Model:
            def __init__(self):
                self.closed = 0
                owner.active += 1
                owner.peak = max(owner.active, owner.peak)

            async def chat(self, messages, tools, **kwargs):
                assert not any(item.name == "task" for item in tools)
                assert all("父对话原始背景" not in message.content for message in messages)
                await owner.gate.wait()
                prompt = next(item.content for item in messages if item.role == "user")
                if prompt == "fail":
                    raise RuntimeError("child model failed")
                return Message(role="assistant", content=prompt)

            async def close(self):
                self.closed += 1
                owner.active -= 1

        model = Model()
        self.models.append(model)
        return model

    async def call(self, number, prompt=None, context=None):
        call = ToolCall(str(number), "task", {
            "description": f"task {number}", "prompt": prompt or f"result {number}",
            "subagent_type": "general-purpose",
        })
        execution_context = context or self.context
        return await MiddlewareManager(build_runtime_middlewares()).wrap_tool_call(
            self.state, execution_context, call,
            lambda: ToolExecutor(self.registry).execute(call, execution_context),
        )


def test_three_slots_six_total_and_all_calls_get_results(tmp_path):
    async def scenario():
        run = TestRun(tmp_path)
        run.gate.clear()
        # 历史 Run 的工作单不占当前额度，即使调用编号相同也不会被误认。
        old = SubagentTask("0", "owner", "thread", "old-run", "old", "old", status="completed", result="old")
        run.state.subtasks[old.task_id] = old
        workers = [asyncio.create_task(run.call(n)) for n in range(8)]
        await eventually(lambda: len(run.models) == 3 and len(run.state.subtasks) == 7)
        assert run.active == run.peak == 3
        assert sum(task.status == "pending" for task in run.state.subtasks.values()) == 3
        assert "maximum 6" in (await workers[6]).content
        assert "maximum 6" in (await workers[7]).content
        run.gate.set()
        results = await asyncio.gather(*workers)
        assert [item.tool_call_id for item in results] == list(map(str, range(8)))
        assert [item.content for item in results[:6]] == [f"result {n}" for n in range(6)]
        assert len(run.models) == 6 and all(model.closed == 1 for model in run.models)
        assert run.active == 0 and run.peak == 3
        assert all(task.status == "completed" for task in run.state.subtasks.values())
        previous = set()
        for snapshot in run.snapshots:
            assert previous <= set(snapshot.subtasks)
            previous = set(snapshot.subtasks)
            assert snapshot.messages[0].content == "父对话原始背景"
        assert run.snapshots[-1].to_dict() == run.state.to_dict()
        # 重复请求使用已保存结果，不再次计费、执行或占额度。
        assert (await run.call(0)).content == "result 0"
        assert len(run.models) == 6 and len(run.state.subtasks) == 7
        assert "另一项任务" in (await run.call(0, prompt="changed")).content

    asyncio.run(scenario())


def test_queued_cancellation_is_saved_without_creating_model_and_consumes_quota(tmp_path):
    async def scenario():
        run = TestRun(tmp_path, max_concurrent=1, max_total=2)
        run.gate.clear()
        first = asyncio.create_task(run.call(1))
        await eventually(lambda: len(run.models) == 1)
        queued = asyncio.create_task(run.call(2))
        await eventually(lambda: len(run.state.subtasks) == 2)
        queued.cancel("cancel queued")
        with pytest.raises(asyncio.CancelledError):
            await queued
        task = next(item for item in run.state.subtasks.values() if item.tool_call_id == "2")
        assert task.status == "cancelled" and not task.messages
        assert len(run.models) == 1
        assert "maximum 2" in (await run.call(3)).content
        run.gate.set()
        await first
        assert run.snapshots[-1].subtasks[task.task_id].status == "cancelled"
        assert any(event.payload.get("status") == "cancelled" for event in run.events)

    asyncio.run(scenario())


def test_cancellation_during_admission_finishes_commit_then_saves_cancelled(tmp_path):
    async def scenario():
        run = TestRun(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause_admission(snapshot):
            if not entered.is_set():
                entered.set()
                await release.wait()

        run.save_hook = pause_admission
        worker = asyncio.create_task(run.call(1))
        await entered.wait()
        worker.cancel("cancel while committing")
        await asyncio.sleep(0)
        assert not worker.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await worker
        task = next(iter(run.state.subtasks.values()))
        assert task.status == "cancelled" and not run.models
        assert [next(iter(item.subtasks.values())).status for item in run.snapshots] == ["pending", "cancelled"]

    asyncio.run(scenario())


def test_registration_waits_for_inflight_child_snapshot_without_losing_either_task(tmp_path):
    async def scenario():
        run = TestRun(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause_child(snapshot):
            child = next(iter(snapshot.subtasks.values()))
            if not entered.is_set() and any(message.role == "assistant" for message in child.messages):
                entered.set()
                await release.wait()

        run.save_hook = pause_child
        first = asyncio.create_task(run.call(1))
        await entered.wait()
        second = asyncio.create_task(run.call(2))
        await asyncio.sleep(0)
        release.set()
        assert [item.content for item in await asyncio.gather(first, second)] == ["result 1", "result 2"]
        assert len(run.snapshots[-1].subtasks) == 2
        assert all(task.status == "completed" for task in run.snapshots[-1].subtasks.values())
        counts = [len(item.subtasks) for item in run.snapshots]
        assert counts == sorted(counts)

    asyncio.run(scenario())


def test_admission_save_error_propagates_through_generic_tool_executor(tmp_path):
    async def scenario():
        run = TestRun(tmp_path)
        original = sqlite3.OperationalError("disk unavailable")

        async def fail_save(_state):
            raise original

        run.save_hook = fail_save
        with pytest.raises(StatePersistenceError) as caught:
            await run.call(1)
        assert caught.value.__cause__ is original
        assert not run.models and not run.state.subtasks

    asyncio.run(scenario())


def test_failed_child_returns_error_and_other_child_completes(tmp_path):
    async def scenario():
        run = TestRun(tmp_path)
        results = await asyncio.gather(run.call(1, "fail"), run.call(2))
        assert "Task failed: child model failed" == results[0].content
        assert results[1].content == "result 2"
        assert {task.status for task in run.state.subtasks.values()} == {"failed", "completed"}
        assert all(model.closed == 1 for model in run.models)

    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("user_id", "other"), ("thread_id", "other"), ("run_id", "other"), ("workspace_path", "/different")])
def test_bound_dispatcher_rejects_foreign_context_without_consuming_quota(tmp_path, field, value):
    async def scenario():
        run = TestRun(tmp_path)
        reply = await run.call(1, context=replace(run.context, **{field: value}))
        assert "不属于" in reply.content and not run.models and not run.state.subtasks

    asyncio.run(scenario())
