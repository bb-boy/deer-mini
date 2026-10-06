"""用可控时钟与真实临时文件验证保存恢复，不调用模型。"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.threads import Thread
from app.memory.store import MemoryStore
from app.repositories.thread_repository import ThreadRepository


def setup_recovery(tmp_path):
    from app.memory.recovery import MemoryRecovery
    from app.repositories.memory_task_repository import MemoryTaskRepository
    now = [datetime(2026, 10, 5, tzinfo=timezone.utc)]
    store = MemoryStore(data_root=tmp_path / "users", clock=lambda: now[0])
    repo = MemoryTaskRepository(clock=lambda: now[0])
    ThreadRepository().create(Thread(id="thread", user_id="alice", workspace_path="/tmp/workspace"))
    worker = MemoryRecovery(store, repo, retry_base_seconds=1, retry_max_seconds=10)
    return store, repo, worker, now


def add(store, name="Preference"):
    return store.prepare("alice", [{"action": "add", "type": "user", "name": name,
        "description": name, "content": "likes " + name}], source_thread_id="thread", source_run_id="run")


def test_recovers_body_saved_before_result_registration(tmp_path, monkeypatch):
    store, repo, worker, now = setup_recovery(tmp_path)
    operation = add(store)[0]
    task = repo.register("alice", "thread", "run", [operation])
    original = repo.finish
    monkeypatch.setattr(repo, "finish", lambda *a, **k: (_ for _ in ()).throw(OSError("db failed")))
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task)["items"][0]["status"] == "running"
    saved = store.read("alice", operation.memory_id)
    monkeypatch.setattr(repo, "finish", original)
    now[0] += timedelta(hours=1)
    repo.recover_inflight()
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task)["status"] == "success"
    assert store.read("alice", operation.memory_id).updated_at == saved.updated_at
    assert len(store.list("alice")) == 1


def test_expired_pending_does_not_start_write_and_retry_has_new_window(tmp_path):
    store, repo, worker, now = setup_recovery(tmp_path)
    operation = add(store)[0]
    task = repo.register("alice", "thread", "run", [operation])
    now[0] += timedelta(hours=25)
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task)["items"][0]["status"] == "failed"
    assert store.read("alice", operation.memory_id) is None
    repo.retry("alice", task)
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task)["status"] == "success"


def test_backoff_does_not_block_new_task_or_repeat_success(tmp_path, monkeypatch):
    from app.storage.errors import StorageError
    store, repo, worker, now = setup_recovery(tmp_path)
    first = add(store, "first")[0]
    second = add(store, "second")[0]
    task1 = repo.register("alice", "thread", "run", [first, second])
    original = store.apply_operation
    calls = []
    def apply(user, operation, **kwargs):
        calls.append(operation.operation_id)
        if operation.operation_id == first.operation_id:
            raise StorageError(backend="file", operation="write", category="no_space", stage="write", commit_state="not_committed", recovery="wait")
        return original(user, operation, **kwargs)
    monkeypatch.setattr(store, "apply_operation", apply)
    asyncio.run(worker.run_ready())
    third = add(store, "third")[0]
    task2 = repo.register("alice", "thread", "run", [third])
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task2)["status"] == "success"
    assert calls.count(second.operation_id) == 1
    assert calls.count(first.operation_id) == 1
    assert repo.get("alice", task1)["status"] == "partial"
    assert repo.get("alice", task1)["items"][0]["status"] == "waiting"


def test_newer_write_conflicts_only_old_item(tmp_path):
    store, repo, worker, now = setup_recovery(tmp_path)
    record = store.upsert("alice", kind="user", name="n", description="d", content="old",
                          source_thread_id="thread", source_run_id="before")
    operation = store.prepare("alice", [{"action":"update", "memory_id":record.id,
        "type":"user", "name":"n", "description":"d", "content":"stale"}],
        source_thread_id="thread", source_run_id="run")[0]
    task = repo.register("alice", "thread", "run", [operation])
    now[0] += timedelta(minutes=1)
    store.upsert("alice", kind="user", name="n", description="d", content="newer",
                 source_thread_id="thread", source_run_id="later", memory_id=record.id)
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task)["items"][0]["status"] == "conflict"
    assert store.read("alice", record.id).content == "newer"


def test_cancel_waits_for_sync_commit_then_stops_future_items(tmp_path, monkeypatch):
    import threading
    store, repo, worker, _ = setup_recovery(tmp_path)
    operations = [*add(store, "first"), *add(store, "second")]
    task = repo.register("alice", "thread", "run", operations)
    started, release = threading.Event(), threading.Event()
    original = store.apply_operation
    def gated(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "apply_operation", gated)
    async def scenario():
        pending = asyncio.create_task(worker.run_ready())
        assert await asyncio.to_thread(started.wait, 3)
        repo.cancel("alice", task)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    asyncio.run(scenario())
    states = [i["status"] for i in repo.get("alice", task)["items"]]
    assert states == ["success", "cancelled"]
    assert len(store.list("alice")) == 1


def test_next_poll_recovers_ledger_failure_without_service_restart(tmp_path, monkeypatch):
    store, repo, worker, _ = setup_recovery(tmp_path)
    operation = add(store)[0]
    task = repo.register("alice", "thread", "run", [operation])
    original = repo.finish
    monkeypatch.setattr(repo, "finish", lambda *a, **k: (_ for _ in ()).throw(OSError("db failed")))
    asyncio.run(worker.run_ready())
    monkeypatch.setattr(repo, "finish", original)
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task)["status"] == "success"


def test_exhausted_unwritten_failure_is_failed_after_verification(tmp_path, monkeypatch):
    from app.storage.errors import StorageError
    store, repo, worker, now = setup_recovery(tmp_path)
    worker.max_attempts = 1
    operation = add(store)[0]
    task = repo.register("alice", "thread", "run", [operation])
    original = store.apply_operation
    def fail_write(*args, **kwargs):
        if kwargs["allow_write"]:
            raise StorageError(backend="file", operation="write", category="no_space", stage="write", recovery="wait")
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "apply_operation", fail_write)
    asyncio.run(worker.run_ready())
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task)["items"][0]["status"] == "failed"


def test_cancel_parked_verification_schedules_final_check(tmp_path):
    store, repo, worker, now = setup_recovery(tmp_path)
    operation = add(store)[0]
    task = repo.register("alice", "thread", "run", [operation])
    repo.claim("alice", task, operation.operation_id)
    repo.finish("alice", task, operation.operation_id, status="verifying", phase="verify")
    repo.cancel("alice", task)
    asyncio.run(worker.run_ready())
    item = repo.get("alice", task, include_content=True)["items"][0]
    assert item["status"] == "cancelled" and item["payload"] is None


def test_commit_finishing_after_deadline_gets_final_verification(tmp_path, monkeypatch):
    from app.storage.errors import StorageError
    store, repo, worker, now = setup_recovery(tmp_path)
    operation = add(store)[0]
    task = repo.register("alice", "thread", "run", [operation])
    now[0] += timedelta(hours=24, seconds=-1)
    original = store.apply_operation
    def finish_late(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["allow_write"]:
            now[0] += timedelta(seconds=2)
            raise StorageError(backend="file", operation="sync", category="io", stage="directory_sync",
                               commit_state="uncertain", recovery="verify")
        return result
    monkeypatch.setattr(store, "apply_operation", finish_late)
    asyncio.run(worker.run_ready())
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task)["status"] == "success"


def test_worker_cleans_only_resolved_history_after_thirty_days(tmp_path):
    store, repo, worker, now = setup_recovery(tmp_path)
    task = repo.register("alice", "thread", "run", add(store))
    asyncio.run(worker.run_ready())
    now[0] += timedelta(days=31)
    asyncio.run(worker.run_ready())
    assert repo.get("alice", task) is None
    assert len(store.snapshot("alice")) == 1
