"""使用临时真实 SQLite 验证记忆与主 Run 的成功、失败和取消边界。"""

import asyncio
import json
from copy import deepcopy

import pytest

from app.domain.messages import Message
from app.infrastructure import database
from app.model.factory import ModelFactory
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.run_repository import RunRepository
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_coordinator import RunCoordinator
from app.services.thread_service import ThreadService


@pytest.fixture
def memory_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "users"))
    monkeypatch.setenv("DEER_MINI_MEMORY_ENABLED", "true")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    database.initialize_database()
    return ThreadService().create_thread("alice", "记忆测试")


async def start(coordinator, thread, message="解释设计时先给结论，再补充理由"):
    return await coordinator.create_and_start_run(
        user_id=thread.user_id, thread_id=thread.id, message=message,
        model_name="fake", thinking_enabled=False, reasoning_effort=None,
    )


def change(evidence="解释设计时先给结论，再补充理由"):
    return {"action": "add", "type": "feedback", "name": "解释风格",
            "description": "解释设计时先给结论", "content":
            "先给结论，再补充理由。\n\n**Why:** 用户明确要求。\n**How to apply:** 解释设计时。",
            "evidence": evidence}


def test_success_extracts_after_persist_and_new_thread_recalls(monkeypatch, memory_thread):
    from app.memory.service import MemoryService
    from app.memory.store import MemoryStore

    async def scenario():
        store, events, clients = MemoryStore(), [], []
        service = MemoryService(store=store, enabled=True)
        run_identity = {}

        class Model:
            def __init__(self):
                self.closed = 0

            async def chat(self, messages, tools, **kwargs):
                snapshot = deepcopy(messages)
                events.append(snapshot)
                system = messages[0].content
                if "memory_extraction" in system:
                    saved = RunRepository().get(run_identity["id"], "alice")
                    assert saved.status == "success"
                    assert CheckpointRepository().latest(memory_thread.id, "alice").state.messages[-1].content == "回答完成"
                    assert tools == []
                    return Message(role="assistant", content=json.dumps({"changes": [change()]}, ensure_ascii=False))
                if "memory_selection" in system:
                    assert tools == []
                    return Message(role="assistant", content='{"memory_indices":[1]}')
                return Message(role="assistant", content="回答完成")

            async def close(self):
                self.closed += 1

        def factory(_factory):
            model = Model()
            clients.append(model)
            return model

        monkeypatch.setattr(ModelFactory, "create_chat_model", factory)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, memory_service=service)
        try:
            run = await start(coordinator, memory_thread)
            run_identity["id"] = run.id
            await coordinator._tasks[run.id]
            await asyncio.sleep(0)
            assert await service.flush(timeout=3)
            memories = store.list("alice")
            assert len(memories) == 1 and memories[0].type == "feedback"
            from app.repositories.memory_task_repository import MemoryTaskRepository
            saved_tasks = MemoryTaskRepository().list_tasks("alice")
            assert len(saved_tasks) == 1 and saved_tasks[0]["status"] == "success"
            assert "memory_extraction" not in events[0][0].content
            second = ThreadService().create_thread("alice", "新对话")
            next_run = await start(coordinator, second, "帮我解释一个设计")
            # 后续抽取没有新的用户偏好依据，不写入。
            run_identity["id"] = next_run.id
            await coordinator._tasks[next_run.id]
            main = next(request for request in reversed(events)
                        if "memory_selection" not in request[0].content
                        and "memory_extraction" not in request[0].content)
            assert any("**Why:**" in item.content for item in main)
            persisted = CheckpointRepository().latest(second.id, "alice").state
            assert not any("<selected_memories>" in item.content for item in persisted.messages)
        finally:
            await coordinator.shutdown()
            await bridge.close()
        assert all(model.closed == 1 for model in clients)

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["error", "cancel", "checkpoint"])
def test_unsuccessful_run_never_extracts(monkeypatch, memory_thread, outcome):
    from app.memory.service import MemoryService
    from app.memory.store import MemoryStore

    async def scenario():
        entered = asyncio.Event()
        created = []

        class Model:
            async def chat(self, **kwargs):
                entered.set()
                if outcome == "cancel":
                    await asyncio.Event().wait()
                if outcome == "error":
                    raise ValueError("model failure")
                return Message(role="assistant", content="回答完成")

            async def close(self):
                pass

        def factory(_factory):
            created.append(Model())
            return created[-1]

        monkeypatch.setattr(ModelFactory, "create_chat_model", factory)
        if outcome == "checkpoint":
            original = CheckpointRepository.save

            def fail_final(repository, checkpoint):
                if any(item.role == "assistant" for item in checkpoint.state.messages):
                    raise OSError("checkpoint failure")
                return original(repository, checkpoint)

            monkeypatch.setattr(CheckpointRepository, "save", fail_final)
        service = MemoryService(store=MemoryStore(), enabled=True)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, memory_service=service)
        try:
            run = await start(coordinator, memory_thread)
            worker = coordinator._tasks[run.id]
            if outcome == "cancel":
                await entered.wait()
                worker.cancel()
            with pytest.raises(asyncio.CancelledError if outcome == "cancel" else Exception):
                await worker
            await asyncio.sleep(0)
            assert await service.flush(timeout=1)
            assert len(created) == 1
            assert service.store.list("alice") == []
            assert RunRepository().get(run.id, "alice").status != "success"
        finally:
            await coordinator.shutdown()
            await bridge.close()

    asyncio.run(scenario())


