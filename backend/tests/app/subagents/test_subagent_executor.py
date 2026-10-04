"""验证真实子 Agent 循环、并发状态合并，以及取消/持久化失败边界。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent
from app.domain.messages import Message, ToolCall
from app.domain.subagents import SubagentTask
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.runtime.agent_runtime import AgentRuntime
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services import thread_service
from app.services.run_service import RunService
from app.subagents.executor import SubagentExecutor
from app.tools.executor import ToolExecutor
from app.tools.read_file import ReadFileTool
from app.tools.registry import ToolRegistry


class RecordingRun:
    def __init__(self, root):
        workspace = root / "workspace"
        workspace.mkdir()
        self.state = ThreadState(
            user_id="alice", thread_id="parent-thread", workspace_path=str(workspace),
            messages=[Message(role="user", content="仅存在于父对话的背景")],
        )
        self.events = []
        self.checkpoints = []
        self.save_hook = None
        self.event_hook = None
        self.registry = ToolRegistry()
        self.registry.register(ReadFileTool())
        self.context = RuntimeContext(
            user_id="alice", thread_id="parent-thread", run_id="parent-run",
            workspace_path=str(workspace), record_event=self.record_event,
            save_checkpoint=self.save_checkpoint,
        )

    async def record_event(self, kind, payload):
        if self.event_hook:
            await self.event_hook(kind, payload)
        event = RunEvent("parent-run", "parent-thread", kind, deepcopy(payload))
        self.events.append(event)
        return event

    async def save_checkpoint(self, state):
        if self.save_hook:
            await self.save_hook(state)
        checkpoint = Checkpoint(
            thread_id=state.thread_id, run_id="parent-run", step=len(self.checkpoints) + 1,
            state=deepcopy(state),
        )
        self.checkpoints.append(checkpoint)
        return checkpoint

    def task(self, prompt="report.txt"):
        task = SubagentTask(
            tool_call_id=f"parent-call-{len(self.state.subtasks)}", user_id="alice",
            thread_id="parent-thread", run_id="parent-run", description=prompt, prompt=prompt,
        )
        self.state.subtasks[task.task_id] = task
        return task

    def executor(self, factory, **kwargs):
        return SubagentExecutor(
            parent_state=self.state, context=self.context, tool_registry=self.registry,
            model_factory=factory, **kwargs,
        )


class AnswerModel:
    def __init__(self, *, error=None, close_error=None):
        self.error = error
        self.close_error = close_error
        self.closed = 0
        self.calls = []

    async def chat(self, messages, tools, on_text_delta=None, on_reasoning_delta=None, **kwargs):
        self.calls.append(deepcopy(messages))
        if self.error:
            raise self.error
        answer = next(message.content for message in messages if message.role == "user")
        await on_reasoning_delta("检查任务输入")
        await on_text_delta(answer)
        return Message(role="assistant", content=answer, reasoning_content="检查任务输入")

    async def close(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error


class FileModel(AnswerModel):
    def __init__(self, tool_name="read_file", *, repeat=False):
        super().__init__()
        self.tool_name = tool_name
        self.repeat = repeat
        self.tool_names = []

    async def chat(self, messages, tools, on_text_delta=None, on_reasoning_delta=None, **kwargs):
        self.calls.append(deepcopy(messages))
        self.tool_names.append([tool.name for tool in tools])
        if len(self.calls) == 1 or self.repeat:
            await on_reasoning_delta("先读取任务指定的文件")
            filename = next(message.content for message in messages if message.role == "user")
            return Message(role="assistant", content="", tool_calls=[ToolCall(
                id=f"child-call-{len(self.calls)}", name=self.tool_name, arguments={"path": filename},
            )])
        assert messages[-1].role == "tool"
        assert messages[-1].tool_call_id == "child-call-1"
        await on_text_delta(messages[-1].content)
        return Message(role="assistant", content=messages[-1].content)


def test_real_tool_loop_preserves_parent_history_and_filters_tools(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        (Path(run.context.workspace_path) / "report.txt").write_text("真实文件里的数据 728")
        old = run.task("历史任务")
        old.run_id, old.status, old.result = "old-run", "completed", "旧结论"
        task = run.task()

        class ForbiddenTool:
            def __init__(self, name):
                self.definition = ToolDefinition(name, "不应对子 Agent 暴露", {})

            async def execute(self, call, context):
                raise AssertionError("子 Agent 不能执行委派或澄清工具")

        for name in ("task", "ask_clarification", "present_files"):
            run.registry.register(ForbiddenTool(name))
        model = FileModel()
        result = await run.executor(lambda: model).execute(task)
        assert result.content == task.result == "真实文件里的数据 728"
        assert not result.is_error and result.tool_call_id == task.tool_call_id
        assert task.status == "completed" and model.closed == 1
        assert model.tool_names == [["read_file"], ["read_file"]]
        assert run.registry.get("task") is not None
        assert [message.role for message in task.messages] == ["system", "user", "assistant", "tool", "assistant"]
        assert all("仅存在于父对话" not in message.content for call in model.calls for message in call)
        assert [message.content for message in run.state.messages] == ["仅存在于父对话的背景"]
        assert all(checkpoint.state.subtasks[old.task_id].result == "旧结论" for checkpoint in run.checkpoints)
        assert all(checkpoint.state.messages[0].id == run.state.messages[0].id for checkpoint in run.checkpoints)
        assert [checkpoint.step for checkpoint in run.checkpoints] == [1, 2, 3, 4, 5]
        for event in run.events:
            assert event.event_type.startswith("subagent.")
            assert event.payload["task_id"] == task.task_id
            assert event.payload["parent_tool_call_id"] == task.tool_call_id
        tools = [event for event in run.events if event.event_type == "subagent.tool.end"]
        assert tools[0].payload["tool_call_id"] == "child-call-1"
        saved_message = run.checkpoints[-1].state.subtasks[task.task_id].messages[-1]
        task.messages[-1].content = "只改内存"
        assert saved_message.content == "真实文件里的数据 728"

    asyncio.run(scenario())


def test_forbidden_tool_cannot_execute_even_if_model_requests_it(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        called = []

        class ForbiddenTool:
            definition = ToolDefinition("task", "nested delegation", {})

            async def execute(self, call, context):
                called.append(call)
                raise AssertionError("必须在注册表层禁止")

        run.registry.register(ForbiddenTool())
        task = run.task()
        model = FileModel("task")
        result = await run.executor(lambda: model).execute(task)
        assert "找不到工具 task" in result.content
        assert called == []

    asyncio.run(scenario())


def test_siblings_run_concurrently_with_independent_clients_and_no_lost_updates(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        tasks = [run.task("资料 A"), run.task("资料 B")]
        entered = []
        barrier = asyncio.Event()
        models = []

        class ConcurrentModel(AnswerModel):
            async def chat(self, *args, **kwargs):
                entered.append(self)
                if len(entered) == 2:
                    barrier.set()
                await barrier.wait()
                return await super().chat(*args, **kwargs)

        def factory():
            model = ConcurrentModel()
            models.append(model)
            return model

        executor = run.executor(factory)
        results = await asyncio.wait_for(asyncio.gather(*(executor.execute(task) for task in tasks)), 2)
        assert [result.content for result in results] == ["资料 A", "资料 B"]
        assert len(models) == 2 and models[0] is not models[1]
        assert all(model.closed == 1 for model in models)
        final = run.checkpoints[-1].state
        assert [final.subtasks[task.task_id].result for task in tasks] == ["资料 A", "资料 B"]
        assert any(all(cp.state.subtasks[task.task_id].status == "running" for task in tasks) for cp in run.checkpoints)
        for task in tasks:
            lengths = [len(cp.state.subtasks[task.task_id].messages) for cp in run.checkpoints]
            assert lengths == sorted(lengths)
        assert tasks[0].messages is not tasks[1].messages

    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("user_id", "bob"), ("thread_id", "other"), ("run_id", "old"), ("subagent_type", "unknown")])
def test_wrong_task_ownership_or_type_never_starts_model(tmp_path, field, value):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        setattr(task, field, value)
        model = AnswerModel()
        with pytest.raises(ValueError):
            await run.executor(lambda: model).execute(task)
        assert not model.calls and not run.checkpoints and task.status == "pending"

    asyncio.run(scenario())


def test_requires_registered_object_and_rejects_partially_filled_pending_task(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        executor = run.executor(AnswerModel)
        with pytest.raises(ValueError, match="已登记"):
            await executor.execute(replace(task))
        task.messages.append(Message(role="user", content="不可覆盖的记录"))
        with pytest.raises(ValueError, match="尚未执行"):
            await executor.execute(task)
        assert not run.checkpoints

    asyncio.run(scenario())


def test_completed_task_is_cached_without_rerunning_tools(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task("只执行一次")
        models = []

        def factory():
            model = AnswerModel()
            models.append(model)
            return model

        executor = run.executor(factory)
        first = await executor.execute(task)
        count = len(run.checkpoints)
        second = await executor.execute(task)
        assert first == second and len(models) == 1 and len(run.checkpoints) == count

    asyncio.run(scenario())


def test_duplicate_running_task_is_rejected_without_changing_original(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        entered, release = asyncio.Event(), asyncio.Event()

        class PausedModel(AnswerModel):
            async def chat(self, *args, **kwargs):
                entered.set()
                await release.wait()
                return await super().chat(*args, **kwargs)

        executor = run.executor(PausedModel)
        worker = asyncio.create_task(executor.execute(task))
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(RuntimeError, match="已经在运行"):
            await executor.execute(task)
        assert task.status == "running"
        release.set()
        assert not (await worker).is_error

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out"])
def test_failed_terminal_task_returns_saved_error_without_retry(tmp_path, status):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        task.status, task.error = status, "原有错误"
        result = await run.executor(lambda: pytest.fail("不能重启终态任务")).execute(task)
        assert result.is_error and "原有错误" in result.content
        assert not run.checkpoints

    asyncio.run(scenario())


@pytest.mark.parametrize("model_error", [ValueError("模型原始错误"), TimeoutError("提供商网络超时")])
def test_failure_keeps_original_error_even_when_close_fails(tmp_path, model_error, caplog):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        model = AnswerModel(error=model_error, close_error=OSError("关闭连接失败"))
        result = await run.executor(lambda: model).execute(task)
        assert result.is_error and task.status == "failed"
        assert task.error == str(model_error) and model.closed == 1
        assert run.checkpoints[-1].state.subtasks[task.task_id].error == str(model_error)

    asyncio.run(scenario())
    assert "保留 Agent 的原始异常" in caplog.text


def test_model_factory_failure_is_recorded(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()

        def factory():
            raise ValueError("模型配置缺失")

        result = await run.executor(factory).execute(task)
        assert result.is_error and task.status == "failed" and task.error == "模型配置缺失"

    asyncio.run(scenario())


def test_execution_deadline_records_timed_out_and_closes_client(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()

        class NeverReplies(AnswerModel):
            async def chat(self, **kwargs):
                await asyncio.Event().wait()

        model = NeverReplies()
        result = await asyncio.wait_for(run.executor(lambda: model, timeout_seconds=0.03).execute(task), 1)
        assert result.is_error and task.status == "timed_out" and model.closed == 1
        assert "0.03 seconds" in task.error
        assert run.checkpoints[-1].state.subtasks[task.task_id].status == "timed_out"

    asyncio.run(scenario())


def test_tool_round_limit_preserves_tool_results_and_marks_failure(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        (Path(run.context.workspace_path) / "report.txt").write_text("不会丢掉的工具结果")
        task = run.task()
        model = FileModel(repeat=True)
        result = await run.executor(lambda: model, max_tool_rounds=2).execute(task)
        assert result.is_error and task.status == "failed" and len(model.calls) == 2
        assert len([message for message in task.messages if message.role == "tool"]) == 2
        assert "2 轮" in task.error

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["start", "assistant", "completed"])
def test_checkpoint_failure_is_fatal_and_never_announces_completion(tmp_path, stage):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        db_error = sqlite3.OperationalError("模拟 Checkpoint 写入失败")

        async def fail_save(snapshot):
            candidate = snapshot.subtasks[task.task_id]
            is_target = (
                stage == "start" or (stage == "assistant" and len(candidate.messages) > 1)
                or (stage == "completed" and candidate.status == "completed")
            )
            if is_target:
                raise db_error

        run.save_hook = fail_save
        model = AnswerModel()
        with pytest.raises(StatePersistenceError) as caught:
            await run.executor(lambda: model).execute(task)
        assert caught.value.__cause__ is db_error
        assert task.status == ("pending" if stage == "start" else "running")
        assert model.closed == (0 if stage == "start" else 1)
        assert not any(event.payload.get("status") == "completed" for event in run.events)

    asyncio.run(scenario())


def test_failure_to_save_failure_preserves_both_original_and_database_error(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        original = ValueError("模型原始错误")
        db_error = OSError("收尾快照磁盘故障")

        async def fail_terminal(snapshot):
            if snapshot.subtasks[task.task_id].status == "failed":
                raise db_error

        run.save_hook = fail_terminal
        with pytest.raises(StatePersistenceError) as caught:
            await run.executor(lambda: AnswerModel(error=original)).execute(task)
        assert caught.value.__cause__ is db_error
        assert caught.value.execution_error is original
        assert task.status == "running"

    asyncio.run(scenario())


def test_cancel_during_model_and_repeated_cancel_during_save_are_durable(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        entered, saving_cancel, release_save = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class PausedModel(AnswerModel):
            async def chat(self, **kwargs):
                entered.set()
                await asyncio.Event().wait()

        async def hold_cancel_save(snapshot):
            if snapshot.subtasks[task.task_id].status == "cancelled":
                saving_cancel.set()
                await release_save.wait()

        run.save_hook = hold_cancel_save
        model = PausedModel()
        worker = asyncio.create_task(run.executor(lambda: model).execute(task))
        await asyncio.wait_for(entered.wait(), 1)
        worker.cancel("第一次取消")
        await asyncio.wait_for(saving_cancel.wait(), 1)
        worker.cancel("第二次取消")
        await asyncio.sleep(0)
        assert not worker.done()
        release_save.set()
        with pytest.raises(asyncio.CancelledError, match="第一次取消"):
            await worker
        assert model.closed == 1 and task.status == "cancelled"
        assert run.checkpoints[-1].state.subtasks[task.task_id].status == "cancelled"
        assert run.events[-1].payload["status"] == "cancelled"

    asyncio.run(scenario())


@pytest.mark.parametrize("held_status,expected_status", [("running", "cancelled"), ("completed", "completed")])
def test_cancel_during_checkpoint_waits_for_commit_and_keeps_confirmed_terminal(tmp_path, held_status, expected_status):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        saving, release = asyncio.Event(), asyncio.Event()
        held = False

        async def pause_save(snapshot):
            nonlocal held
            if not held and snapshot.subtasks[task.task_id].status == held_status:
                held = True
                saving.set()
                await release.wait()

        run.save_hook = pause_save
        worker = asyncio.create_task(run.executor(AnswerModel).execute(task))
        await asyncio.wait_for(saving.wait(), 1)
        worker.cancel("保存期间取消")
        await asyncio.sleep(0)
        assert not worker.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert task.status == expected_status
        assert run.checkpoints[-1].state.subtasks[task.task_id].status == expected_status

    asyncio.run(scenario())


def test_cancel_with_broken_checkpoint_is_fatal_not_successful_cancellation(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        entered = asyncio.Event()
        db_error = OSError("取消状态保存失败")

        class PausedModel(AnswerModel):
            async def chat(self, **kwargs):
                entered.set()
                await asyncio.Event().wait()

        async def fail_cancel(snapshot):
            if snapshot.subtasks[task.task_id].status == "cancelled":
                raise db_error

        run.save_hook = fail_cancel
        worker = asyncio.create_task(run.executor(PausedModel).execute(task))
        await asyncio.wait_for(entered.wait(), 1)
        worker.cancel("用户取消")
        with pytest.raises(StatePersistenceError) as caught:
            await worker
        assert caught.value.__cause__ is db_error
        assert isinstance(caught.value.execution_error, asyncio.CancelledError)
        assert task.status == "running"
        assert not any(event.payload.get("status") == "cancelled" for event in run.events)

    asyncio.run(scenario())


def test_status_notification_failure_does_not_change_committed_result(tmp_path, caplog):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()

        async def fail_status(kind, payload):
            if kind == "subagent.status":
                raise OSError("通知通道失败")

        run.event_hook = fail_status
        result = await run.executor(AnswerModel).execute(task)
        assert not result.is_error and task.status == "completed"
        assert run.checkpoints[-1].state.subtasks[task.task_id].status == "completed"

    asyncio.run(scenario())
    assert "状态已保存" in caplog.text


def test_cancellation_reaches_inflight_tool_and_retains_unfinished_call(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        task = run.task()
        entered, stopped = asyncio.Event(), asyncio.Event()

        class PausedTool:
            definition = ToolDefinition("paused_tool", "等待外部操作", {})

            async def execute(self, call, context):
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        run.registry.register(PausedTool())
        model = FileModel("paused_tool")
        worker = asyncio.create_task(run.executor(lambda: model).execute(task))
        await asyncio.wait_for(entered.wait(), 1)
        worker.cancel("取消工具执行")
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert stopped.is_set() and model.closed == 1 and task.status == "cancelled"
        # 保留原调用，并明确记录结果未确认；下次读取不会误以为工具已经成功。
        assert task.messages[-2].role == "assistant"
        assert task.messages[-2].tool_calls[0].name == "paused_tool"
        assert task.messages[-1].role == "tool"
        assert task.messages[-1].tool_call_id == task.messages[-2].tool_calls[0].id
        assert "返回结果尚未确认" in task.messages[-1].content

    asyncio.run(scenario())


def test_task_group_joins_sibling_cleanup_before_propagating_fatal_state_error(tmp_path):
    async def scenario():
        run = RecordingRun(tmp_path)
        first, second = run.task("first"), run.task("second")
        second_entered, second_stopped = asyncio.Event(), asyncio.Event()

        class CoordinatedModel(AnswerModel):
            async def chat(self, messages, **kwargs):
                prompt = next(message.content for message in messages if message.role == "user")
                if prompt == "first":
                    await second_entered.wait()
                    return await super().chat(messages=messages, **kwargs)
                try:
                    second_entered.set()
                    await asyncio.Event().wait()
                finally:
                    second_stopped.set()

        async def fail_first_completion(snapshot):
            if snapshot.subtasks[first.task_id].status == "completed":
                raise OSError("first 的最终快照失败")

        run.save_hook = fail_first_completion
        executor = run.executor(CoordinatedModel)
        with pytest.raises(ExceptionGroup) as caught:
            async with asyncio.TaskGroup() as group:
                group.create_task(executor.execute(first))
                group.create_task(executor.execute(second))
        assert any(isinstance(error, StatePersistenceError) for error in caught.value.exceptions)
        assert second_stopped.is_set() and second.status == "cancelled"
        assert run.checkpoints[-1].state.subtasks[second.task_id].status == "cancelled"

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_final_save", [False, True])
def test_runtime_and_tool_executor_preserve_child_state_and_confirm_real_run_result(tmp_path, monkeypatch, fail_final_save):
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()
    thread = thread_service.ThreadService().create_thread("alice", "executor integration")
    run = RunService().create_run("alice", thread.id, "test-model")
    (Path(thread.workspace_path) / "report.txt").write_text("真实 Runtime 的文件内容")

    class ParentAgent:
        async def run(self, state, context):
            task = SubagentTask(
                user_id=context.user_id, thread_id=context.thread_id, run_id=context.run_id,
                tool_call_id="parent-task-call", description="读取文件", prompt="report.txt",
            )
            state.subtasks[task.task_id] = task
            state.messages.append(Message(role="assistant", content="", tool_calls=[
                ToolCall(id=task.tool_call_id, name="task", arguments={}),
            ]))
            await context.save_checkpoint(state)
            registry = ToolRegistry()
            registry.register(ReadFileTool())
            executor = SubagentExecutor(
                parent_state=state, context=context, tool_registry=registry, model_factory=FileModel,
            )

            class TestDelegationTool:
                # 仅用于集成测试；生产工具表尚未注册 task。
                definition = ToolDefinition("task", "test delegation", {})

                async def execute(self, call, context):
                    return await executor.execute(task)

            registry.register(TestDelegationTool())
            message = await ToolExecutor(registry).execute(ToolCall(task.tool_call_id, "task", {}), context)
            state.messages.append(message)
            await context.save_checkpoint(state)
            state.messages.append(Message(role="assistant", content=message.content))
            return state

    class FaultyCheckpointRepository(CheckpointRepository):
        def save(self, checkpoint):
            if any(task.status == "completed" for task in checkpoint.state.subtasks.values()):
                raise sqlite3.OperationalError("关键子任务快照写入失败")
            return super().save(checkpoint)

    async def scenario():
        bridge = MemoryStreamBridge()
        runtime = AgentRuntime(bridge)
        if fail_final_save:
            runtime._checkpoint_repository = FaultyCheckpointRepository()
        try:
            if fail_final_save:
                with pytest.raises(StatePersistenceError):
                    await runtime.run("alice", thread.id, run.id, "读取资料", ParentAgent())
            else:
                await runtime.run("alice", thread.id, run.id, "读取资料", ParentAgent())
            restored = CheckpointRepository().latest(thread.id, "alice").state
            child = next(iter(restored.subtasks.values()))
            assert restored.messages[0].content == "读取资料"
            assert any(message.role == "tool" and message.content == "真实 Runtime 的文件内容" for message in child.messages)
            assert child.status == ("running" if fail_final_save else "completed")
            saved_run = RunService().get_run(run.id, "alice")
            assert saved_run.status == ("error" if fail_final_save else "success")
            stream = [event.data for event in [event async for event in bridge.subscribe(run.id)] if event.event == "run_event"]
            assert stream[-1]["event_type"] == ("run.error" if fail_final_save else "run.end")
            assert stream[-1]["payload"]["status_confirmed"] is True
        finally:
            await bridge.close()

    asyncio.run(scenario())
