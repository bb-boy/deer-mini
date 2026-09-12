"""BashTool 的工具协议和结果转换测试。"""

import asyncio
from pathlib import Path

import pytest

from app.domain.messages import ToolCall
from app.runtime.context import RuntimeContext
from app.sandbox.base import CommandResult
from app.tools.bash import BashTool


class FakeCommandRunner:
    def __init__(self, result: CommandResult | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, str]] = []

    async def run(self, **kwargs) -> CommandResult:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_context(workspace: Path) -> RuntimeContext:
    return RuntimeContext(
        user_id="alice",
        thread_id="thread-1",
        run_id="run-1",
        workspace_path=str(workspace),
        record_event=None,
        save_checkpoint=None,
    )


def make_call(**arguments) -> ToolCall:
    return ToolCall(id="call-1", name="bash", arguments=arguments)


def test_bash_tool_definition_requires_description_and_command():
    tool = BashTool(FakeCommandRunner(CommandResult("", 0)))

    assert tool.definition.name == "bash"
    assert set(tool.definition.parameters["properties"]) == {
        "description",
        "command",
    }
    assert tool.definition.parameters["required"] == ["description", "command"]
    assert tool.definition.parameters["additionalProperties"] is False


def test_bash_tool_runs_command_in_runtime_workspace(tmp_path: Path):
    runner = FakeCommandRunner(CommandResult("42 report.csv\n", 0))
    tool = BashTool(runner)

    result = asyncio.run(
        tool.execute(
            make_call(description="统计行数", command="wc -l report.csv"),
            make_context(tmp_path),
        )
    )

    assert result.is_error is False
    assert result.content == "42 report.csv\n"
    assert runner.calls == [
        {
            "command": "wc -l report.csv",
            "workspace_path": str(tmp_path),
            "user_id": "alice",
            "thread_id": "thread-1",
            "run_id": "run-1",
            "tool_call_id": "call-1",
        }
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"description": "", "command": "pwd"}, "执行说明不能为空"),
        ({"description": "查看目录", "command": ""}, "Bash 命令不能为空"),
        ({"description": "查看目录", "command": 123}, "Bash 命令必须是字符串"),
    ],
)
def test_bash_tool_rejects_invalid_arguments(
    tmp_path: Path,
    arguments,
    message: str,
):
    runner = FakeCommandRunner(CommandResult("should not run", 0))

    result = asyncio.run(
        BashTool(runner).execute(make_call(**arguments), make_context(tmp_path))
    )

    assert result.is_error is True
    assert result.content == message
    assert runner.calls == []


def test_bash_tool_marks_nonzero_exit_as_error(tmp_path: Path):
    runner = FakeCommandRunner(CommandResult("command failed\n", 7))

    result = asyncio.run(
        BashTool(runner).execute(
            make_call(description="运行检查", command="false"),
            make_context(tmp_path),
        )
    )

    assert result.is_error is True
    assert result.content == "command failed\n\n[Exit Code: 7]"


def test_bash_tool_marks_timeout_as_error(tmp_path: Path):
    runner = FakeCommandRunner(
        CommandResult("started\n", exit_code=None, timed_out=True)
    )

    result = asyncio.run(
        BashTool(runner).execute(
            make_call(description="等待", command="sleep 60"),
            make_context(tmp_path),
        )
    )

    assert result.is_error is True
    assert result.content == "started\n\n[命令执行超时，容器已停止]"


def test_bash_tool_turns_runner_failure_into_tool_error(tmp_path: Path):
    runner = FakeCommandRunner(RuntimeError("Docker daemon unavailable"))

    result = asyncio.run(
        BashTool(runner).execute(
            make_call(description="查看目录", command="pwd"),
            make_context(tmp_path),
        )
    )

    assert result.is_error is True
    assert result.content == "Bash 执行失败：Docker daemon unavailable"


def test_bash_tool_propagates_cancellation(tmp_path: Path):
    class CancelledRunner(FakeCommandRunner):
        async def run(self, **kwargs) -> CommandResult:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            BashTool(CancelledRunner(CommandResult("", 0))).execute(
                make_call(description="等待", command="sleep 60"),
                make_context(tmp_path),
            )
        )
