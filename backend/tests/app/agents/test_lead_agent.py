"""LeadAgent 的“模型 → 工具 → 模型”闭环测试。"""

import asyncio
import os
from pathlib import Path

import pytest

from app.agents.lead_agent import LeadAgent
from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.model.base import TextDeltaHandler
from app.model.factory import ModelFactory
from app.runtime.context import RuntimeContext
from app.tools.executor import ToolExecutor
from app.tools.read_file import ReadFileTool
from app.tools.registry import ToolRegistry


class ScriptedToolCallingModel:
    """第一轮调用 read_file，第二轮收到工具结果后给出最终回答。"""

    def __init__(self) -> None:
        self.received_messages: list[list[Message]] = []
        self.received_tool_names: list[list[str]] = []
        self.close_called = False

    async def chat(
        self,
        messages: list[Message],
        tools,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        on_text_delta: TextDeltaHandler | None = None,
    ) -> Message:
        # 保存副本，避免随后 Agent 向同一个 state 追加消息影响断言。
        self.received_messages.append(
            [Message.from_dict(message.to_dict()) for message in messages]
        )
        self.received_tool_names.append([tool.name for tool in tools])

        if len(self.received_messages) == 1:
            if on_text_delta is not None:
                await on_text_delta("我先读取文件。")
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-read-report",
                        name="read_file",
                        arguments={"path": "report.txt"},
                    )
                ],
            )

        if len(self.received_messages) == 2:
            tool_message = messages[-1]
            assert tool_message.role == "tool"
            assert tool_message.tool_call_id == "call-read-report"
            assert tool_message.content == "项目代号：青鹿-728。负责人：小明。"

            if on_text_delta is not None:
                await on_text_delta("项目代号是青鹿-728，负责人是小明。")
            return Message(
                role="assistant",
                content="项目代号是青鹿-728，负责人是小明。",
            )

        raise AssertionError("Agent 不应进行第三次模型调用")

    async def close(self) -> None:
        self.close_called = True


class FailingModel:
    """模拟模型服务在第一轮请求时失败。"""

    def __init__(self) -> None:
        self.close_called = False

    async def chat(self, *args, **kwargs) -> Message:
        raise RuntimeError("模拟模型请求失败")

    async def close(self) -> None:
        self.close_called = True