def test_extraction_failure_preserves_success(monkeypatch, memory_thread, caplog):
    from app.memory.service import MemoryService

    async def scenario():
        class Model:
            async def chat(self, messages, **kwargs):
                if "memory_extraction" in messages[0].content:
                    raise ValueError("DO_NOT_LOG_MEMORY_BODY")
                return Message(role="assistant", content="回答完成")

            async def close(self):
                pass

        monkeypatch.setattr(ModelFactory, "create_chat_model", lambda _: Model())
        service = MemoryService(enabled=True)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, memory_service=service)
        try:
            run = await start(coordinator, memory_thread)
            await coordinator._tasks[run.id]
            await asyncio.sleep(0)
            assert await service.flush(timeout=3)
            assert RunRepository().get(run.id, "alice").status == "success"
            assert "DO_NOT_LOG_MEMORY_BODY" not in caplog.text
        finally:
            await coordinator.shutdown()
            await bridge.close()

    asyncio.run(scenario())


def test_outer_run_timeout_during_selection_closes_clients_without_extracting(monkeypatch, memory_thread):
    from app.memory.service import MemoryService
    from app.memory.store import MemoryStore

    async def scenario():
        store = MemoryStore()
        store.upsert("alice", kind="user", name="技术背景", description="熟悉Python",
                     content="用户熟悉Python", source_thread_id="old", source_run_id="old")
        clients, selected = [], asyncio.Event()

        class Model:
            closed = 0

            async def chat(self, messages, **kwargs):
                assert "memory_selection" in messages[0].content
                selected.set()
                await asyncio.Event().wait()

            async def close(self):
                self.closed += 1

        def factory(_factory):
            model = Model()
            clients.append(model)
            return model

        monkeypatch.setattr(ModelFactory, "create_chat_model", factory)
        service = MemoryService(store=store, enabled=True)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None, memory_service=service,
                                     run_timeout_seconds=0.15)
        try:
            run = await start(coordinator, memory_thread)
            worker = coordinator._tasks[run.id]
            with pytest.raises(TimeoutError):
                await worker
            await asyncio.sleep(0)
            assert selected.is_set()
            assert RunRepository().get(run.id, "alice").status == "timeout"
            assert await service.flush(timeout=1)
            assert len(clients) == 2 and all(model.closed == 1 for model in clients)
            assert len(store.list("alice")) == 1
        finally:
            await coordinator.shutdown()
            await bridge.close()

    asyncio.run(scenario())
