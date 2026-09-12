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
        user_id: str,
        thread_id: str,
        run_id: str,
        tool_call_id: str,
    ) -> CommandResult:
        ...


class SandboxLifecycle(Protocol):
    """Runtime 只负责通知 Run 边界，不参与 Docker 的具体操作。"""

    async def begin_run(
        self, *, user_id: str, thread_id: str, run_id: str, workspace_path: str
    ) -> None:
        ...

    async def end_run(self, *, user_id: str, thread_id: str, run_id: str) -> None:
        ...
