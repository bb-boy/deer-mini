"""临时 SQLite 中验证 Coordinator → 主模型 → task → 子模型/工具 → 主模型。"""

import asyncio
import logging
from pathlib import Path
import sqlite3

import pytest

from app.domain.messages import Message, ToolCall
from app.infrastructure import database
from app.model.factory import ModelFactory
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository
from app.runtime.errors import StatePersistenceError
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_coordinator import RunCoordinator
from app.services.thread_service import ThreadService


@pytest.fixture
def thread(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "users"))
    database.initialize_database()
    return ThreadService().create_thread("task-user", "委派闭环验证")


def task_calls(count):
    return [ToolCall(f"parent-{n}", "task", {
        "description": f"读取文件 {n}", "prompt": f"{n}.txt", "subagent_type": "general-purpose",
    }) for n in range(count)]


class ParentModel:
    def __init__(self, count, expected=None):
        self.count, self.expected, self.calls, self.closed = count, expected, 0, 0

    async def chat(self, messages, tools, on_text_delta=None, **kwargs):
        self.calls += 1
        assert "task" in {item.name for item in tools}
        if self.calls == 1:
            return Message(role="assistant", content="", tool_calls=task_calls(self.count))
        replies = messages[-self.count:]
        assert [message.tool_call_id for message in replies] == [f"parent-{n}" for n in range(self.count)]
        if self.expected is not None:
            assert [message.content for message in replies] == self.expected
        await on_text_delta("已综合子任务结果")
        return Message(role="assistant", content="已综合子任务结果")

    async def close(self):
        self.closed += 1


@pytest.mark.parametrize("one_child_fails", [False, True])
def test_delegated_file_results_return_to_parent_and_persist(monkeypatch, thread, one_child_fails, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    for n in range(5):
        (Path(thread.workspace_path) / f"{n}.txt").write_text(f"真实文件 {n}")

    async def scenario():
        models = []
        expected = [f"真实文件 {n}" for n in range(5)]
        if one_child_fails:
            expected[0] = "Task failed: child provider failed"
        parent = ParentModel(5, expected)

        class Child:
            def __init__(self):
                self.calls, self.closed = 0, 0

            async def chat(self, messages, tools, on_text_delta=None, **kwargs):
                self.calls += 1
                assert [item.name for item in tools] == ["read_file", "read_tool_result"]
                filename = next(message.content for message in messages if message.role == "user")
                if filename == "0.txt" and one_child_fails:
                    raise RuntimeError("child provider failed")
                if self.calls == 1:
                    return Message(role="assistant", content="", tool_calls=[ToolCall("child-read", "read_file", {"path": filename})])
                await on_text_delta(messages[-1].content)
                return Message(role="assistant", content=messages[-1].content)

            async def close(self):
                self.closed += 1

        def factory(_factory):
            model = parent if not models else Child()
            models.append(model)
            return model

        monkeypatch.setattr(ModelFactory, "create_chat_model", factory)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, run_timeout_seconds=30)
        try:
            run = await coordinator.create_and_start_run(user_id=thread.user_id, thread_id=thread.id,
                message="分工读取五份资料", model_name="fake", thinking_enabled=False, reasoning_effort=None)
            worker = coordinator._tasks[run.id]
            async with asyncio.timeout(35):
                events = [event async for event in bridge.subscribe(run.id)]
                state = await worker
            restored = CheckpointRepository().latest(thread.id, thread.user_id).state
            assert restored.to_dict() == state.to_dict()
            assert len(models) == 6 and all(model.closed == 1 for model in models)
            assert len(state.subtasks) == 5
            assert sum(task.status == "failed" for task in state.subtasks.values()) == int(one_child_fails)
            assert all(task.status in {"completed", "failed"} for task in state.subtasks.values())
            assert RunRepository().get(run.id, thread.user_id).status == "success"
            assert ThreadRepository().get(thread.id, thread.user_id).status == "idle"
            event_types = [event.data.get("event_type") for event in events]
            assert "subagent.tool.start" in event_types and "subagent.text.delta" in event_types
            assert "run.end" in event_types and "run.error" not in event_types
            history = CheckpointRepository().history(thread.id, thread.user_id, run.id)
            assert [item.step for item in history] == list(range(1, len(history) + 1))
            for item in history:
                if any(message.role == "tool" for message in item.state.messages):
                    assert all(task.status in {"completed", "failed"} for task in item.state.subtasks.values())
        finally:
            await coordinator.shutdown()
            await bridge.close()

    asyncio.run(scenario())
    # 主模型两轮，成功的子模型各两轮，失败的子模型只请求一轮。
    expected_model_calls = 12 - int(one_child_fails)
    messages = [record.getMessage() for record in caplog.records]
    assert sum(message.startswith("模型调用前 ") for message in messages) == expected_model_calls
    assert sum(message.startswith("模型调用后 ") for message in messages) == expected_model_calls
    expected_tool_calls = 10 - int(one_child_fails)
    assert sum(message.startswith("工具调用前 ") for message in messages) == expected_tool_calls
    assert sum(message.startswith("工具调用后 ") for message in messages) == expected_tool_calls


