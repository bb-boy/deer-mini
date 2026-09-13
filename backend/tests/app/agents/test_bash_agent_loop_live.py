"""真实 Docker Bash 与 Agent Loop 的可选集成测试。"""

import asyncio
import os
from pathlib import Path
import subprocess

import pytest

from app.agents.lead_agent import LeadAgent
from app.domain.checkpoints import Checkpoint
from app.domain.events import RunEvent
from app.domain.messages import Message, ToolCall
from app.domain.threads import ThreadState
from app.model.base import TextDeltaHandler
from app.model.factory import ModelFactory
from app.runtime.context import RuntimeContext
from app.sandbox.docker_runner import DockerCommandRunner, DockerRunnerConfig
from app.tools.bash import BashTool
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_SANDBOX_TEST") != "1",
    reason="设置 RUN_DOCKER_SANDBOX_TEST=1 后才启动真实 Docker 容器",
)


class RecordingNameRunner(DockerCommandRunner):
    """记录随机容器名，供测试确认超时清理完成。"""

    last_container_name: str | None = None

    def build_run_args(self, **kwargs) -> tuple[str, list[str]]:
        container_name, args = super().build_run_args(**kwargs)
        self.last_container_name = container_name
        return container_name, args


def _build_context(workspace: Path) -> RuntimeContext:
    events: list[RunEvent] = []
    checkpoints: list[Checkpoint] = []

    async def record_event(event_type, payload) -> RunEvent:
        event = RunEvent(
            run_id="run-bash-live",
            thread_id="thread-bash-live",
            event_type=event_type,
            payload=payload,
            sequence=len(events) + 1,
        )
        events.append(event)
        return event

    async def save_checkpoint(state: ThreadState) -> Checkpoint:
        checkpoint = Checkpoint(
            thread_id=state.thread_id,
            run_id="run-bash-live",
            step=len(checkpoints) + 1,
            state=ThreadState.from_dict(state.to_dict()),
        )
        checkpoints.append(checkpoint)
        return checkpoint

    return RuntimeContext(
        user_id="bash-live-user",
        thread_id="thread-bash-live",
        run_id="run-bash-live",
        workspace_path=str(workspace),
        record_event=record_event,
        save_checkpoint=save_checkpoint,
    )


def _build_bash_agent(model) -> LeadAgent:
    runner = DockerCommandRunner(
        DockerRunnerConfig(timeout_seconds=20, network_enabled=False)
    )
    registry = ToolRegistry()
    registry.register(BashTool(runner))
    return LeadAgent(model, registry, ToolExecutor(registry))


class ScriptedBashModel:
    """第一轮调用 Bash，第二轮根据真实容器输出回答。"""

    def __init__(self) -> None:
        self.call_count = 0
        self.close_called = False

    async def chat(
        self,
        messages: list[Message],
        tools,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        on_text_delta: TextDeltaHandler | None = None,
        on_reasoning_delta: TextDeltaHandler | None = None,
    ) -> Message:
        self.call_count += 1
        if self.call_count == 1:
            assert [tool.name for tool in tools] == ["bash"]
            return Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-real-docker",
                        name="bash",
                        arguments={
                            "description": "创建结果文件并读取内容",
                            "command": (
                                "printf 'docker-agent-728' > result.txt "
                                "&& cat result.txt"
                            ),
                        },
                    )
                ],
            )

        assert messages[-1].role == "tool"
        assert messages[-1].content == "docker-agent-728"
        return Message(role="assistant", content="已得到 docker-agent-728")

    async def close(self) -> None:
        self.close_called = True


def test_agent_loop_returns_real_docker_output_to_model(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace,with-comma"
    workspace.mkdir()
    context = _build_context(workspace)
    model = ScriptedBashModel()
    state = ThreadState(
        thread_id=context.thread_id,
        user_id=context.user_id,
        workspace_path=context.workspace_path,
        messages=[Message(role="user", content="创建并检查 result.txt")],
    )

    final_state = asyncio.run(_build_bash_agent(model).run(state, context))

    assert (workspace / "result.txt").read_text() == "docker-agent-728"
    assert [message.role for message in final_state.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert final_state.messages[2].content == "docker-agent-728"
    assert final_state.messages[-1].content == "已得到 docker-agent-728"
    assert model.close_called is True


def test_real_docker_timeout_removes_container(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = RecordingNameRunner(
        DockerRunnerConfig(timeout_seconds=0.5, network_enabled=False)
    )

    result = asyncio.run(
        runner.run(
            command="printf started; sleep 60",
            workspace_path=str(workspace),
            run_id="timeout-live",
            tool_call_id="call-timeout-live",
        )
    )

    assert result.timed_out is True
    assert runner.last_container_name is not None
    remaining = subprocess.run(
        [
            runner.config.docker_binary,
            "ps",
            "-a",
            "--filter",
            f"name=^{runner.last_container_name}$",
            "--format",
            "{{.Names}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert remaining.stdout == ""


def test_real_docker_cancel_removes_container(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = RecordingNameRunner(
        DockerRunnerConfig(timeout_seconds=20, network_enabled=False)
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            runner.run(
                command="printf started; sleep 60",
                workspace_path=str(workspace),
                run_id="cancel-live",
                tool_call_id="call-cancel-live",
            )
        )
        for _ in range(100):
            if runner.last_container_name is not None:
                inspected = subprocess.run(
                    ["docker", "inspect", runner.last_container_name],
                    capture_output=True,
                )
                if inspected.returncode == 0:
                    break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("真实 Docker 容器没有在预期时间内启动")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    remaining = subprocess.run(
        [
            runner.config.docker_binary,
            "ps",
            "-a",
            "--filter",
            f"name=^{runner.last_container_name}$",
            "--format",
            "{{.Names}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert remaining.stdout == ""


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_AGENT_TEST") != "1",
    reason="设置 RUN_LIVE_AGENT_TEST=1 后才访问真实模型服务",
)
def test_live_model_can_choose_real_docker_bash(tmp_path: Path) -> None:
    """真实模型必须选择 Bash，并依据真实容器结果回答。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = _build_context(workspace)
    state = ThreadState(
        thread_id=context.thread_id,
        user_id=context.user_id,
        workspace_path=context.workspace_path,
        messages=[
            Message(
                role="user",
                content=(
                    "必须调用 bash 工具执行下面的任务，不能自行猜测结果："
                    "用 printf 把 bash-live-936 写入 live.txt，再用 cat 输出它。"
                    "工具完成后，只回答文件中的字符串。"
                ),
            )
        ],
    )
    model = ModelFactory("ustc-deepseek-flash").create_chat_model()

    async def run_with_timeout() -> ThreadState:
        return await asyncio.wait_for(
            _build_bash_agent(model).run(state, context),
            timeout=240,
        )

    final_state = asyncio.run(run_with_timeout())

    tool_messages = [
        message for message in final_state.messages if message.role == "tool"
    ]
    assert tool_messages
    assert "bash-live-936" in tool_messages[-1].content
    assert (workspace / "live.txt").read_text() == "bash-live-936"
    assert final_state.messages[-1].role == "assistant"
    assert "bash-live-936" in final_state.messages[-1].content
