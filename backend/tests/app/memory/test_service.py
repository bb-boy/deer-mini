"""后台任务的用户串行边界、快照和取消清理。"""

import asyncio

import pytest

from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.memory.service import MemoryService
from app.memory.store import MemoryStore


def state(user="alice"):
    return ThreadState(user_id=user, thread_id="thread", workspace_path="/tmp/workspace",
                       messages=[Message(role="user", content="原始用户消息")])


def test_same_user_serializes_other_user_progresses_and_snapshot_isolated(monkeypatch, tmp_path):
    async def scenario():
        first_started, release, other_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []

        async def extract(_extractor, snapshot, run_id):
            events.append((run_id, snapshot.messages[-1].content))
            if run_id == "first":
                first_started.set()
                await release.wait()
                raise ValueError("first failed")
            if run_id == "other":
                other_finished.set()

        monkeypatch.setattr("app.memory.service.MemoryExtractor.prepare", extract)
        service = MemoryService(store=MemoryStore(data_root=tmp_path), enabled=True)
        original = state()
        service.schedule(original, "first", lambda: None)
        original.messages[-1].content = "之后修改的消息"
        await first_started.wait()
        service.schedule(state(), "second", lambda: None)
        service.schedule(state("bob"), "other", lambda: None)
        await asyncio.wait_for(other_finished.wait(), 1)
        assert [event[0] for event in events] == ["first", "other"]
        assert events[0][1] == "原始用户消息"
        assert not await service.flush(timeout=0.01)
        release.set()
        assert await service.flush(timeout=1)
        assert [event[0] for event in events] == ["first", "other", "second"]
        await service.shutdown()
        assert not service._tasks and not service._tails

    asyncio.run(scenario())


def test_shutdown_cancels_active_and_queued_jobs_and_closes_model(tmp_path):
    async def scenario():
        entered = asyncio.Event()
        clients = []

        class Model:
            closed = 0

            async def chat(self, **kwargs):
                entered.set()
                await asyncio.Event().wait()

            async def close(self):
                self.closed += 1

        def factory():
            client = Model()
            clients.append(client)
            return client

        service = MemoryService(store=MemoryStore(data_root=tmp_path), enabled=True,
                                shutdown_timeout=0.01)
        service.schedule(state(), "first", factory)
        await asyncio.wait_for(entered.wait(), 1)
        service.schedule(state(), "second", factory)
        await asyncio.wait_for(service.shutdown(), 1)
        service.schedule(state(), "after-shutdown", factory)
        assert len(clients) == 1 and clients[0].closed == 1
        assert not service._tasks and not service._tails

    asyncio.run(scenario())


def test_disabled_service_never_schedules_or_constructs_model(tmp_path):
    async def scenario():
        def forbidden():
            raise AssertionError("disabled memory called model")
        service = MemoryService(store=MemoryStore(data_root=tmp_path), enabled=False)
        service.schedule(state(), "run", forbidden)
        assert await service.flush(timeout=1)
        assert not service._tasks
        await service.shutdown()

    asyncio.run(scenario())


def test_deleted_source_cannot_register_after_extraction(monkeypatch, tmp_path):
    from app.domain.threads import Thread
    from app.repositories.thread_repository import ThreadRepository
    from app.repositories.memory_task_repository import MemoryTaskRepository
    ThreadRepository().create(Thread(id="thread", user_id="alice", workspace_path="/tmp/workspace"))
    store = MemoryStore(data_root=tmp_path / "users")
    operations = store.prepare("alice", [{"action":"add", "type":"user", "name":"n", "description":"d", "content":"c"}],
                               source_thread_id="thread", source_run_id="run")
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def prepare(*args):
            entered.set()
            await release.wait()
            return operations
        monkeypatch.setattr("app.memory.service.MemoryExtractor.prepare", prepare)
        service = MemoryService(store=store, enabled=True)
        service.schedule(state(), "run", lambda: None)
        await entered.wait()
        ThreadRepository().delete("thread", "alice")
        release.set()
        assert await service.flush(timeout=2)
        assert MemoryTaskRepository().list_tasks("alice") == []
        assert store.snapshot("alice") == []
        await service.shutdown()
    asyncio.run(scenario())


def test_registration_failure_does_not_start_file_save(monkeypatch, tmp_path, caplog):
    from app.repositories.memory_task_repository import MemoryTaskRepository
    store = MemoryStore(data_root=tmp_path / "users")
    operations = store.prepare("alice", [{"action":"add", "type":"user", "name":"n", "description":"d", "content":"PRIVATE"}],
                               source_thread_id="thread", source_run_id="run")
    async def prepare(*args):
        return operations
    def fail(*args):
        raise OSError("PRIVATE")
    monkeypatch.setattr("app.memory.service.MemoryExtractor.prepare", prepare)
    monkeypatch.setattr(MemoryTaskRepository, "register", fail)
    async def scenario():
        service = MemoryService(store=store, enabled=True)
        service.schedule(state(), "run", lambda: None)
        assert await service.flush(timeout=2)
        assert store.snapshot("alice") == []
        assert "PRIVATE" not in caplog.text
        await service.shutdown()
    asyncio.run(scenario())


def test_start_restores_registered_tasks_without_model(tmp_path):
    from app.domain.threads import Thread
    from app.repositories.thread_repository import ThreadRepository
    from app.repositories.memory_task_repository import MemoryTaskRepository
    ThreadRepository().create(Thread(id="thread", user_id="alice", workspace_path="/tmp/workspace"))
    store = MemoryStore(data_root=tmp_path / "users")
    operations = store.prepare("alice", [{"action":"add", "type":"user", "name":"n", "description":"d", "content":"c"}],
                               source_thread_id="thread", source_run_id="run")
    repo = MemoryTaskRepository()
    task = repo.register("alice", "thread", "run", operations)
    async def scenario():
        service = MemoryService(store=store, repository=repo, enabled=True)
        await service.start()
        await service.recovery.run_ready()
        assert repo.get("alice", task)["status"] == "success"
        assert len(store.snapshot("alice")) == 1
        await service.shutdown()
    asyncio.run(scenario())


def test_stop_accepting_stops_future_registered_saves(tmp_path):
    from app.domain.threads import Thread
    from app.repositories.thread_repository import ThreadRepository
    ThreadRepository().create(Thread(id="thread", user_id="alice", workspace_path="/tmp/workspace"))
    store = MemoryStore(data_root=tmp_path / "users")
    operations = store.prepare("alice", [{"action":"add", "type":"user", "name":"n", "description":"d", "content":"c"}],
                               source_thread_id="thread", source_run_id="run")
    async def scenario():
        service = MemoryService(store=store, enabled=True)
        task = service.repository.register("alice", "thread", "run", operations)
        service.stop_accepting()
        await service.recovery.run_ready()
        assert service.repository.get("alice", task)["items"][0]["status"] == "pending"
        assert store.snapshot("alice") == []
        await service.shutdown()
    asyncio.run(scenario())


def test_cancelled_shutdown_still_closes_active_model(tmp_path):
    async def scenario():
        entered = asyncio.Event()
        class Model:
            closed = False
            async def chat(self, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            async def close(self):
                self.closed = True
        model = Model()
        service = MemoryService(store=MemoryStore(data_root=tmp_path), enabled=True, shutdown_timeout=30)
        service.schedule(state(), "run", lambda: model)
        await entered.wait()
        shutdown = asyncio.create_task(service.shutdown())
        await asyncio.sleep(0)
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert model.closed
        assert not service._tasks
    asyncio.run(scenario())