def test_cancelled_batch_cleans_queued_tasks_and_next_run_has_fresh_allowance(monkeypatch, thread):
    async def scenario():
        first_parent, second_parent = ParentModel(5), ParentModel(2, ["0.txt", "1.txt"])
        phase, created = 1, []

        class Child:
            closed = 0

            async def chat(self, messages, tools, **kwargs):
                assert all(tool.name != "task" for tool in tools)
                if phase == 1:
                    await asyncio.Event().wait()
                return Message(role="assistant", content=next(item.content for item in messages if item.role == "user"))

            async def close(self):
                self.closed += 1

        def factory(_factory):
            if not created:
                model = first_parent
            elif phase == 2 and second_parent not in created:
                model = second_parent
            else:
                model = Child()
            created.append(model)
            return model

        monkeypatch.setattr(ModelFactory, "create_chat_model", factory)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, run_timeout_seconds=30)
        try:
            first = await coordinator.create_and_start_run(user_id=thread.user_id, thread_id=thread.id,
                message="开始五项工作", model_name="fake", thinking_enabled=False, reasoning_effort=None)
            first_worker = coordinator._tasks[first.id]
            states = {}
            async with asyncio.timeout(20):
                async for event in bridge.subscribe(first.id):
                    if event.data.get("event_type") == "subagent.status":
                        payload = event.data["payload"]
                        states[payload["task_id"]] = payload["status"]
                    if len(states) == 5 and sum(status == "running" for status in states.values()) == 3:
                        break
                cancelled = await coordinator.cancel_run(user_id=thread.user_id, thread_id=thread.id, run_id=first.id)
                with pytest.raises(asyncio.CancelledError):
                    await first_worker
            assert cancelled.status == "interrupted"
            saved = CheckpointRepository().latest(thread.id, thread.user_id).state
            assert len(saved.subtasks) == 5 and all(task.status == "cancelled" for task in saved.subtasks.values())
            assert len(created) == 4 and all(model.closed == 1 for model in created)
            assert [message.tool_call_id for message in saved.messages if message.role == "tool"] == [f"parent-{n}" for n in range(5)]
            assert ThreadRepository().get(thread.id, thread.user_id).status == "idle"
            phase = 2
            second = await coordinator.create_and_start_run(user_id=thread.user_id, thread_id=thread.id,
                message="继续两项新任务", model_name="fake", thinking_enabled=False, reasoning_effort=None)
            final = await coordinator._tasks[second.id]
            assert RunRepository().get(second.id, thread.user_id).status == "success"
            new_tasks = [task for task in final.subtasks.values() if task.run_id == second.id]
            assert len(new_tasks) == 2 and all(task.status == "completed" for task in new_tasks)
            assert len(final.subtasks) == 7  # 原先的五个取消记录仍然保留。
        finally:
            await coordinator.shutdown()
            await bridge.close()

    asyncio.run(scenario())


def test_checkpoint_failure_makes_real_run_error_and_ends_stream(monkeypatch, thread):
    original_save = CheckpointRepository.save
    original_error = sqlite3.OperationalError("test disk failure")
    failed = False

    def fail_once(repository, checkpoint):
        nonlocal failed
        if checkpoint.state.subtasks and not failed:
            failed = True
            raise original_error
        return original_save(repository, checkpoint)

    monkeypatch.setattr(CheckpointRepository, "save", fail_once)
    parent = ParentModel(1)
    monkeypatch.setattr(ModelFactory, "create_chat_model", lambda _factory: parent)

    async def scenario():
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, run_timeout_seconds=20)
        try:
            run = await coordinator.create_and_start_run(user_id=thread.user_id, thread_id=thread.id,
                message="触发状态故障", model_name="fake", thinking_enabled=False, reasoning_effort=None)
            worker = coordinator._tasks[run.id]
            with pytest.raises(StatePersistenceError) as caught:
                await worker
            assert caught.value.__cause__ is original_error
            assert parent.calls == 1 and parent.closed == 1
            assert RunRepository().get(run.id, thread.user_id).status == "error"
            assert ThreadRepository().get(thread.id, thread.user_id).status == "idle"
            events = [item async for item in bridge.subscribe(run.id)]
            ends = [event.data for event in events if event.data.get("event_type") in {"run.end", "run.error"}]
            assert len(ends) == 1 and ends[0]["event_type"] == "run.error"
            assert ends[0]["payload"]["status_confirmed"] is True
            saved = CheckpointRepository().latest(thread.id, thread.user_id).state
            assert saved.messages[-1].role == "tool" and saved.messages[-1].tool_call_id == "parent-0"
        finally:
            await coordinator.shutdown()
            await bridge.close()

    asyncio.run(scenario())
