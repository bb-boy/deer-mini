"""AgentRuntime 的完整生命周期测试。"""

import asyncio
from pathlib import Path

import pytest

from app.domain.messages import Message
from app.infrastructure import database
from app.repositories.events_repository import EventRepository
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository
from app.runtime.agent_runtime import AgentRuntime
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services import thread_service
from app.services.run_service import RunService
from app.services.thread_service import ThreadService


@pytest.fixture
def isolated_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """让每个测试使用独立 SQLite 文件和独立 Thread 工作目录。"""
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "deer_mini.db")
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()


class SuccessfulAgent:
    """仅用于验证 Runtime 生命周期的测试 Agent，不是最终 Agent。"""

    async def run(self, state, context):
        # 模拟 Agent 向用户流式报告一段文字。
        await context.record_event(
            "text.delta",
            {"text": "我已处理这条用户消息"},
        )

        # 模拟 Agent 把最终回答放进完整对话状态。
        state.messages.append(
            Message(role="assistant", content="我已处理这条用户消息")
        )
        return state


class FailingAgent:
    """仅用于验证 Runtime 的错误收尾。"""

    async def run(self, state, context):
        raise RuntimeError("模拟模型请求失败")


def create_thread_and_run(user_id: str):
    """创建一组真实且属于同一用户的 Thread 与 pending Run。"""
    thread = ThreadService().create_thread(user_id, "runtime test")
    run = RunService().create_run(user_id, thread.id, "demo-model")
    return thread, run


def test_run_saves_state_records_events_and_ends_stream(
    isolated_storage,
) -> None:
    """成功时应保存状态、记录事件、结束 Run 和结束 Stream。"""
    user_id = "alice"
    thread, run = create_thread_and_run(user_id)
    bridge = MemoryStreamBridge()
    runtime = AgentRuntime(bridge)

    async def execute_and_collect():
        final_state = await runtime.run(
            user_id=user_id,
            thread_id=thread.id,
            run_id=run.id,
            user_message="读取 report.txt",
            agent=SuccessfulAgent(),
        )

        # publish_end 已执行；订阅会读完缓存事件后自然结束。
        streamed_events = []
        async for stream_event in bridge.subscribe(run.id):
            streamed_events.append(stream_event)

        return final_state, streamed_events

    final_state, streamed_events = asyncio.run(execute_and_collect())

    saved_run = RunRepository().get(run.id, user_id)
    saved_thread = ThreadRepository().get(thread.id, user_id)
    checkpoints = runtime._checkpoint_repository.history(
        thread.id,
        user_id,
        run.id,
    )
    events = EventRepository().list_for_run(thread.id, run.id, user_id)

    # Run 完成后，Run 应成功，Thread 应恢复空闲。
    assert saved_run is not None
    assert saved_run.status == "success"
    assert saved_thread is not None
    assert saved_thread.status == "idle"

    # 第一份快照保存用户消息，第二份快照保存 Agent 最终回答。
    assert [checkpoint.step for checkpoint in checkpoints] == [1, 2]
    assert [message.content for message in final_state.messages] == [
        "读取 report.txt",
        "我已处理这条用户消息",
    ]

    # 业务事件会持久化到 SQLite。
    assert [event.event_type for event in events] == [
        "run.start",
        "text.delta",
        "run.end",
    ]

    # metadata 只存在于 Stream；RunEvent 会包装为 run_event。
    assert [event.event for event in streamed_events] == [
        "run_event",
        "metadata",
        "run_event",
        "run_event",
    ]


def test_run_records_error_and_ends_stream_when_agent_fails(
    isolated_storage,
) -> None:
    """Agent 报错时应记录 run.error、结束 Run 和结束 Stream。"""
    user_id = "alice"
    thread, run = create_thread_and_run(user_id)
    bridge = MemoryStreamBridge()
    runtime = AgentRuntime(bridge)

    async def execute_and_collect():
        with pytest.raises(RuntimeError, match="模拟模型请求失败"):
            await runtime.run(
                user_id=user_id,
                thread_id=thread.id,
                run_id=run.id,
                user_message="读取 report.txt",
                agent=FailingAgent(),
            )

        streamed_events = []
        async for stream_event in bridge.subscribe(run.id):
            streamed_events.append(stream_event)

        return streamed_events

    streamed_events = asyncio.run(execute_and_collect())

    saved_run = RunRepository().get(run.id, user_id)
    saved_thread = ThreadRepository().get(thread.id, user_id)
    events = EventRepository().list_for_run(thread.id, run.id, user_id)

    assert saved_run is not None
    assert saved_run.status == "error"
    assert saved_run.error == "模拟模型请求失败"
    assert saved_thread is not None
    assert saved_thread.status == "idle"
    assert [event.event_type for event in events] == ["run.start", "run.error"]
    assert bridge._get_or_create_stream(run.id).ended is True
    assert [event.event for event in streamed_events] == [
        "run_event",
        "metadata",
        "run_event",
    ]
