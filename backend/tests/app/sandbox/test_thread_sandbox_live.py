"""真实 Docker 验证：跨 Run 复用、隔离、超时/取消和重启清理。"""

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest

from app.agents.lead_agent import LeadAgent
from app.domain.messages import Message, ToolCall
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.events_repository import EventRepository
from app.repositories.run_repository import RunRepository
from app.runtime.agent_runtime import AgentRuntime
from app.runtime.stream_bridge import MemoryStreamBridge
from app.sandbox.docker_runner import DockerCommandRunner, DockerRunnerConfig
from app.sandbox.manager import ThreadSandboxManager
from app.services import thread_service
from app.services.run_service import RunService
from app.services.thread_service import ThreadService
from app.tools.bash import BashTool
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_SANDBOX_TEST") != "1",
    reason="设置 RUN_DOCKER_SANDBOX_TEST=1 后才启动隔离的真实 Docker 容器",
)


def ctx(workspace, run="run-1", thread="thread-1"):
    return dict(user_id="sandbox-test", thread_id=thread, run_id=run, workspace_path=str(workspace))


async def finish(manager, context):
    await manager.end_run(**{k: v for k, v in context.items() if k != "workspace_path"})


async def execute(manager, context, command):
    return await manager.run(**context, command=command, tool_call_id="call-test")


def test_real_bash_inherits_host_clock_and_timezone(tmp_path):
    if not Path("/etc/localtime").is_file():
        pytest.skip("当前宿主机没有 /etc/localtime 时区文件")
    host_timezone = subprocess.check_output(
        ["date", "+%z %Z"], text=True,
        env={**os.environ, "TZ": ":/etc/localtime"},
    ).strip()
    command = "date '+%s %z %Z'; date -u '+%z %Z'"

    def check_result(result, started):
        assert result.exit_code == 0
        local, utc = result.output.strip().splitlines()
        epoch, timezone = local.split(" ", 1)
        assert int(started) <= int(epoch) <= int(time.time())
        assert timezone == host_timezone
        assert utc == "+0000 UTC"

    async def scenario():
        runner = DockerCommandRunner()
        started = time.time()
        check_result(await runner.run(
            command=command, workspace_path=str(tmp_path),
            run_id="timezone-direct", tool_call_id="time-check",
        ), started)

        # 同一 Thread 的下一轮继续复用容器，也必须保持相同的时区和系统时钟。
        manager = ThreadSandboxManager(runner, scope=uuid4().hex)
        container_ids = []
        try:
            for run_id in ("timezone-1", "timezone-2"):
                context = ctx(tmp_path, run=run_id)
                await manager.begin_run(**context)
                started = time.time()
                check_result(await execute(manager, context, command), started)
                container_ids.append(manager._active[(context["user_id"], context["thread_id"])].container_id)
                await finish(manager, context)
            assert container_ids[0] == container_ids[1]
            mounts = subprocess.check_output(
                ["docker", "inspect", "--format", "{{range .Mounts}}{{if eq .Destination \"/etc/localtime\"}}{{.RW}}{{end}}{{end}}", container_ids[0]],
                text=True,
            ).strip()
            assert mounts == "false"
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_real_reuse_isolation_recycling_and_persistent_workspace(tmp_path):
    async def scenario():
        now = [0.0]
        runner = DockerCommandRunner(DockerRunnerConfig(idle_timeout_seconds=10))
        manager = ThreadSandboxManager(runner, scope=uuid4().hex, clock=lambda: now[0])
        other_workspace = tmp_path / "other"
        other_workspace.mkdir()
        first = ctx(tmp_path)
        key = (first["user_id"], first["thread_id"])
        await manager.start()
        try:
            await manager.begin_run(**first)
            assert manager._active[key].container_id is None
            result = await execute(manager, first, "printf keep > /tmp/session-marker; printf saved > durable.txt")
            assert result.exit_code == 0
            original = manager._active[key].container_id
            now[0] = 100
            assert await manager.reap_idle() == []
            await finish(manager, first)

            second = ctx(tmp_path, run="run-2")
            await manager.begin_run(**second)
            result = await execute(manager, second, "cat /tmp/session-marker; cat durable.txt")
            assert result.output == "keepsaved"
            assert manager._active[key].container_id == original
            await finish(manager, second)

            other = ctx(other_workspace, thread="thread-2")
            await manager.begin_run(**other)
            result = await execute(manager, other, "test ! -e /tmp/session-marker && test ! -e durable.txt")
            assert result.exit_code == 0

            now[0] = 110
            assert await manager.reap_idle() == [original]
            assert not await runner.container_is_running(original)
            third = ctx(tmp_path, run="run-3")
            await manager.begin_run(**third)
            result = await execute(manager, third, "test ! -e /tmp/session-marker && cat durable.txt")
            assert result.output == "saved"
            assert manager._active[key].container_id != original
            active_ids = [entry.container_id for entry in manager._active.values()]
        finally:
            await manager.close()
        for name in active_ids:
            assert not await runner.container_is_running(name)

    asyncio.run(scenario())


