"""子任务状态的独立性、身份关联与 JSON 恢复。"""

import json

import pytest

from app.domain.messages import Message, ToolCall
from app.domain.subagents import SubagentTask
from app.domain.threads import ThreadState


def make_task(**overrides) -> SubagentTask:
    values = {
        "tool_call_id": "call-parent", "user_id": "alice",
        "thread_id": "thread-1", "run_id": "run-1",
        "description": "查阅官方文档", "prompt": "读取官方文档并保留来源链接",
    }
    return SubagentTask(**(values | overrides))


def test_tasks_have_independent_ids_and_message_lists():
    first = make_task()
    second = make_task(run_id="run-2")
    first.messages.append(Message(role="user", content=first.prompt))

    # 同一个模型调用编号在另一轮重复出现，不能混成同一张工作单。
    assert first.tool_call_id == second.tool_call_id
    assert first.task_id != second.task_id
    assert first.status == second.status == "pending"
    assert second.messages == []


def test_full_child_exchange_roundtrips_without_entering_parent_messages():
    task = make_task(
        status="completed", result="结论与来源",
        messages=[
            Message(role="user", content="读取官方文档"),
            Message(role="assistant", content="", reasoning_content="先读取资料",
                    tool_calls=[ToolCall(
                        id="child-fetch", name="web_fetch",
                        arguments={"url": "https://docs.python.org/3/library/asyncio.html"},
                    )]),
            Message(role="tool", content="官方正文", tool_call_id="child-fetch"),
            Message(role="assistant", content="结论与来源"),
        ],
    )
    state = ThreadState(
        thread_id="thread-1", user_id="alice", workspace_path="/tmp/workspace",
        messages=[Message(role="user", content="请比较两份资料")],
        subtasks={task.task_id: task},
    )

    restored = ThreadState.from_dict(json.loads(json.dumps(state.to_dict())))
    child = restored.subtasks[task.task_id]

    assert restored.to_dict() == state.to_dict()
    assert [message.content for message in restored.messages] == ["请比较两份资料"]
    assert child.tool_call_id == "call-parent"
    assert child.messages[1].tool_calls[0].arguments["url"].endswith("asyncio.html")
    assert child.messages[1].reasoning_content == "先读取资料"
    assert child.messages[2].tool_call_id == "child-fetch"
    assert child.messages[2].content == "官方正文"
    child.messages[1].tool_calls[0].arguments["url"] = "changed-after-load"
    assert task.messages[1].tool_calls[0].arguments["url"].startswith("https://")


def test_records_from_different_runs_keep_their_own_status_and_result():
    first = make_task(status="completed", result="上轮结论")
    second = make_task(run_id="run-2", status="failed", error="读取失败")
    state = ThreadState(
        thread_id="thread-1", user_id="alice",
        subtasks={first.task_id: first, second.task_id: second},
    )
    restored = ThreadState.from_dict(json.loads(json.dumps(state.to_dict())))

    assert restored.subtasks[first.task_id].run_id == "run-1"
    assert restored.subtasks[first.task_id].result == "上轮结论"
    assert restored.subtasks[second.task_id].run_id == "run-2"
    assert restored.subtasks[second.task_id].status == "failed"
    assert restored.subtasks[second.task_id].error == "读取失败"


def test_legacy_states_restore_empty_independent_subtask_dictionaries():
    old = {
        "thread_id": "thread-1", "user_id": "alice", "workspace_path": None,
        "messages": [Message(role="user", content="旧对话").to_dict()],
    }
    first = ThreadState.from_dict(old)
    second = ThreadState.from_dict(old)
    task = make_task()
    first.subtasks[task.task_id] = task

    assert second.subtasks == {}
    assert second.messages[0].content == "旧对话"
    assert second.to_dict()["subtasks"] == {}


@pytest.mark.parametrize("overrides", [{"user_id": "bob"}, {"thread_id": "thread-2"}])
def test_tasks_cannot_be_attached_to_another_owner_or_thread(overrides):
    task = make_task(**overrides)
    with pytest.raises(ValueError, match="当前用户和 Thread"):
        ThreadState(thread_id="thread-1", user_id="alice", subtasks={task.task_id: task})


def test_subtask_dictionary_key_must_match_the_saved_execution_id():
    task = make_task()
    with pytest.raises(ValueError, match="字典键必须与 task_id 一致"):
        ThreadState(thread_id="thread-1", user_id="alice", subtasks={"wrong-id": task})


def test_subtask_ownership_is_rechecked_before_serialization():
    task = make_task()
    state = ThreadState(thread_id="thread-1", user_id="alice", subtasks={task.task_id: task})
    task.user_id = "bob"
    with pytest.raises(ValueError, match="当前用户和 Thread"):
        state.to_dict()


def test_invalid_status_is_rejected_when_loading_or_saving():
    task = make_task()
    data = task.to_dict()
    data["status"] = "unknown"
    with pytest.raises(ValueError, match="无效的子任务状态"):
        SubagentTask.from_dict(data)
    task.status = "unknown"
    with pytest.raises(ValueError, match="无效的子任务状态"):
        task.to_dict()


@pytest.mark.parametrize("raw_subtasks", [None, []])
def test_malformed_subtasks_are_not_silently_treated_as_an_old_checkpoint(raw_subtasks):
    with pytest.raises(ValueError, match="subtasks 必须是字典"):
        ThreadState.from_dict({
            "thread_id": "thread-1", "user_id": "alice", "workspace_path": None,
            "subtasks": raw_subtasks,
        })
