"""验证 Thread 复用、Run 占用保护，以及领取/回收之间的互斥。"""

import asyncio

import pytest

from app.sandbox.base import CommandResult
from app.sandbox.docker_runner import DockerRunnerConfig
from app.sandbox.manager import ThreadSandboxManager


class FakeDocker:
    def __init__(self):
        self.config = DockerRunnerConfig(idle_timeout_seconds=10)
        self.live = set()
        self.created = []
        self.executed = []
        self.remove_error = False
        self.exec_error = None

    async def start_container(self, **kwargs):
        name = kwargs["container_name"]
        self.created.append(name)
        self.live.add(name)

    async def run_in_container(self, *, container_name, command):
        self.executed.append(container_name)
        if self.exec_error is not None:
            raise self.exec_error
        return CommandResult(command, 0)

    async def container_is_running(self, name):
        return name in self.live

    async def remove_container(self, name):
        if self.remove_error:
            raise RuntimeError("Docker unavailable")
        self.live.discard(name)

    async def remove_owned_containers(self, scope):
        self.live.clear()


def context(path, run="run-1", thread="thread-1", user="alice"):
    return dict(user_id=user, thread_id=thread, run_id=run, workspace_path=str(path))


async def end(manager, ctx):
    await manager.end_run(**{k: v for k, v in ctx.items() if k != "workspace_path"})


async def command(manager, ctx):
    return await manager.run(**ctx, command="pwd", tool_call_id="call-1")


def test_lazy_creation_reuse_and_stale_release(tmp_path):
    async def scenario():
        docker = FakeDocker()
        manager = ThreadSandboxManager(docker)
        first = context(tmp_path)
        await manager.begin_run(**first)
        assert docker.created == []
        await command(manager, first)
        await command(manager, first)
        await end(manager, first)
        assert ("alice", "thread-1") in manager._warm_pool

        second = context(tmp_path, run="run-2")
        await manager.begin_run(**second)
        await end(manager, first)  # 旧 Run 的重复收尾不能释放 run-2。
        await command(manager, second)
        assert len(docker.created) == 1
        assert docker.executed == [docker.created[0]] * 3
        assert manager._active[("alice", "thread-1")].active_run_id == "run-2"
        await end(manager, second)
        await manager.close()
        assert not docker.live

    asyncio.run(scenario())


def test_isolation_active_protection_and_idle_timeout(tmp_path):
    async def scenario():
        now = [0.0]
        docker = FakeDocker()
        manager = ThreadSandboxManager(docker, clock=lambda: now[0])
        a = context(tmp_path)
        b = context(tmp_path, user="bob")
        c = context(tmp_path, thread="thread-2")
        for ctx in (a, b, c):
            await manager.begin_run(**ctx)
            await command(manager, ctx)
        assert len(set(docker.created)) == 3
        now[0] = 100
        assert await manager.reap_idle() == []  # 整个 Run 都受占用保护。
        await end(manager, a)
        now[0] = 109
        assert await manager.reap_idle() == []
        now[0] = 110
        assert await manager.reap_idle() == [docker.created[0]]
        assert len(docker.live) == 2
        await manager.close()

    asyncio.run(scenario())


def test_rebuild_after_container_disappears(tmp_path):
    async def scenario():
        docker = FakeDocker()
        manager = ThreadSandboxManager(docker)
        ctx = context(tmp_path)
        await manager.begin_run(**ctx)
        await command(manager, ctx)
        docker.live.clear()
        await command(manager, ctx)
        assert len(docker.created) == 2
        assert docker.executed[0] != docker.executed[1]
        await manager.close()

    asyncio.run(scenario())


def test_reclaim_waits_until_reaping_finishes(tmp_path):
    async def scenario():
        now = [0.0]
        started = asyncio.Event()
        proceed = asyncio.Event()

        class SlowRemoval(FakeDocker):
            async def remove_container(self, name):
                started.set()
                await proceed.wait()
                await super().remove_container(name)

        docker = SlowRemoval()
        manager = ThreadSandboxManager(docker, clock=lambda: now[0])
        first = context(tmp_path)
        await manager.begin_run(**first)
        await command(manager, first)
        await end(manager, first)
        now[0] = 20
        reaping = asyncio.create_task(manager.reap_idle())
        await started.wait()
        second = context(tmp_path, run="run-2")
        acquiring = asyncio.create_task(manager.begin_run(**second))
        await asyncio.sleep(0)
        assert not acquiring.done()
        proceed.set()
        await reaping
        await acquiring
        await command(manager, second)
        assert len(docker.created) == 2
        assert docker.executed[-1] in docker.live
        await manager.close()

    asyncio.run(scenario())


def test_cancelled_command_is_destroyed_and_failed_cleanup_never_reused(tmp_path):
    async def scenario():
        docker = FakeDocker()
        manager = ThreadSandboxManager(docker)
        ctx = context(tmp_path)
        await manager.begin_run(**ctx)
        docker.exec_error = asyncio.CancelledError()
        docker.remove_error = True
        with pytest.raises(asyncio.CancelledError):
            await command(manager, ctx)
        await end(manager, ctx)
        entry = manager._warm_pool[("alice", "thread-1")]
        assert entry.broken
        next_ctx = context(tmp_path, run="run-2")
        await manager.begin_run(**next_ctx)
        docker.exec_error = None
        with pytest.raises(RuntimeError, match="unavailable"):
            await command(manager, next_ctx)
        assert len(docker.executed) == 1
        docker.remove_error = False
        await command(manager, next_ctx)
        assert len(docker.created) == 2
        await manager.close()

    asyncio.run(scenario())


def test_delete_destroys_container_before_files_and_rejects_active_run(tmp_path):
    async def scenario():
        docker = FakeDocker()
        manager = ThreadSandboxManager(docker)
        ctx = context(tmp_path)
        await manager.begin_run(**ctx)
        await command(manager, ctx)
        deleted = []

        async def delete_files():
            assert not docker.live
            deleted.append(True)

        with pytest.raises(RuntimeError, match="运行中"):
            await manager.delete_thread(user_id="alice", thread_id="thread-1", delete_workspace=delete_files)
        await end(manager, ctx)
        await manager.delete_thread(user_id="alice", thread_id="thread-1", delete_workspace=delete_files)
        assert deleted == [True]
        assert not manager._warm_pool

    asyncio.run(scenario())
