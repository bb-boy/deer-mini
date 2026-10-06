"""存储失败日志仅记录安全分类，原始异常仍按 Runtime 边界传播。"""

import asyncio
import errno

import pytest

from app.runtime.errors import StatePersistenceError
from app.storage.errors import classify_os_error


PRIVATE_MARKER = "SYNTHETIC_PRIVATE_STORAGE_VALUE"


def failure(wrapped=False):
    try:
        try:
            raise OSError(errno.ENOSPC, PRIVATE_MARKER)
        except OSError as error:
            raise classify_os_error(error, operation="write", stage="directory_sync",
                                    commit_state="uncertain") from error
    except Exception as error:
        if not wrapped:
            return error
        try:
            raise StatePersistenceError("关键状态保存失败") from error
        except StatePersistenceError as wrapped_error:
            return wrapped_error


@pytest.mark.parametrize("kind", ["coordinator", "event_writer", "stream_cleanup"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_background_failure_observers_do_not_log_private_storage_causes(caplog, kind, wrapped):
    from app.runtime.event_recorder import EventRecorder
    from app.runtime.stream_bridge import MemoryStreamBridge
    from app.services.run_coordinator import RunCoordinator

    async def scenario():
        async def fail():
            raise failure(wrapped)
        task = asyncio.create_task(fail())
        await asyncio.gather(task, return_exceptions=True)
        if kind == "coordinator":
            observer = object.__new__(RunCoordinator)
            observer._tasks = {}
            observer._consume_finished_task("run", task)
        elif kind == "event_writer":
            observer = object.__new__(EventRecorder)
            observer._run_id = "run"
            observer._observe_writer(task)
        else:
            observer = MemoryStreamBridge()
            observer._consume_cleanup_task("run", task)
            await observer.close()
    asyncio.run(scenario())
    assert caplog.records
    assert PRIVATE_MARKER not in caplog.text
    assert "directory_sync" in caplog.text
    assert "no_space" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("critical", [False, True])
def test_ordinary_errors_keep_traceback_diagnostics(caplog, critical):
    from app.services.run_coordinator import RunCoordinator

    async def scenario():
        async def fail():
            if critical:
                raise StatePersistenceError("ordinary diagnostic")
            raise RuntimeError("ordinary diagnostic")
        task = asyncio.create_task(fail())
        await asyncio.gather(task, return_exceptions=True)
        observer = object.__new__(RunCoordinator)
        observer._tasks = {}
        observer._consume_finished_task("run", task)
    asyncio.run(scenario())
    assert "ordinary diagnostic" in caplog.text
    assert caplog.records[-1].exc_info is not None


def test_auxiliary_event_retry_logs_safe_fields_without_changing_failure_policy(caplog):
    from app.runtime.event_recorder import EventRecorder
    from app.runtime.stream_bridge import MemoryStreamBridge
    class BrokenRepository:
        def append_batch(self, events, user_id):
            raise failure()
    async def scenario():
        bridge = MemoryStreamBridge()
        recorder = EventRecorder("alice", "thread", "run", bridge, BrokenRepository(),
                                 max_attempts=1, flush_interval=1)
        await recorder.record_event("run.start", {})
        await recorder.close()
        assert recorder.dropped_events == 1
        await bridge.close()
    asyncio.run(scenario())
    assert PRIVATE_MARKER not in caplog.text
    assert "directory_sync" in caplog.text


def test_runtime_failure_cleanup_logs_safe_fields_and_preserves_fatal_exception(caplog):
    from app.runtime.agent_runtime import AgentRuntime
    from app.runtime.stream_bridge import MemoryStreamBridge
    from app.services.run_service import RunService
    from app.services.thread_service import ThreadService
    thread = ThreadService().create_thread("alice", "safe logs")
    run = RunService().create_run("alice", thread.id, "test")
    original = failure(wrapped=True)
    class Agent:
        async def run(self, state, context):
            raise original
    class BrokenReadService(RunService):
        def get_run(self, *args):
            raise failure()
    async def scenario():
        bridge = MemoryStreamBridge()
        runtime = AgentRuntime(bridge, run_service=BrokenReadService())
        with pytest.raises(StatePersistenceError) as caught:
            await runtime.run(user_id="alice", thread_id=thread.id, run_id=run.id,
                              user_message="hello", agent=Agent())
        assert caught.value is original
        assert RunService().get_run(run.id, "alice").status == "error"
        await bridge.close()
    asyncio.run(scenario())
    assert PRIVATE_MARKER not in caplog.text
    assert "directory_sync" in caplog.text


@pytest.mark.parametrize("cleanup_phase", ["middleware", "model"])
def test_lead_agent_cleanup_cannot_log_private_storage_causes_or_replace_original(caplog, cleanup_phase):
    from app.agents.lead_agent import LeadAgent
    from app.agents.middleware import AgentMiddleware
    from app.domain.threads import ThreadState
    from app.runtime.context import RuntimeContext
    from app.tools.executor import ToolExecutor
    from app.tools.registry import ToolRegistry
    original = failure(wrapped=True)
    class Cleanup(AgentMiddleware):
        async def before_agent(self, state, context):
            raise original
        async def after_agent(self, state, context, error):
            if cleanup_phase == "middleware":
                raise failure()
    class Model:
        async def close(self):
            if cleanup_phase == "model":
                raise RuntimeError("ordinary close failure")
    async def noop(*args):
        pass
    async def scenario():
        registry = ToolRegistry()
        agent = LeadAgent(Model(), registry, ToolExecutor(registry), middlewares=[Cleanup()])
        state = ThreadState(user_id="alice", thread_id="thread")
        context = RuntimeContext("alice", "thread", "run", "/unused", noop, noop)
        with pytest.raises(StatePersistenceError) as caught:
            await agent.run(state, context)
        assert caught.value is original
    asyncio.run(scenario())
    assert PRIVATE_MARKER not in caplog.text
    assert "directory_sync" in caplog.text


@pytest.mark.parametrize("wrapped", [False, True])
def test_tool_abort_cleanup_preserves_original_and_redacts_storage_causes(caplog, wrapped):
    from app.agents.middleware import MiddlewareManager
    from app.agents.tool_calls import execute_tool_calls
    from app.domain.messages import ToolCall
    from app.domain.threads import ThreadState
    from app.runtime.context import RuntimeContext
    original = failure(wrapped=True)
    class Executor:
        async def execute(self, *args):
            raise original
    async def noop(*args):
        pass
    async def failed_save(*args):
        raise failure(wrapped)
    async def scenario():
        state = ThreadState(user_id="alice", thread_id="thread")
        context = RuntimeContext("alice", "thread", "run", "/unused", noop, failed_save)
        with pytest.raises(StatePersistenceError) as caught:
            await execute_tool_calls(state=state, context=context, calls=[ToolCall("call", "tool", {})],
                                     round_number=1, executor=Executor(), middleware=MiddlewareManager([]))
        assert caught.value is original
    asyncio.run(scenario())
    assert PRIVATE_MARKER not in caplog.text
    assert "directory_sync" in caplog.text