def build_registry_and_executor() -> tuple[ToolRegistry, ToolExecutor]:
    """注册真实 read_file，并创建统一工具执行器。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    return registry, ToolExecutor(registry)


def build_recording_context(
    workspace_path: Path,
) -> tuple[RuntimeContext, list[RunEvent], list[Checkpoint]]:
    """创建只写内存的 RuntimeContext，记录事件和状态快照。"""
    events: list[RunEvent] = []
    checkpoints: list[Checkpoint] = []

    async def record_event(event_type, payload) -> RunEvent:
        event = RunEvent(
            run_id="run-agent-loop-test",
            thread_id="thread-agent-loop-test",
            event_type=event_type,
            payload=payload,
            sequence=len(events) + 1,
        )
        events.append(event)
        return event

    def save_checkpoint(state: ThreadState) -> Checkpoint:
        checkpoint = Checkpoint(
            thread_id=state.thread_id,
            run_id="run-agent-loop-test",
            step=len(checkpoints) + 1,
            # Checkpoint 必须保存当时的快照，不能继续引用可变的 state。
            state=ThreadState.from_dict(state.to_dict()),
        )
        checkpoints.append(checkpoint)
        return checkpoint

    context = RuntimeContext(
        user_id="agent-loop-test-user",
        thread_id="thread-agent-loop-test",
        run_id="run-agent-loop-test",
        workspace_path=str(workspace_path),
        record_event=record_event,
        save_checkpoint=save_checkpoint,
    )
    return context, events, checkpoints


def test_agent_loop_executes_real_tool_and_returns_result_to_model(
    tmp_path: Path,
) -> None:
    """模型的工具调用应真实执行，结果必须进入下一轮模型消息。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.txt").write_text(
        "项目代号：青鹿-728。负责人：小明。",
        encoding="utf-8",
    )

    model = ScriptedToolCallingModel()
    registry, executor = build_registry_and_executor()
    context, events, checkpoints = build_recording_context(workspace)
    initial_state = ThreadState(
        thread_id=context.thread_id,
        user_id=context.user_id,
        workspace_path=context.workspace_path,
        messages=[Message(role="user", content="读取 report.txt 后告诉我项目信息")],
    )
    agent = LeadAgent(model, registry, executor)

    final_state = asyncio.run(agent.run(initial_state, context))

    assert [message.role for message in final_state.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert final_state.messages[1].tool_calls[0].name == "read_file"
    assert final_state.messages[2].tool_call_id == "call-read-report"
    assert final_state.messages[2].content == "项目代号：青鹿-728。负责人：小明。"
    assert final_state.messages[-1].content == "项目代号是青鹿-728，负责人是小明。"

    # 第二次模型调用必须包含真实工具结果，这个断言就是 Agent Loop 的核心。
    assert len(model.received_messages) == 2
    assert [message.role for message in model.received_messages[1]] == [
        "user",
        "assistant",
        "tool",
    ]
    assert model.received_tool_names == [["read_file"], ["read_file"]]
    assert model.close_called is True

    assert [event.event_type for event in events] == [
        "text.delta",
        "tool.start",
        "tool.end",
        "text.delta",
    ]
    assert events[1].payload["tool_name"] == "read_file"
    assert events[2].payload["content"] == "项目代号：青鹿-728。负责人：小明。"

    # LeadAgent 保存“模型决定调用工具”和“工具执行完成”两个关键快照；
    # 最终回答由上层 AgentRuntime 再保存。
    assert [checkpoint.step for checkpoint in checkpoints] == [1, 2]
    assert [message.role for message in checkpoints[0].state.messages] == [
        "user",
        "assistant",
    ]
    assert [message.role for message in checkpoints[1].state.messages] == [
        "user",
        "assistant",
        "tool",
    ]


def test_agent_loop_closes_model_and_preserves_error(tmp_path: Path) -> None:
    """模型失败时必须释放连接，并把原异常交给 AgentRuntime。"""
    model = FailingModel()
    registry, executor = build_registry_and_executor()
    context, events, checkpoints = build_recording_context(tmp_path)
    state = ThreadState(
        thread_id=context.thread_id,
        user_id=context.user_id,
        workspace_path=context.workspace_path,
        messages=[Message(role="user", content="你好")],
    )

    with pytest.raises(RuntimeError, match="模拟模型请求失败"):
        asyncio.run(LeadAgent(model, registry, executor).run(state, context))

    assert model.close_called is True
    assert events == []
    assert checkpoints == []


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_AGENT_TEST") != "1",
    reason="设置 RUN_LIVE_AGENT_TEST=1 后才访问真实模型服务",
)
def test_agent_loop_with_live_model(tmp_path: Path) -> None:
    """可选：用真实 USTC 模型验证模型能自主调用 read_file。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.txt").write_text(
        "项目代号：青鹿-728。负责人：小明。",
        encoding="utf-8",
    )

    model = ModelFactory("ustc-deepseek-flash").create_chat_model()
    registry, executor = build_registry_and_executor()
    context, _, _ = build_recording_context(workspace)
    state = ThreadState(
        thread_id=context.thread_id,
        user_id=context.user_id,
        workspace_path=context.workspace_path,
        messages=[
            Message(
                role="user",
                content=(
                    "必须调用 read_file 读取 report.txt，不能猜测文件内容。"
                    "读取后，只回答项目代号和负责人。"
                ),
            )
        ],
    )
    agent = LeadAgent(model, registry, executor)

    async def run_with_timeout() -> ThreadState:
        return await asyncio.wait_for(
            agent.run(state, context),
            timeout=240,
        )

    final_state = asyncio.run(run_with_timeout())

    assert any(message.role == "tool" for message in final_state.messages)
    assert final_state.messages[-1].role == "assistant"
    assert "青鹿-728" in final_state.messages[-1].content
    assert "小明" in final_state.messages[-1].content
