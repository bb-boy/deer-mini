"""辅助日志与关键状态必须有不同的失败语义。"""

import asyncio
import threading

import pytest

from app.domain.messages import Message
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.events_repository import EventRepository
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository
from app.runtime.agent_runtime import AgentRuntime
from app.runtime.stream_bridge import MemoryStreamBridge
from app.sandbox.manager import ThreadSandboxManager
from app.services import thread_service
from app.services.run_service import RunService


@pytest.fixture
def storage(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "runtime.db")
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()
    thread = thread_service.ThreadService().create_thread("alice", "runtime")
    run = RunService().create_run("alice", thread.id, "test")
    return thread, run


class AnswerAgent:
    called = False

    async def run(self, state, context):
        self.called = True
        await context.record_event("text.delta", {"text": "完成", "message_id": "answer"})
        state.messages.append(Message(role="assistant", content="完成", id="answer"))
        return state


def run_args(storage, agent):
    thread, run = storage
    return dict(user_id="alice", thread_id=thread.id, run_id=run.id, user_message="你好", agent=agent)


def broken_logs(*args, **kwargs):
    raise OSError("辅助日志故障")


def test_run_succeeds_even_if_every_auxiliary_log_write_fails(storage, monkeypatch):
    monkeypatch.setattr(EventRepository, "append_batch", broken_logs)

    async def scenario():
        thread, run = storage
        bridge = MemoryStreamBridge()
        result = await AgentRuntime(bridge).run(**run_args(storage, AnswerAgent()))
        assert result.messages[-1].content == "完成"
        assert RunRepository().get(run.id, "alice").status == "success"
        assert CheckpointRepository().latest(thread.id, "alice").state.messages[-1].content == "完成"
        events = [event async for event in bridge.subscribe(run.id)]
        assert events[-1].data["event_type"] == "run.end"
        assert events[-1].data["payload"]["status_confirmed"] is True
        assert EventRepository().list_for_run(thread.id, run.id, "alice") == []
        await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_initial", [False, True])
def test_checkpoint_failure_is_fatal_and_not_hidden_by_log_failure(storage, monkeypatch, fail_initial):
    monkeypatch.setattr(EventRepository, "append_batch", broken_logs)

    class BrokenCheckpoint(CheckpointRepository):
        def save(self, checkpoint):
            if fail_initial or checkpoint.state.messages[-1].role == "assistant":
                raise OSError("关键状态写入失败")
            return super().save(checkpoint)

    async def scenario():
        thread, run = storage
        bridge = MemoryStreamBridge()
        agent = AnswerAgent()
        with pytest.raises(OSError, match="关键状态写入失败"):
            await AgentRuntime(bridge, checkpoint_repository=BrokenCheckpoint()).run(**run_args(storage, agent))
        assert agent.called is not fail_initial
        assert RunRepository().get(run.id, "alice").status == "error"
        events = [event async for event in bridge.subscribe(run.id)]
        kinds = [event.data.get("event_type") for event in events]
        assert "run.end" not in kinds
        assert kinds[-1] == "run.error"
        assert "关键状态写入失败" in events[-1].data["payload"]["message"]
        await bridge.close()

    asyncio.run(scenario())


def test_success_is_announced_only_after_run_status_is_persisted(storage):
    thread, run = storage

    class AuditedBridge(MemoryStreamBridge):
        async def publish(self, run_id, event, data):
            if data.get("event_type") == "run.end":
                assert RunRepository().get(run_id, "alice").status == "success"
            await super().publish(run_id, event, data)

    async def scenario():
        bridge = AuditedBridge()
        await AgentRuntime(bridge).run(**run_args(storage, AnswerAgent()))
        await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("all_status_writes_fail", [False, True])
def test_run_status_write_failure_never_announces_success(storage, monkeypatch, all_status_writes_fail):
    def fail_status(*args, **kwargs):
        raise OSError("Run 状态写入失败")

    monkeypatch.setattr(RunService, "finish_run", fail_status)
    if all_status_writes_fail:
        monkeypatch.setattr(RunService, "recover_orphaned_run", fail_status)

    async def scenario():
        thread, run = storage
        bridge = MemoryStreamBridge()
        with pytest.raises(OSError, match="Run 状态写入失败"):
            await AgentRuntime(bridge).run(**run_args(storage, AnswerAgent()))
        events = [event async for event in bridge.subscribe(run.id)]
        assert "run.end" not in [event.data.get("event_type") for event in events]
        last = events[-1].data
        assert last["event_type"] == "run.error"
        assert last["payload"]["status_confirmed"] is not all_status_writes_fail
        assert RunRepository().get(run.id, "alice").status == ("running" if all_status_writes_fail else "error")
        await bridge.close()

    asyncio.run(scenario())


def test_cancel_during_checkpoint_waits_for_write_and_preserves_step_order(storage):
    class GatedCheckpoint(CheckpointRepository):
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def save(self, checkpoint):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                assert self.release.wait(3)
            return super().save(checkpoint)

    repo = GatedCheckpoint()

    async def scenario():
        thread, run = storage
        bridge = MemoryStreamBridge()
        task = asyncio.create_task(AgentRuntime(bridge, checkpoint_repository=repo).run(
            **run_args(storage, AnswerAgent()),
        ))
        try:
            assert await asyncio.to_thread(repo.started.wait, 2)
            task.cancel("cancelled_by_user")
            await asyncio.sleep(0.01)
            assert not task.done()
            task.cancel("second_cancel")
            repo.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert RunRepository().get(run.id, "alice").status == "interrupted"
            history = repo.history(thread.id, "alice", run.id)
            assert [item.step for item in history] == [1, 2]
            events = [event async for event in bridge.subscribe(run.id)]
            assert events[-1].data["event_type"] == "run.interrupted"
        finally:
            repo.release.set()
            await bridge.close()

    asyncio.run(scenario())


