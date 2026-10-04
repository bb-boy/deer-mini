"""真实工具循环验证结果预算、完整落盘、分页读取及错误边界。"""

import asyncio
from copy import deepcopy

import pytest

from app.agents.lead_agent import LeadAgent
from app.agents.middleware import MiddlewareManager
from app.agents.middleware_stack import build_runtime_middlewares
from app.agents.tool_calls import execute_tool_calls
from app.domain.messages import Message, ToolCall
from app.domain.subagents import SubagentTask
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition, ToolResult
from app.filesystem.thread_paths import ThreadPaths
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_coordinator import RunCoordinator
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


def run_context(tmp_path):
    for name in ("workspace", "uploads", "outputs"):
        (tmp_path / name).mkdir(exist_ok=True)
    events, snapshots = [], []

    async def event(kind, payload):
        events.append((kind, deepcopy(payload)))

    async def save(state):
        snapshots.append(deepcopy(state))

    return RuntimeContext("user", "thread", "run", str(tmp_path / "workspace"), event, save), events, snapshots


@pytest.mark.parametrize("size", [49_999, 50_000, 50_001])
def test_single_result_threshold_counts_characters_and_preserves_full_text(tmp_path, size):
    context, events, snapshots = run_context(tmp_path)
    original = "汉" * size

    class Tool:
        definition = ToolDefinition("large", "large", {})

        async def execute(self, call, context):
            return ToolResult(call.id, call.name, original, is_error=True)

    class Model:
        calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Message(role="assistant", content="", tool_calls=[ToolCall("call", "large", {})])
            result = kwargs["messages"][-1]
            assert result.is_error and result.tool_call_id == "call"
            assert result.tool_result_file is not None if size > 50_000 else result.tool_result_file is None
            if size > 50_000:
                path = ThreadPaths(tmp_path).resolve_agent_path(result.tool_result_file)
                assert path.read_text(encoding="utf-8") == original
                assert result.content.endswith(original[:2000])
                assert len(result.content) < 3000 and result.tool_result_chars == size
            else:
                assert result.content == original
            return Message(role="assistant", content="done")

        async def close(self):
            pass

    registry = ToolRegistry()
    registry.register(Tool())
    state = ThreadState("thread", "user", messages=[Message(role="user", content="test")])
    asyncio.run(LeadAgent(Model(), registry, ToolExecutor(registry)).run(state, context))
    assert snapshots[-1].messages[-2].to_dict() == state.messages[-2].to_dict()
    end = next(payload for kind, payload in events if kind == "tool.end")
    assert end["tool_result_file"] == state.messages[-2].tool_result_file


@pytest.mark.parametrize("sizes, expected_spilled", [
    ([50_000] * 4, []),
    ([41_000, 42_000, 43_000, 44_000, 45_000], [4]),
    ([50_000, 50_000, 50_000, 50_000, 30_000], [0]),
])
def test_one_model_message_budget_selects_largest_result_after_all_calls(tmp_path, sizes, expected_spilled):
    context, events, _ = run_context(tmp_path)

    class Tool:
        definition = ToolDefinition("text", "text", {})

        async def execute(self, call, context):
            return ToolResult(call.id, call.name, str(call.arguments["index"]) * call.arguments["size"])

    class Model:
        calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Message(role="assistant", content="", tool_calls=[
                    ToolCall(str(index), "text", {"index": index, "size": size})
                    for index, size in enumerate(sizes)
                ])
            results = kwargs["messages"][-len(sizes):]
            assert sum(len(result.content) for result in results) <= 200_000
            assert [index for index, result in enumerate(results) if result.tool_result_file] == expected_spilled
            for index in expected_spilled:
                result = results[index]
                path = ThreadPaths(tmp_path).resolve_agent_path(result.tool_result_file)
                assert path.read_text() == str(index) * sizes[index]
                assert result.tool_result_chars == sizes[index]
            return Message(role="assistant", content="done")

        async def close(self):
            pass

    registry = ToolRegistry()
    registry.register(Tool())
    state = ThreadState("thread", "user", messages=[Message(role="user", content="test")])
    asyncio.run(LeadAgent(Model(), registry, ToolExecutor(registry)).run(state, context))
    ends = [payload for kind, payload in events if kind == "tool.end"]
    results = [message for message in state.messages if message.role == "tool"]
    assert len(ends) == len(results)
    assert sum(len(payload["content"]) for payload in ends) <= 200_000
    for payload, result in zip(ends, results, strict=True):
        assert payload["tool_call_id"] == result.tool_call_id
        assert payload["content"] == result.content
        assert payload["tool_result_file"] == result.tool_result_file


