"""临时 SQLite 中的规划、并发委派、恢复查询和失败收尾。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import router
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
def todo_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "todos.db"))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "users"))
    database.initialize_database()
    return ThreadService().create_thread("todo-user", "任务清单")


def plan_call(status="in_progress", call_id="plan"):
    return Message(role="assistant", content="", tool_calls=[ToolCall(call_id, "write_todos", {
        "todos": [{"content": "查阅资料", "status": status}],
    })])


async def start(coordinator, thread, message="列计划并完成工作"):
    return await coordinator.create_and_start_run(user_id=thread.user_id, thread_id=thread.id,
        message=message, model_name="test", thinking_enabled=False, reasoning_effort=None)


class OnePlanModel:
    def __init__(self):
        self.calls, self.closed = 0, 0

    async def chat(self, **kwargs):
        self.calls += 1
        assert self.calls == 1
        return plan_call()

    async def close(self):
        self.closed += 1


def test_plan_survives_parallel_child_checkpoints_and_api_history(monkeypatch, todo_thread):
    thread = todo_thread
    for number in range(2):
        (Path(thread.workspace_path) / f"{number}.txt").write_text(f"资料 {number}")

    async def scenario():
        models, requests = [], []
        entered = 0
        both_entered = asyncio.Event()

        class Parent:
            calls, closed = 0, 0

            async def chat(self, messages, **kwargs):
                self.calls += 1
                requests.append(deepcopy(messages))
                if self.calls == 1:
                    return plan_call()
                if self.calls == 2:
                    assert json.loads(messages[-1].content)["status"] == "saved"
                    return Message(role="assistant", content="", tool_calls=[ToolCall(f"task-{n}", "task", {
                        "description": f"查阅资料 {n}", "prompt": f"{n}.txt", "subagent_type": "general-purpose",
                    }) for n in range(2)])
                if self.calls == 3:
                    assert [message.content for message in messages[-2:]] == ["资料 0", "资料 1"]
                    return Message(role="assistant", content="已查阅两份资料。")
                if self.calls == 4:
                    assert "<system_reminder>" in messages[-1].content
                    return plan_call("completed", "finish-plan")
                assert self.calls == 5
                return Message(role="assistant", content="两份资料查阅完成，清单已更新。")

            async def close(self):
                self.closed += 1

        class Child:
            calls, closed = 0, 0

            async def chat(self, messages, tools, **kwargs):
                nonlocal entered
                self.calls += 1
                assert [tool.name for tool in tools] == ["read_file", "read_tool_result"]
                assert all("<todo_list_system>" not in message.content for message in messages)
                if self.calls == 1:
                    entered += 1
                    if entered == 2:
                        both_entered.set()
                    await both_entered.wait()  # 若调度退化为串行，这里会超时，测试失败。
                    filename = next(message.content for message in messages if message.role == "user")
                    return Message(role="assistant", content="", tool_calls=[ToolCall("read", "read_file", {"path": filename})])
                return Message(role="assistant", content=messages[-1].content)

            async def close(self):
                self.closed += 1

        def factory(_factory):
            model = Parent() if not models else Child()
            models.append(model)
            return model

        monkeypatch.setattr(ModelFactory, "create_chat_model", factory)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, run_timeout_seconds=30)
        try:
            run = await start(coordinator, thread)
            async with asyncio.timeout(35):
                state = await coordinator._tasks[run.id]
                events = [event async for event in bridge.subscribe(run.id)]
            assert len(models) == 3 and all(model.closed == 1 for model in models)
            assert state.todos[0].status == "completed"
            assert len(state.subtasks) == 2 and all(task.status == "completed" for task in state.subtasks.values())
            assert RunRepository().get(run.id, thread.user_id).status == "success"
            restored = CheckpointRepository().latest(thread.id, thread.user_id)
            assert restored.state.to_dict() == state.to_dict()
            history = CheckpointRepository().history(thread.id, thread.user_id, run.id)
            child_snapshots = [item for item in history if item.state.subtasks]
            assert child_snapshots and all(item.state.todos_run_id == run.id for item in child_snapshots)
            assert all(len(item.state.todos) == 1 for item in child_snapshots)
            assert [item.step for item in history] == list(range(1, len(history) + 1))
            assert all("<system_reminder>" not in message.content for item in history for message in item.state.messages)
            assert sum(event.data.get("event_type") == "todos.updated" for event in events) == 2
            assert any(event.data.get("event_type") == "run.end" for event in events)
            return run, state
        finally:
            await coordinator.shutdown()
            await bridge.close()

    run, state = asyncio.run(scenario())
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        latest_path = f"/api/threads/{thread.id}/state"
        latest = client.get(latest_path, params={"user_id": thread.user_id})
        history = client.get(f"/api/threads/{thread.id}/runs/{run.id}/checkpoints", params={"user_id": thread.user_id})
        assert latest.status_code == history.status_code == 200
        assert latest.json()["state"]["todos"] == [item.to_dict() for item in state.todos]
        assert latest.json()["state"]["todos_tool_call_id"] == "finish-plan"
        assert any(item["state"]["todos"] and item["state"]["todos"][0]["status"] == "in_progress" for item in history.json())
        assert client.get(latest_path, params={"user_id": "other-user"}).status_code == 404


def test_todo_checkpoint_failure_errors_run_and_closes_stream(monkeypatch, todo_thread):
    original_save = CheckpointRepository.save
    original_error = sqlite3.OperationalError("todo checkpoint unavailable")
    failed = False

    def fail_plan_once(repository, checkpoint):
        nonlocal failed
        if checkpoint.state.todos and not failed:
            failed = True
            raise original_error
        return original_save(repository, checkpoint)

    monkeypatch.setattr(CheckpointRepository, "save", fail_plan_once)
    model = OnePlanModel()
    monkeypatch.setattr(ModelFactory, "create_chat_model", lambda _factory: model)

    async def scenario():
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, run_timeout_seconds=20)
        try:
            run = await start(coordinator, todo_thread)
            with pytest.raises(StatePersistenceError) as caught:
                await coordinator._tasks[run.id]
            assert caught.value.__cause__ is original_error
            assert model.closed == model.calls == 1
            assert RunRepository().get(run.id, todo_thread.user_id).status == "error"
            assert ThreadRepository().get(todo_thread.id, todo_thread.user_id).status == "idle"
            events = [event async for event in bridge.subscribe(run.id)]
            assert not any(event.data.get("event_type") in {"todos.updated", "run.end"} for event in events)
            assert any(event.data.get("event_type") == "run.error" and event.data["payload"]["status_confirmed"] for event in events)
            restored = CheckpointRepository().latest(todo_thread.id, todo_thread.user_id).state
            assert restored.todos == []
            assert restored.messages[-1].role == "tool" and restored.messages[-1].tool_call_id == "plan"
        finally:
            await coordinator.shutdown()
            await bridge.close()

    asyncio.run(scenario())


def test_cancel_in_sqlite_commit_preserves_plan_and_next_run_can_answer(monkeypatch, todo_thread):
    original_save = CheckpointRepository.save
    release = threading.Event()
    blocked_once = False
    models = []

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def controlled_save(repository, checkpoint):
            nonlocal blocked_once
            if checkpoint.state.todos and not blocked_once:
                blocked_once = True
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise TimeoutError("test commit was not released")
            return original_save(repository, checkpoint)

        class AnswerModel:
            calls, closed = 0, 0

            async def chat(self, messages, **kwargs):
                self.calls += 1
                assert "<system_reminder>" not in messages[-1].content
                assert messages[-1].content == "新的简单问题"
                return Message(role="assistant", content="直接回答新问题")

            async def close(self):
                self.closed += 1

        def factory(_factory):
            model = OnePlanModel() if not models else AnswerModel()
            models.append(model)
            return model

        monkeypatch.setattr(CheckpointRepository, "save", controlled_save)
        monkeypatch.setattr(ModelFactory, "create_chat_model", factory)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, run_timeout_seconds=20)
        try:
            run = await start(coordinator, todo_thread)
            worker = coordinator._tasks[run.id]
            async with asyncio.timeout(10):
                await entered.wait()
                worker.cancel("user stop")
                await asyncio.sleep(0)
                assert not worker.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await worker
            assert RunRepository().get(run.id, todo_thread.user_id).status == "interrupted"
            restored = CheckpointRepository().latest(todo_thread.id, todo_thread.user_id).state
            assert restored.todos[0].status == "in_progress" and restored.todos_run_id == run.id
            assert json.loads(restored.messages[-1].content)["status"] == "saved"
            events = [event async for event in bridge.subscribe(run.id)]
            assert any(event.data.get("event_type") == "run.interrupted" for event in events)
            next_run = await start(coordinator, todo_thread, "新的简单问题")
            final = await coordinator._tasks[next_run.id]
            assert RunRepository().get(next_run.id, todo_thread.user_id).status == "success"
            assert final.todos_run_id == run.id and final.todos[0].status == "in_progress"
            assert final.messages[-1].content == "直接回答新问题"
            assert len(models) == 2 and all(model.closed == model.calls == 1 for model in models)
        finally:
            release.set()
            await coordinator.shutdown()
            await bridge.close()

    asyncio.run(scenario())