def test_timeout_closes_stream_and_preserves_real_terminal_status(storage):
    class WaitingAgent:
        async def run(self, state, context):
            await asyncio.sleep(10)

    async def scenario():
        thread, run = storage
        bridge = MemoryStreamBridge()
        with pytest.raises(TimeoutError):
            await AgentRuntime(bridge).run(**run_args(storage, WaitingAgent()), timeout_seconds=0.01)
        assert RunRepository().get(run.id, "alice").status == "timeout"
        events = [event async for event in bridge.subscribe(run.id)]
        assert events[-1].data["event_type"] == "run.timeout"
        await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "error", "interrupted", "timeout"])
def test_next_run_can_start_while_previous_auxiliary_logs_are_still_closing(
    storage, monkeypatch, outcome,
):
    thread, first_run = storage
    write_started, release_write = threading.Event(), threading.Event()
    original_append = EventRepository.append_batch

    def gated_append(self, events, user_id):
        if events[0].run_id == first_run.id:
            write_started.set()
            assert release_write.wait(10)
        return original_append(self, events, user_id)

    monkeypatch.setattr(EventRepository, "append_batch", gated_append)

    async def scenario():
        bridge = MemoryStreamBridge()
        # begin/end 只登记归属，不调用 Docker；使用真实容器管理器验证下一轮。
        manager = ThreadSandboxManager(object())
        terminal_received, agent_started = asyncio.Event(), asyncio.Event()

        class FirstAgent(AnswerAgent):
            async def run(self, state, context):
                agent_started.set()
                if outcome == "error":
                    raise ValueError("Agent 执行失败")
                if outcome in {"interrupted", "timeout"}:
                    await asyncio.Event().wait()
                return await super().run(state, context)

        async def watch_terminal():
            async for event in bridge.subscribe(first_run.id):
                if event.data.get("event_type") in {
                    "run.end", "run.error", "run.interrupted", "run.timeout",
                }:
                    terminal_received.set()
                    return

        watcher = asyncio.create_task(watch_terminal())
        first_task = asyncio.create_task(AgentRuntime(
            bridge, sandbox_lifecycle=manager,
        ).run(**run_args(storage, FirstAgent()), timeout_seconds=0.05 if outcome == "timeout" else 240))
        try:
            await asyncio.wait_for(agent_started.wait(), 5)
            if outcome == "interrupted":
                first_task.cancel("cancelled_by_user")
            await asyncio.wait_for(terminal_received.wait(), 5)
            assert await asyncio.to_thread(write_started.wait, 2)
            assert not first_task.done()
            assert RunRepository().get(first_run.id, "alice").status == outcome

            # 终态通知后立刻发送下一条消息，不必等上一轮辅助日志结束。
            second_run = RunService().create_run("alice", thread.id, "test")
            result = await AgentRuntime(bridge, sandbox_lifecycle=manager).run(
                **run_args((thread, second_run), AnswerAgent()),
            )
            assert result.messages[-1].content == "完成"
            assert RunRepository().get(second_run.id, "alice").status == "success"
        finally:
            release_write.set()
            if not terminal_received.is_set():
                first_task.cancel()
            await asyncio.gather(first_task, return_exceptions=True)
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await manager.close()
            await bridge.close()

    asyncio.run(scenario())


def test_cancel_during_initial_lookup_finishes_run_and_stream(storage):
    class GatedThreadRepository(ThreadRepository):
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()

        def get(self, thread_id, user_id):
            self.started.set()
            assert self.release.wait(5)
            return super().get(thread_id, user_id)

    repository = GatedThreadRepository()

    async def scenario():
        thread, run = storage
        bridge = MemoryStreamBridge()
        agent = AnswerAgent()
        task = asyncio.create_task(AgentRuntime(
            bridge, thread_repository=repository,
        ).run(**run_args(storage, agent)))
        try:
            assert await asyncio.to_thread(repository.started.wait, 2)
            task.cancel("server_shutdown")
            repository.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not agent.called
            assert RunRepository().get(run.id, "alice").status == "interrupted"
            assert ThreadRepository().get(thread.id, "alice").status == "idle"

            async def receive_all():
                return [event async for event in bridge.subscribe(run.id)]

            events = await asyncio.wait_for(receive_all(), 1)
            assert events[-1].data["event_type"] == "run.interrupted"
        finally:
            repository.release.set()
            await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("wrong_owner", [True, False])
def test_failed_ownership_check_does_not_change_or_close_another_run(storage, wrong_owner):
    async def scenario():
        thread, run = storage
        bridge = MemoryStreamBridge()
        arguments = run_args(storage, AnswerAgent())
        arguments["user_id" if wrong_owner else "thread_id"] = "unrelated"
        with pytest.raises(ValueError, match="不存在"):
            await AgentRuntime(bridge).run(**arguments)
        assert RunRepository().get(run.id, "alice").status == "pending"
        # 如果误关闭了其他 Run 的流，这次合法发布会失败。
        await bridge.publish(run.id, "metadata", {"run_id": run.id, "thread_id": thread.id})
        await bridge.close()

    asyncio.run(scenario())