def test_real_timeout_and_cancellation_remove_container(tmp_path):
    async def scenario():
        runner = DockerCommandRunner(DockerRunnerConfig(timeout_seconds=10))
        manager = ThreadSandboxManager(runner, scope=uuid4().hex)
        context = ctx(tmp_path)
        key = (context["user_id"], context["thread_id"])
        try:
            await manager.begin_run(**context)
            await execute(manager, context, "true")
            original = manager._active[key].container_id
            runner.config = replace(runner.config, timeout_seconds=0.3)
            result = await execute(manager, context, "sleep 60")
            assert result.timed_out
            assert not await runner.container_is_running(original)
            runner.config = replace(runner.config, timeout_seconds=10)
            await execute(manager, context, "true")
            replacement = manager._active[key].container_id
            task = asyncio.create_task(execute(manager, context, "touch started; sleep 60"))
            async with asyncio.timeout(10):
                while not (tmp_path / "started").exists():
                    await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not await runner.container_is_running(replacement)
            await finish(manager, context)
            assert not manager._warm_pool
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_startup_removes_only_own_orphaned_containers(tmp_path):
    async def scenario():
        runner = DockerCommandRunner()
        scope = uuid4().hex
        own = "deer-mini-test-" + uuid4().hex
        unrelated = "deer-mini-test-" + uuid4().hex
        manager = ThreadSandboxManager(runner, scope=scope)
        try:
            for name, owner in [(own, scope), (unrelated, uuid4().hex)]:
                await runner.start_container(
                    container_name=name, workspace_path=str(tmp_path),
                    user_id="sandbox-test", thread_id=name, scope=owner,
                )
            await manager.start()
            assert not await runner.container_is_running(own)
            assert await runner.container_is_running(unrelated)
        finally:
            await manager.close()
            await runner.remove_container(own)
            await runner.remove_container(unrelated)

    asyncio.run(scenario())


class BashModel:
    """模型响应固定，但实际 Bash、工具循环、状态保存均运行真实实现。"""

    def __init__(self, command):
        self.command = command
        self.calls = 0

    async def chat(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return Message(role="assistant", content="", tool_calls=[
                ToolCall(id=uuid4().hex, name="bash", arguments={
                    "description": "验证跨 Run 复用", "command": self.command,
                })
            ])
        assert messages[-1].role == "tool"
        return Message(role="assistant", content=messages[-1].content)

    async def close(self):
        pass


def test_runtime_keeps_tool_loop_checkpoints_and_stream_across_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()

    async def scenario():
        thread = ThreadService().create_thread("sandbox-test", "warm pool")
        runner = DockerCommandRunner()
        manager = ThreadSandboxManager(runner, scope=uuid4().hex)
        bridge = MemoryStreamBridge()
        runtime = AgentRuntime(bridge, sandbox_lifecycle=manager)
        key = (thread.user_id, thread.id)
        names = []
        try:
            for command in ["printf runtime > /tmp/runtime-marker", "cat /tmp/runtime-marker"]:
                run = RunService().create_run(thread.user_id, thread.id, "scripted-test")
                registry = ToolRegistry()
                registry.register(BashTool(manager))
                agent = LeadAgent(BashModel(command), registry, ToolExecutor(registry))
                final_state = await runtime.run(
                    user_id=thread.user_id, thread_id=thread.id, run_id=run.id,
                    user_message="执行 Bash", agent=agent,
                )
                assert RunRepository().get(run.id, thread.user_id).status == "success"
                names.append(manager._warm_pool[key].container_id)
                assert key not in manager._active
                checkpoints = CheckpointRepository().history(thread.id, thread.user_id, run.id)
                assert any(message.role == "tool" for cp in checkpoints for message in cp.state.messages)
                assert EventRepository().list_for_run(thread.id, run.id, thread.user_id)[-1].event_type == "run.end"
                async with asyncio.timeout(2):
                    events = [event async for event in bridge.subscribe(run.id)]
                assert any(event.event == "metadata" for event in events)
            assert names[0] == names[1]
            assert final_state.messages[-1].content == "runtime"
        finally:
            await manager.close()
            await bridge.close()

    asyncio.run(scenario())