def test_restored_history_compacts_each_model_message_separately(tmp_path):
    context, _, snapshots = run_context(tmp_path)
    state = ThreadState("thread", "user")
    for round_number in range(2):
        calls = [ToolCall(f"{round_number}-{i}", "text", {}) for i in range(3)]
        state.messages.append(Message(role="assistant", content="", tool_calls=calls))
        state.messages.extend(Message(role="tool", content="x" * 50_000, tool_call_id=call.id) for call in calls)
    asyncio.run(MiddlewareManager(build_runtime_middlewares()).before_model(state, context))
    assert all(message.tool_result_file is None for message in state.messages)
    assert not snapshots  # 总历史 300k，但每条模型消息只有 150k。


def test_restored_oversized_history_is_saved_before_next_model_request(tmp_path):
    context, _, snapshots = run_context(tmp_path)
    state = ThreadState("thread", "user", messages=[
        Message(role="assistant", content="", tool_calls=[ToolCall("old", "text", {})]),
        Message(role="tool", content="x" * 60_000, tool_call_id="old"),
    ])
    manager = MiddlewareManager(build_runtime_middlewares())
    asyncio.run(manager.before_model(state, context))
    assert state.messages[-1].tool_result_file is not None
    assert snapshots[-1].to_dict() == state.to_dict()
    first_path = state.messages[-1].tool_result_file
    asyncio.run(manager.before_model(state, context))
    assert state.messages[-1].tool_result_file == first_path and len(snapshots) == 1


def test_many_previews_obey_total_budget_without_resaving_full_files(tmp_path):
    context, _, _ = run_context(tmp_path)
    calls = [ToolCall(str(index), "text", {}) for index in range(100)]
    state = ThreadState("thread", "user", messages=[Message(role="assistant", content="", tool_calls=calls)])
    state.messages.extend(Message(role="tool", content="x" * 50_001, tool_call_id=call.id) for call in calls)
    asyncio.run(MiddlewareManager(build_runtime_middlewares()).before_model(state, context))
    results = state.messages[1:]
    assert sum(len(message.content) for message in results) <= 200_000
    assert len(list((tmp_path / "workspace" / ".tool-results").glob("*.txt"))) == 100
    assert all(message.tool_result_chars == 50_001 for message in results)


def test_storage_failure_is_fatal_and_keeps_original_message(tmp_path, monkeypatch):
    from app.filesystem import tool_result_store

    context, _, _ = run_context(tmp_path)
    message = Message(role="tool", content="x" * 60_000, tool_call_id="call")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(tool_result_store, "save_text", fail)

    async def return_result():
        return message

    async def scenario():
        with pytest.raises(StatePersistenceError):
            await MiddlewareManager(build_runtime_middlewares()).wrap_tool_call(
                ThreadState("thread", "user"), context, ToolCall("call", "text", {}), return_result,
            )
        assert message.content == "x" * 60_000 and message.tool_result_file is None

    asyncio.run(scenario())


def test_storage_rejects_symlink_directory(tmp_path):
    context, _, _ = run_context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "workspace" / ".tool-results").symlink_to(outside)

    async def return_result():
        return Message(role="tool", content="x" * 60_000, tool_call_id="call")

    async def scenario():
        with pytest.raises(StatePersistenceError):
            await MiddlewareManager(build_runtime_middlewares()).wrap_tool_call(
                ThreadState("thread", "user"), context, ToolCall("call", "text", {}), return_result,
            )
    asyncio.run(scenario())
    assert not list(outside.iterdir())


