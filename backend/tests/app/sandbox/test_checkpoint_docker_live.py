"""真实 Docker 与恢复点协作；显式启用后仅操作临时数据和独立标签容器。"""

import asyncio
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from app.domain.messages import Message
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.runtime.agent_runtime import AgentRuntime
from app.runtime.stream_bridge import MemoryStreamBridge
from app.sandbox.docker_runner import DockerCommandRunner, DockerRunnerConfig
from app.sandbox.manager import ThreadSandboxManager
from app.services.run_coordinator import RunCoordinator
from app.services.run_service import RunService
from app.services.thread_service import ThreadService


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_SANDBOX_TEST") != "1",
    reason="设置 RUN_DOCKER_SANDBOX_TEST=1 后才启动真实 Docker 容器",
)


@pytest.fixture
def isolated_thread(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "users"))
    monkeypatch.setenv("DEER_MINI_MEMORY_ENABLED", "false")
    database.initialize_database()
    return ThreadService().create_thread("checkpoint-docker-test")


def remaining_containers(scope: str) -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=deer-mini.sandbox-owner={scope}"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return result.stdout.splitlines()


async def wait_for_write(path: Path) -> None:
    for _ in range(200):
        if path.exists() and path.stat().st_size:
            return
        await asyncio.sleep(0.05)
    pytest.fail("真实容器没有在预期时间内写入临时文件")


def test_real_container_stopped_before_capture_and_after_turn(isolated_thread):
    """旧后台写入在快照前停止；新一轮后台写入也必须在成功返回前停止。"""
    thread = isolated_thread
    workspace = Path(thread.workspace_path)
    scope = f"checkpoint-test-{uuid4().hex}"

    async def scenario() -> None:
        runner = DockerCommandRunner(DockerRunnerConfig(timeout_seconds=20, network_enabled=False))
        manager = ThreadSandboxManager(runner, scope=scope)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=manager)
        try:
            await coordinator.start()
            await manager.begin_run(user_id=thread.user_id, thread_id=thread.id,
                                    run_id="old", workspace_path=thread.workspace_path)
            result = await manager.run(
                command="nohup bash -c 'while :; do echo old >> old.txt; sleep 0.05; done' >/dev/null 2>&1 &",
                workspace_path=thread.workspace_path, user_id=thread.user_id,
                thread_id=thread.id, run_id="old", tool_call_id="old-writer",
            )
            assert result.exit_code == 0
            await wait_for_write(workspace / "old.txt")
            await manager.end_run(user_id=thread.user_id, thread_id=thread.id, run_id="old")
            assert remaining_containers(scope)
            run = RunService().create_run(thread.user_id, thread.id, "mock")

            class Agent:
                async def run(self, state, context):
                    assert not remaining_containers(scope)
                    before = (workspace / "old.txt").read_bytes()
                    await asyncio.sleep(0.3)
                    assert (workspace / "old.txt").read_bytes() == before
                    assert len(coordinator.file_checkpoints.list_points(thread)) == 1
                    result = await manager.run(
                        command="nohup bash -c 'while :; do echo new >> new.txt; sleep 0.05; done' >/dev/null 2>&1 &",
                        workspace_path=thread.workspace_path, user_id=thread.user_id,
                        thread_id=thread.id, run_id=run.id, tool_call_id="new-writer",
                    )
                    assert result.exit_code == 0
                    await wait_for_write(workspace / "new.txt")
                    state.messages.append(Message(role="assistant", content="done"))
                    return state

            await AgentRuntime(bridge, sandbox_lifecycle=manager,
                               file_checkpoints=coordinator.file_checkpoints).run(
                thread.user_id, thread.id, run.id, "write", Agent())
            assert RunService().get_run(run.id, thread.user_id).status == "success"
            assert not remaining_containers(scope)
            stopped = (workspace / "new.txt").read_bytes()
            await asyncio.sleep(0.3)
            assert (workspace / "new.txt").read_bytes() == stopped
            point = coordinator.file_checkpoints.list_points(thread)[0]
            preview = await coordinator.preview_restore(user_id=thread.user_id,
                thread_id=thread.id, point_id=point["id"])
            restored = await coordinator.restore_thread(user_id=thread.user_id,
                thread_id=thread.id, operation_id="restore-real-docker", point_id=point["id"],
                revision=preview["revision"], fingerprint=preview["fingerprint"])
            assert restored["status"] == "committed" and restored["cleaned"]
            assert not (workspace / "new.txt").exists()
            assert CheckpointRepository().latest(thread.id, thread.user_id).state.messages == []
            await asyncio.sleep(0.3)
            assert not (workspace / "new.txt").exists()
        finally:
            await coordinator.shutdown()
            await bridge.close()
        assert not remaining_containers(scope)

    asyncio.run(scenario())


