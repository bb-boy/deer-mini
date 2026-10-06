"""并发恢复必须等待所有在途提交结束，再释放扫描锁或传播异常。"""

import asyncio
import threading
from datetime import datetime, timezone

import pytest

from app.memory.recovery import MemoryRecovery
from app.runtime.errors import StatePersistenceError
from app.storage.errors import StorageError


class _ReadyUsers:
    def now(self):
        return datetime.now(timezone.utc)

    def cleanup_expired_history(self):
        pass

    def recover_inflight(self):
        pass

    def due(self):
        return [{"user_id": "a"}, {"user_id": "b"}]


@pytest.mark.parametrize("cancel", [False, True])
def test_multi_user_recovery_holds_lock_until_every_inflight_commit_finishes(cancel):
    started = {user: threading.Event() for user in "ab"}
    release = {user: threading.Event() for user in "ab"}
    returned = {user: threading.Event() for user in "ab"}
    worker = MemoryRecovery(None, _ReadyUsers())
    failure = StatePersistenceError("critical")

    def attempt(item):
        user = item["user_id"]
        started[user].set()
        assert release[user].wait(5)
        returned[user].set()
        if user == "a" and not cancel:
            raise failure
    worker._attempt = attempt

    async def scenario():
        pending = asyncio.create_task(worker.run_ready())
        try:
            for event in started.values():
                assert await asyncio.to_thread(event.wait, 3)
            if cancel:
                pending.cancel()
            release["a"].set()
            assert await asyncio.to_thread(returned["a"].wait, 3)
            # Let the first child report its result; the gated second child must keep the scan open.
            await asyncio.sleep(0.05)
            assert not pending.done()
            assert worker._poll_lock.locked()
            release["b"].set()
            with pytest.raises(asyncio.CancelledError if cancel else StatePersistenceError) as caught:
                await pending
            if not cancel:
                assert caught.value is failure
            assert returned["b"].is_set()
        finally:
            for event in release.values():
                event.set()
            await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(scenario())


def test_repository_default_connection_closes_on_success_and_error(monkeypatch):
    from app.repositories import memory_task_repository as module
    from app.repositories.memory_task_repository import MemoryTaskRepository

    opened = []
    connect = module.connect
    def tracked_connect():
        connection = connect()
        opened.append(connection)
        return connection
    monkeypatch.setattr(module, "connect", tracked_connect)
    repo = MemoryTaskRepository()
    assert repo.list_tasks("nobody") == []
    with pytest.raises(ValueError):
        repo.cancel("nobody", "missing")
    assert len(opened) == 2
    for connection in opened:
        with pytest.raises(StorageError):
            connection.execute("SELECT 1")