@pytest.mark.parametrize("mode", ["lines", "encoding", "invalid", "missing"])
def test_registered_read_file_crosses_real_agent_middleware_and_returns_to_model(tmp_path, monkeypatch, caplog, mode):
    import logging
    context, _, _ = run_context(tmp_path)
    monkeypatch.setenv("TAVILY_API_KEY", "")
    registry = RunCoordinator(MemoryStreamBridge(), bash_runner=None)._build_tool_registry()
    schema = registry.get("read_file").definition.parameters
    assert {"path", "encoding", "base", "offset"} <= schema["properties"].keys()
    assert registry.get("read_tool_result") is not None
    path = tmp_path / "workspace" / "report.txt"
    args = {"path": "report.txt"}
    expected, is_error = "", False
    if mode == "lines":
        path.write_text("".join(f"第 {i} 行\n" for i in range(1, 21)))
        args.update(base=10, offset=5)
        expected = "".join(f"第 {i} 行\n" for i in range(10, 15))
    elif mode == "encoding":
        path.write_bytes("中文报告".encode("gb18030"))
        args["encoding"] = "gb18030"
        expected = "中文报告"
    elif mode == "invalid":
        args["base"] = 0
        expected, is_error = "参数格式错误", True
    else:
        expected, is_error = "不存在", True

    class Model:
        calls = 0
        closed = False

        async def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Message(role="assistant", content="", tool_calls=[ToolCall("read", "read_file", args)])
            message = kwargs["messages"][-1]
            assert message.is_error is is_error and message.tool_call_id == "read"
            assert expected in message.content
            return Message(role="assistant", content="done")

        async def close(self):
            self.closed = True

    model = Model()
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    state = ThreadState("thread", "user", messages=[Message(role="user", content="read")])
    asyncio.run(LeadAgent(model, registry, ToolExecutor(registry)).run(state, context))
    assert model.calls == 2 and model.closed
    assert "工具调用前 name=read_file" in caplog.text and "工具调用后 name=read_file" in caplog.text


@pytest.mark.parametrize("disk_failure", [False, True])
def test_cancelled_round_finalizes_aggregate_budget_before_checkpoint_and_sse(tmp_path, monkeypatch, disk_failure):
    context, events, snapshots = run_context(tmp_path)
    calls = [ToolCall(str(i), "text", {}) for i in range(4)] + [ToolCall("stop", "stop", {})]
    state = ThreadState("thread", "user", messages=[Message(role="assistant", content="", tool_calls=calls)])

    class Text:
        definition = ToolDefinition("text", "text", {})

        async def execute(self, call, context):
            return ToolResult(call.id, call.name, "x" * 50_000)

    class Stop:
        definition = ToolDefinition("stop", "stop", {})

        async def execute(self, call, context):
            raise asyncio.CancelledError("user stop")

    if disk_failure:
        from app.filesystem import tool_result_store

        def fail(*args):
            raise OSError("disk full")
        monkeypatch.setattr(tool_result_store, "save_text", fail)

    registry = ToolRegistry()
    registry.register(Text())
    registry.register(Stop())

    async def scenario():
        expected = StatePersistenceError if disk_failure else asyncio.CancelledError
        with pytest.raises(expected) as caught:
            await execute_tool_calls(
                state=state, context=context, calls=calls, round_number=1,
                executor=ToolExecutor(registry), middleware=MiddlewareManager(build_runtime_middlewares()),
            )
        if disk_failure:
            assert isinstance(caught.value.execution_error, asyncio.CancelledError)
            return
        results = state.messages[1:]
        assert sum(len(message.content) for message in results) <= 200_000
        assert results[0].tool_result_file is not None
        assert snapshots[-1].to_dict() == state.to_dict()
        ends = [payload for kind, payload in events if kind == "tool.end"]
        assert len(ends) == 5 and sum(len(payload["content"]) for payload in ends) <= 200_000
        assert ends[0]["tool_result_file"] == results[0].tool_result_file

    asyncio.run(scenario())


def test_cancelled_round_stores_recovered_completed_task_result(tmp_path):
    context, events, snapshots = run_context(tmp_path)
    call = ToolCall("task-call", "task", {})
    state = ThreadState("thread", "user", messages=[Message(role="assistant", content="", tool_calls=[call])])

    class CompletedThenCancelled:
        definition = ToolDefinition("task", "task", {})

        async def execute(self, call, context):
            task = SubagentTask(
                tool_call_id=call.id, user_id="user", thread_id="thread", run_id="run",
                description="done", prompt="done", status="completed", result="x" * 60_000,
            )
            state.subtasks[task.task_id] = task
            raise asyncio.CancelledError("stop before returning")

    registry = ToolRegistry()
    registry.register(CompletedThenCancelled())

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await execute_tool_calls(
                state=state, context=context, calls=[call], round_number=1,
                executor=ToolExecutor(registry), middleware=MiddlewareManager(build_runtime_middlewares()),
            )
        result = state.messages[-1]
        assert result.tool_result_file is not None and not result.is_error
        assert ThreadPaths(tmp_path).resolve_agent_path(result.tool_result_file).read_text() == "x" * 60_000
        assert snapshots[-1].to_dict() == state.to_dict()
        end = next(payload for kind, payload in events if kind == "tool.end")
        assert end["tool_result_file"] == result.tool_result_file

    asyncio.run(scenario())