def test_real_container_history_readonly_and_snapshot_private(isolated_thread):
    """Docker 实际挂载保护历史工具结果，且不暴露恢复点对象目录。"""
    thread = isolated_thread
    workspace = Path(thread.workspace_path)
    history = workspace / ".tool-results"
    history.mkdir()
    (history / "saved.txt").write_text("history", encoding="utf-8")
    private = workspace.parent / ".checkpoints"
    private.mkdir()
    (private / "private.txt").write_text("private", encoding="utf-8")
    runner = DockerCommandRunner(DockerRunnerConfig(timeout_seconds=20, network_enabled=False))

    async def scenario() -> None:
        result = await runner.run(
            command=("test ! -e /mnt/user-data/.checkpoints && "
                     "test ! -w .tool-results/saved.txt && "
                     "! (echo overwritten > .tool-results/saved.txt)"),
            workspace_path=thread.workspace_path, run_id=uuid4().hex, tool_call_id="mount-check",
        )
        assert result.exit_code == 0, result.output
        assert (history / "saved.txt").read_text() == "history"

    asyncio.run(scenario())


def test_real_active_write_cancel_then_restore(isolated_thread):
    """取消真实前台写入并收尾后，恢复消息和文件且不会再次出现残留写入。"""
    thread = isolated_thread
    workspace = Path(thread.workspace_path)
    scope = f"checkpoint-test-{uuid4().hex}"

    async def scenario() -> None:
        runner = DockerCommandRunner(DockerRunnerConfig(timeout_seconds=30, network_enabled=False))
        manager = ThreadSandboxManager(runner, scope=scope)
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=manager)
        task = None
        try:
            await coordinator.start()
            run = RunService().create_run(thread.user_id, thread.id, "mock")

            class Agent:
                async def run(self, state, context):
                    await manager.run(
                        command="while :; do echo tick >> live.txt; sleep 0.05; done",
                        workspace_path=thread.workspace_path, user_id=thread.user_id,
                        thread_id=thread.id, run_id=run.id, tool_call_id="active-writer",
                    )
                    raise AssertionError("取消必须中断执行")

            task = asyncio.create_task(AgentRuntime(bridge, sandbox_lifecycle=manager,
                file_checkpoints=coordinator.file_checkpoints).run(
                    thread.user_id, thread.id, run.id, "long write", Agent()))
            await wait_for_write(workspace / "live.txt")
            assert remaining_containers(scope)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert RunService().get_run(run.id, thread.user_id).status == "interrupted"
            assert not remaining_containers(scope)
            point = coordinator.file_checkpoints.list_points(thread)[0]
            preview = await coordinator.preview_restore(user_id=thread.user_id,
                thread_id=thread.id, point_id=point["id"])
            restored = await coordinator.restore_thread(user_id=thread.user_id,
                thread_id=thread.id, operation_id="restore-cancelled-docker", point_id=point["id"],
                revision=preview["revision"], fingerprint=preview["fingerprint"])
            assert restored["status"] == "committed" and restored["cleaned"]
            assert CheckpointRepository().latest(thread.id, thread.user_id).state.messages == []
            await asyncio.sleep(0.3)
            assert not (workspace / "live.txt").exists()
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await coordinator.shutdown()
            await bridge.close()
        assert not remaining_containers(scope)

    asyncio.run(scenario())
