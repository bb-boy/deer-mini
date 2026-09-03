"""命令执行器的最小契约。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    """一次受控命令执行的结构化结果。"""

    output: str
    exit_code: int | None
    timed_out: bool = False
    output_truncated: bool = False


class CommandRunner(Protocol):
    """规定 BashTool 可以调用的执行器接口。"""

    async def run(
        self,
        *,
        command: str,
        workspace_path: str,
        run_id: str,
        tool_call_id: str,
    ) -> CommandResult:
        ...
