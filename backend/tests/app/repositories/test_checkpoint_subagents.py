"""通过真实临时 SQLite 保存、恢复完整父子任务状态。"""

import json

import pytest

from app.domain.checkpoints import Checkpoint
from app.domain.messages import Message, ToolCall
from app.domain.subagents import SubagentTask
from app.domain.threads import ThreadState
from app.infrastructure import database
from app.repositories.checkpoint_repository import CheckpointRepository
from app.services import thread_service
from app.services.run_service import RunService
from app.services.thread_service import ThreadService


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "subtasks.db"))
    monkeypatch.setattr(thread_service, "DATA_ROOT", tmp_path / "users")
    database.initialize_database()
    thread = ThreadService().create_thread("alice", "子任务快照")
    run = RunService().create_run(thread.user_id, thread.id, "test-model")
    return thread, run, CheckpointRepository()


def make_state(thread, run):
    child = SubagentTask(
        tool_call_id="parent-call", user_id=thread.user_id,
        thread_id=thread.id, run_id=run.id,
        description="读取文档", prompt="读取官方文档并保留来源",
        messages=[Message(role="user", content="读取官方文档并保留来源")],
    )
    sibling = SubagentTask(
        tool_call_id="sibling-call", user_id=thread.user_id,
        thread_id=thread.id, run_id=run.id,
        description="另一项调研", prompt="读取另一份资料",
        status="failed", error="页面无法读取",
    )
    state = ThreadState(
        thread_id=thread.id, user_id=thread.user_id,
        workspace_path=thread.workspace_path,
        messages=[Message(role="user", content="比较两个资料来源")],
        subtasks={child.task_id: child, sibling.task_id: sibling},
    )
    return state, child, sibling


def test_each_checkpoint_preserves_parent_and_all_child_steps(storage):
    thread, run, repository = storage
    state, child, sibling = make_state(thread, run)
    repository.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=1, state=state))

    child.status = "running"
    child.messages.extend([
        Message(role="assistant", content="", tool_calls=[ToolCall(
            id="fetch-call", name="web_fetch", arguments={"url": "https://example.org/docs"},
        )]),
        Message(role="tool", content="读取到的正文", tool_call_id="fetch-call"),
    ])
    repository.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=2, state=state))

    child.status = "completed"
    child.result = "结论与来源：https://example.org/docs"
    child.messages.append(Message(role="assistant", content=child.result))
    repository.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=3, state=state))

    # 新 Repository 从磁盘重读，避免仅检查同一个内存对象。
    reader = CheckpointRepository()
    history = reader.history(thread.id, thread.user_id, run.id)
    assert [item.step for item in history] == [1, 2, 3]
    assert [item.state.subtasks[child.task_id].status for item in history] == [
        "pending", "running", "completed",
    ]
    assert [len(item.state.subtasks[child.task_id].messages) for item in history] == [1, 3, 4]
    assert history[0].state.subtasks[child.task_id].result is None
    for item in history:
        assert item.state.messages[0].content == "比较两个资料来源"
        assert len(item.state.messages) == 1
        assert item.state.subtasks[sibling.task_id].error == "页面无法读取"

    latest = reader.latest(thread.id, thread.user_id)
    by_run = reader.latest_for_run(thread.id, run.id, thread.user_id)
    assert latest is not None and by_run is not None
    assert latest.state.to_dict() == by_run.state.to_dict() == state.to_dict()
    assert latest.state.subtasks[child.task_id].messages[1].tool_calls[0].id == "fetch-call"
    assert latest.state.subtasks[child.task_id].messages[2].tool_call_id == "fetch-call"
    assert RunService().get_run(run.id, thread.user_id).status == "pending"
    assert reader.latest(thread.id, "bob") is None
    assert reader.latest_for_run(thread.id, run.id, "bob") is None
    assert reader.history(thread.id, "bob", run.id) == []


def test_old_sqlite_checkpoint_can_be_loaded_and_extended_without_migration(storage):
    thread, run, repository = storage
    state, child, _ = make_state(thread, run)
    legacy_payload = state.to_dict()
    legacy_payload.pop("subtasks")
    checkpoint = repository.save(Checkpoint(
        thread_id=thread.id, run_id=run.id, step=1,
        state=ThreadState.from_dict(legacy_payload),
    ))
    with database.connect() as connection:
        connection.execute(
            "UPDATE checkpoints SET state_json = ? WHERE id = ?",
            (json.dumps(legacy_payload), checkpoint.id),
        )

    restored = repository.latest(thread.id, thread.user_id)
    assert restored is not None
    assert restored.state.subtasks == {}
    assert restored.state.messages[0].content == "比较两个资料来源"
    restored.state.subtasks[child.task_id] = child
    repository.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=2, state=restored.state))
    history = repository.history(thread.id, thread.user_id, run.id)
    assert history[0].state.subtasks == {}
    assert history[1].state.subtasks[child.task_id].to_dict() == child.to_dict()


def test_invalid_child_ownership_does_not_reach_sqlite(storage):
    thread, run, repository = storage
    state, child, _ = make_state(thread, run)
    child.thread_id = "another-thread"

    with pytest.raises(ValueError, match="当前用户和 Thread"):
        repository.save(Checkpoint(thread_id=thread.id, run_id=run.id, step=1, state=state))
    assert repository.history(thread.id, thread.user_id, run.id) == []
