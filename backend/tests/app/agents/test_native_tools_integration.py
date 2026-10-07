"""无 Docker/网络的原生工具循环与文件 checkpoint 集成。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.agents.lead_agent import LeadAgent
from app.agents.workspace_context_middleware import WorkspaceContextMiddleware
from app.domain.checkpoints import Checkpoint
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.domain.tools import ToolDefinition
from app.repositories.checkpoint_repository import CheckpointRepository
from app.runtime.context import RuntimeContext
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.file_checkpoint_service import FileCheckpointService
from app.services.run_coordinator import RunCoordinator
from app.services.run_service import RunService
from app.services.thread_service import ThreadService
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


def test_native_tool_loop_and_checkpoint_restore(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    thread = ThreadService().create_thread("alice")
    run = RunService().create_run("alice", thread.id, "mock")
    workspace = Path(thread.workspace_path)
    original = workspace.parent / "uploads" / "source.txt"
    original.write_bytes(b"before\r\n")
    state = ThreadState(thread.id, "alice", workspace_path=thread.workspace_path)
    checkpoint_service = FileCheckpointService()
    point = checkpoint_service.capture_turn(thread, run, state, "produce report")
    state.messages.append(Message(role="user", content="produce report"))
    events = []
    saved = []

    async def event(kind, payload):
        events.append((kind, deepcopy(payload)))

    async def save(current):
        saved.append(deepcopy(current))
        CheckpointRepository().save(Checkpoint(thread.id, run.id, len(saved), deepcopy(current)))

    context = RuntimeContext("alice", thread.id, run.id, thread.workspace_path, event, save)
    registry = RunCoordinator(MemoryStreamBridge(), bash_runner=None)._build_tool_registry()
    assert registry.get("write_file") is not None
    assert registry.get("bash") is None
    assert registry.get("web_search") is None
    calls = [
        ToolCall("create", "write_file", {"path": "outputs/report.txt", "content": "draft\n"}),
        ToolCall("refuse", "write_file", {"path": "outputs/report.txt", "content": "lost"}),
        ToolCall("edit", "edit_file", {"path": "/mnt/user-data/outputs/report.txt", "old_string": "draft", "new_string": "final"}),
        ToolCall("upload", "edit_file", {"path": "uploads/source.txt", "old_string": "before", "new_string": "after"}),
        ToolCall("read", "read_file", {"path": "outputs/report.txt"}),
        ToolCall("glob", "glob", {"path": "outputs", "pattern": "**/*.txt"}),
        ToolCall("grep", "grep", {"path": "outputs", "pattern": "final"}),
    ]

    class Model:
        turn = 0

        async def chat(self, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return Message(role="assistant", content="", tool_calls=calls)
            messages = {m.tool_call_id: m for m in kwargs["messages"] if m.role == "tool"}
            assert set(messages) == {call.id for call in calls}
            assert [name for name, result in messages.items() if result.is_error] == ["refuse"]
            assert messages["read"].content == "final\n"
            assert json.loads(messages["glob"].content)["files"] == ["outputs/report.txt"]
            matches = json.loads(messages["grep"].content)["matches"]
            assert len(matches) == 1 and matches[0]["path"] == "outputs/report.txt"
            return Message(role="assistant", content="done")

        async def close(self):
            pass

    asyncio.run(LeadAgent(Model(), registry, ToolExecutor(registry)).run(state, context))
    assert saved[-1].messages[-1].content == "done"
    ends = {payload["tool_call_id"]: payload for kind, payload in events if kind == "tool.end"}
    assert set(ends) == {call.id for call in calls}
    assert ends["refuse"]["is_error"] is True
    assert original.read_bytes() == b"after\r\n"
    preview = checkpoint_service.preview(thread, point["id"])
    assert "uploads/source.txt" in preview["modified"]
    assert "outputs/report.txt" in preview["deleted"]
    result = checkpoint_service.restore(thread, "native-restore", point["id"], preview["revision"], preview["fingerprint"])
    assert result["status"] == "committed"
    assert original.read_bytes() == b"before\r\n"
    assert not (workspace.parent / "outputs" / "report.txt").exists()


def test_tool_guidance_only_mentions_registered_capabilities(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = RuntimeContext("alice", "thread", "run", str(workspace), None, None)
    registry = ToolRegistry()

    class Search:
        definition = ToolDefinition("grep", "search", {})

    registry.register(Search())
    state = ThreadState("thread", "alice")
    asyncio.run(WorkspaceContextMiddleware(registry).before_agent(state, context))
    prompt = state.messages[0].content
    assert "grep" in prompt and "行号" in prompt
    assert "write_file" not in prompt and "edit_file" not in prompt and "read_file" not in prompt
    assert "bash" not in prompt and "web_search" not in prompt
    assert str(tmp_path) not in prompt


def test_native_commit_uncertainty_stops_runtime_without_model_retry(monkeypatch):
    from app.runtime.agent_runtime import AgentRuntime
    from app.runtime.errors import StatePersistenceError
    from app.storage.errors import StorageError
    from app.storage.file_io import AtomicWriter

    monkeypatch.setenv("TAVILY_API_KEY", "")
    thread = ThreadService().create_thread("alice")
    run = RunService().create_run("alice", thread.id, "mock")
    real_commit = AtomicWriter.commit

    def uncertain_commit(writer):
        real_commit(writer)
        if writer.name == "fatal.txt":
            raise StorageError(backend="file", operation="write", category="io",
                               stage="directory_sync", commit_state="uncertain", recovery="verify")

    monkeypatch.setattr(AtomicWriter, "commit", uncertain_commit)

    class Model:
        calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            assert self.calls == 1, "模型不应把持久化失败当作普通工具结果继续"
            return Message(role="assistant", content="", tool_calls=[
                ToolCall("fatal", "write_file", {"path": "outputs/fatal.txt", "content": "written"}),
            ])

        async def close(self):
            pass

    model = Model()
    registry = RunCoordinator(MemoryStreamBridge(), bash_runner=None)._build_tool_registry()
    agent = LeadAgent(model, registry, ToolExecutor(registry))

    async def scenario():
        bridge = MemoryStreamBridge()
        try:
            with pytest.raises(StatePersistenceError):
                await AgentRuntime(bridge, file_checkpoints=FileCheckpointService()).run(
                    "alice", thread.id, run.id, "write file", agent,
                )
        finally:
            await bridge.close()

    asyncio.run(scenario())
    assert model.calls == 1
    assert RunService().get_run(run.id, "alice").status == "error"
    # 替换可能已完成，但 Runtime 必须保留失败，不可重新执行或宣告成功。
    assert (Path(thread.workspace_path).parent / "outputs/fatal.txt").read_text() == "written"
