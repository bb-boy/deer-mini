"""Bash 的完整输出参与结果策略，临时捕获文件必须清理。"""

import asyncio

import pytest

from app.domain.messages import ToolCall
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.sandbox.base import CommandResult
from app.tools.bash import BashTool


@pytest.mark.parametrize("exit_code,timed_out,suffix", [(0, False, ""), (7, False, "[Exit Code: 7]"), (None, True, "[命令执行超时，容器已停止]")])
def test_bash_uses_complete_output_and_removes_capture_file(tmp_path, exit_code, timed_out, suffix):
    path = tmp_path / "raw.log"
    original = "汉" * 60_000
    path.write_text(original)

    class Runner:
        async def run(self, **kwargs):
            return CommandResult("short head and tail", exit_code, timed_out,
                                 output_truncated=True, full_output_path=str(path))

    context = RuntimeContext("user", "thread", "run", str(tmp_path), None, None)
    result = asyncio.run(BashTool(Runner()).execute(
        ToolCall("call", "bash", {"description": "test", "command": "print"}), context,
    ))
    assert result.content == original + ("\n" + suffix if suffix else "")
    assert result.is_error is bool(suffix)
    assert not path.exists()


def test_bash_preserves_result_storage_failure(tmp_path):
    error = StatePersistenceError("output file failure")

    class Runner:
        async def run(self, **kwargs):
            raise error

    async def scenario():
        with pytest.raises(StatePersistenceError) as caught:
            await BashTool(Runner()).execute(
                ToolCall("call", "bash", {"description": "test", "command": "print"}),
                RuntimeContext("user", "thread", "run", str(tmp_path), None, None),
            )
        assert caught.value is error

    asyncio.run(scenario())


def test_bash_complete_output_preserves_carriage_returns(tmp_path):
    path = tmp_path / "raw.log"
    path.write_bytes(b"first\r\nsecond\r\n")

    class Runner:
        async def run(self, **kwargs):
            return CommandResult("preview", 0, full_output_path=str(path))

    result = asyncio.run(BashTool(Runner()).execute(
        ToolCall("call", "bash", {"description": "test", "command": "print"}),
        RuntimeContext("user", "thread", "run", str(tmp_path), None, None),
    ))
    assert result.content == "first\r\nsecond\r\n"
