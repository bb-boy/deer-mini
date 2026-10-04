"""任务清单的历史兼容、完整替换形状和后台身份约束。"""

from copy import deepcopy

import pytest

from app.domain.threads import ThreadState
from app.domain.todos import TodoItem


def test_todo_state_round_trip_and_old_checkpoints_remain_readable():
    old = {"thread_id": "thread", "user_id": "user", "messages": [], "workspace_path": "/workspace"}
    restored = ThreadState.from_dict(old)
    assert restored.todos == []
    assert restored.todos_run_id is restored.todos_tool_call_id is None
    restored.todos = [TodoItem("查阅资料", "in_progress"), TodoItem("整理结论", "pending")]
    restored.todos_run_id, restored.todos_tool_call_id = "run", "call"
    serialized = restored.to_dict()
    assert ThreadState.from_dict(serialized).to_dict() == serialized
    copied = ThreadState.from_dict(serialized)
    copied.todos[0].status = "completed"
    assert restored.todos[0].status == "in_progress"
    assert serialized["todos"][0]["status"] == "in_progress"


@pytest.mark.parametrize("invalid", [
    None, "task", [], {}, {"content": "a"}, {"content": "", "status": "pending"},
    {"content": " \n ", "status": "pending"}, {"content": 1, "status": "pending"},
    {"content": "a", "status": "failed"}, {"content": "a", "status": []},
    {"content": "a", "status": "pending", "run_id": "model-chosen"},
])
def test_todo_items_reject_malformed_data(invalid):
    with pytest.raises(ValueError):
        TodoItem.from_dict(invalid)


@pytest.mark.parametrize("change", [
    {"todos": None}, {"todos": {}}, {"todos": ["bad"]},
    {"todos": [{"content": "a", "status": "pending"}]},
    {"todos_run_id": "run"}, {"todos_tool_call_id": "call"},
    {"todos_run_id": "", "todos_tool_call_id": "call"},
    {"todos_run_id": 1, "todos_tool_call_id": "call"},
])
def test_state_rejects_missing_identity_or_bad_plan(change):
    payload = ThreadState(user_id="user", thread_id="thread").to_dict()
    payload.update(deepcopy(change))
    with pytest.raises(ValueError):
        ThreadState.from_dict(payload)


def test_empty_plan_can_remember_its_confirmed_clear_operation():
    state = ThreadState(user_id="user", thread_id="thread", todos_run_id="run", todos_tool_call_id="clear")
    assert ThreadState.from_dict(state.to_dict()).todos_tool_call_id == "clear"
    state.todos = [{"content": "not a TodoItem", "status": "pending"}]
    with pytest.raises(ValueError):
        state.to_dict()
