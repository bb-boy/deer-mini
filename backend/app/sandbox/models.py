"""Thread 容器在使用中和 warm pool 之间流转时的登记信息。"""

from dataclasses import dataclass


@dataclass
class SandboxEntry:
    user_id: str
    thread_id: str
    workspace_path: str
    # Run 开始只登记占用；第一次 Bash 才创建并填入容器名。
    container_id: str | None = None
    active_run_id: str | None = None
    # 使用中为 None；归还时记录单调时钟的秒数，只在本进程内使用。
    idle_since: float | None = None
    # 清理失败的容器仍须登记，但绝不能再次交给 Bash 使用。
    broken: bool = False
