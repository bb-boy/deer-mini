"""真实 Coordinator/Runtime/主子循环验证新版读取、全文落盘与读回。"""

import asyncio
from pathlib import Path

import pytest

from app.domain.messages import Message, ToolCall
from app.filesystem.thread_paths import ThreadPaths
from app.infrastructure import database
from app.model.factory import ModelFactory
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.run_repository import RunRepository
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_coordinator import RunCoordinator
from app.services.thread_service import ThreadService


@pytest.mark.parametrize("mode", ["lines", "large", "child_large"])
def test_runtime_reads_lines_or_recovers_full_large_result(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("DEER_MINI_DATABASE_PATH", str(tmp_path / "flow.db"))
    monkeypatch.setenv("DEER_MINI_DATA_ROOT", str(tmp_path / "users"))
    database.initialize_database()
    thread = ThreadService().create_thread("flow-user", "读取验证")
    text = ("0123456789汉🙂" * 5000) if mode != "lines" else "".join(f"第 {i} 行\n" for i in range(1, 21))
    (Path(thread.workspace_path) / "report.txt").write_text(text, encoding="utf-8")
    models = []

    class Reader:
        calls = 0
        closed = 0

        async def chat(self, messages, tools, **kwargs):
            self.calls += 1
            system_messages = [message.content for message in messages if message.role == "system"]
            assert system_messages
            assert all(thread.workspace_path not in content for content in system_messages)
            assert any("/mnt/user-data/workspace" in content for content in system_messages)
            assert "read_tool_result" in {tool.name for tool in tools}
            if self.calls == 1:
                args = {"path": "report.txt"}
                if mode == "lines":
                    args.update(base=10, offset=5)
                return Message(role="assistant", content="", tool_calls=[ToolCall("read", "read_file", args)])
            result = messages[-1]
            assert result.role == "tool" and not result.is_error
            if mode == "lines":
                assert result.content == "".join(f"第 {i} 行\n" for i in range(10, 15))
                return Message(role="assistant", content="读取了第10至14行")
            if self.calls == 2:
                assert result.tool_result_chars == len(text)
                paths = ThreadPaths(Path(thread.workspace_path).parent)
                assert paths.resolve_agent_path(result.tool_result_file).read_text() == text
                return Message(role="assistant", content="", tool_calls=[ToolCall("page", "read_tool_result", {
                    "path": result.tool_result_file, "offset": 2000, "limit": 2000,
                })])
            assert result.content == text[2000:4000]
            return Message(role="assistant", content="已读回全文片段")

        async def close(self):
            self.closed += 1

    class Parent(Reader):
        async def chat(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Message(role="assistant", content="", tool_calls=[ToolCall("delegate", "task", {
                    "description": "读取报告", "prompt": "读取 report.txt 并检查后续内容",
                    "subagent_type": "general-purpose",
                })])
            assert messages[-1].content == "已读回全文片段"
            return Message(role="assistant", content="已收到子 Agent 的读取结果")

    def factory(_factory):
        model = Parent() if mode == "child_large" and not models else Reader()
        models.append(model)
        return model

    monkeypatch.setattr(ModelFactory, "create_chat_model", factory)

    async def scenario():
        bridge = MemoryStreamBridge()
        coordinator = RunCoordinator(bridge, bash_runner=None)
        try:
            run = await coordinator.create_and_start_run(
                user_id=thread.user_id, thread_id=thread.id, message="读取报告",
                model_name="fake", thinking_enabled=False, reasoning_effort=None,
            )
            state = await coordinator._tasks[run.id]
            assert RunRepository().get(run.id, thread.user_id).status == "success"
            restored = CheckpointRepository().latest(thread.id, thread.user_id).state
            assert restored.to_dict() == state.to_dict()
            assert all(model.closed == 1 for model in models)
            if mode == "child_large":
                task = next(iter(state.subtasks.values()))
                assert task.status == "completed"
                assert next(message for message in task.messages if message.role == "tool").tool_result_file is not None
            elif mode == "large":
                assert next(message for message in state.messages if message.role == "tool").tool_result_file is not None
        finally:
            await coordinator.shutdown()
            await bridge.close()

    asyncio.run(scenario())
