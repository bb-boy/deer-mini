"""使用真实模型验证 deer_mini 的完整 AgentLoop。"""

import asyncio
from pathlib import Path

from app.agents.lead_agent import LeadAgent
from app.infrastructure.database import initialize_database
from app.model.factory import ModelFactory
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.events_repository import EventRepository
from app.runtime.agent_runtime import AgentRuntime
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_service import RunService
from app.services.thread_service import ThreadService
from app.tools.executor import ToolExecutor
from app.tools.read_file import ReadFileTool
from app.tools.registry import ToolRegistry


async def main() -> None:
    """
    创建独立 Thread 和真实文件，让模型自动调用 read_file。

    本脚本会访问真实模型服务，并在 SQLite 留下本次 Run 的记录，
    方便随后检查 Checkpoint 和 RunEvent。
    """
    initialize_database()

    user_id = "agent-loop-manual-check"
    thread = ThreadService().create_thread(user_id, "AgentLoop 真实模型验证")
    workspace = Path(thread.workspace_path)
    (workspace / "report.txt").write_text(
        "项目代号：青鹿-728。负责人：小明。\n",
        encoding="utf-8",
    )

    run = RunService().create_run(
        user_id=user_id,
        thread_id=thread.id,
        model_name="ustc-deepseek-flash",
        thinking_enabled=False,
    )

    registry = ToolRegistry()
    registry.register(ReadFileTool())
    agent = LeadAgent(
        model=ModelFactory(run.model_name).create_chat_model(),
        tool_registry=registry,
        tool_executor=ToolExecutor(registry),
        thinking_enabled=run.thinking_enabled,
        reasoning_effort=run.reasoning_effort,
    )

    final_state = await AgentRuntime(MemoryStreamBridge()).run(
        user_id=user_id,
        thread_id=thread.id,
        run_id=run.id,
        user_message=(
            "必须调用 read_file 读取 report.txt，不能猜测文件内容。"
            "读取后，只回答项目代号和负责人。"
        ),
        agent=agent,
    )

    tool_call_message = next(
        message
        for message in final_state.messages
        if message.role == "assistant" and message.tool_calls
    )
    tool_message = next(
        message for message in final_state.messages if message.role == "tool"
    )
    final_answer = final_state.messages[-1]

    assert tool_call_message.tool_calls[0].name == "read_file"
    assert tool_message.tool_call_id == tool_call_message.tool_calls[0].id
    assert final_answer.role == "assistant"
    assert "青鹿-728" in final_answer.content
    assert "小明" in final_answer.content

    checkpoints = CheckpointRepository().history(thread.id, user_id, run.id)
    events = EventRepository().list_for_run(thread.id, run.id, user_id)
    assert len(checkpoints) >= 4
    assert any(event.event_type == "tool.start" for event in events)
    assert any(event.event_type == "tool.end" for event in events)

    print(f"Thread: {thread.id}")
    print(f"Run: {run.id}")
    print("最终回答：", final_answer.content)
    print("Checkpoint steps:", [checkpoint.step for checkpoint in checkpoints])
    print("Run events:", [event.event_type for event in events])
    print("PASS: 真实模型已自动调用 read_file，并完成完整 AgentLoop。")


if __name__ == "__main__":
    asyncio.run(main())
