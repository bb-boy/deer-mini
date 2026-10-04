"""记录“某段对话的容器现在由谁使用”。

SandboxEntry 像设备借用登记卡，ThreadSandboxManager 负责填写和移动这张卡。
正在使用时，卡片放在 _active 字典；归还后，放在 _warm_pool 字典等待复用。
warm pool 在这里就是“已准备好、暂时空闲的容器集合”。

例子：
1. run-101 开始：先记下 alice、thread-001、workspace 和 active_run_id。
2. 第一次调用 Bash：创建容器，并记下 container_id。
3. run-101 结束：active_run_id 清空，idle_since 记下开始闲置的时刻。
4. run-102 开始：同一张登记卡再次进入使用中，容器可以继续复用。

这些登记只存在于当前后端进程的内存中，程序重启后不会自动从 SQLite 恢复。
服务器上的 workspace/uploads/outputs 文件则独立保存在磁盘上。
"""

# dataclass 根据字段自动生成 __init__，让我们能按字段名创建一张登记卡。
from dataclasses import dataclass



@dataclass
class SandboxEntry:

    """保存一段对话的容器登记信息，供 manager.py 使用。

    创建时接收：用户 ID、对话 ID、服务器上的 workspace 路径。
    还可传入下面的可选字段；通常从默认值开始，由管理器逐步填写。
    得到：一个可修改的 Python 对象；创建这张卡本身不会创建 Docker 容器。
    例子：SandboxEntry(user_id="alice", thread_id="t1", workspace_path="/data/t1/workspace")。
    """

    # 这段对话属于谁，例如 alice；用于区分不同用户的记录。
    user_id: str
    # 这是哪段对话，例如 thread-001；同一对话可以有 run-101、run-102 等多轮执行。
    thread_id: str
    # 服务器上的真实工作目录；容器内部使用的 /mnt/user-data/workspace 由挂载对应过来。
    workspace_path: str
    # 容器的标识。当前实现保存的是 Docker 容器名称，例如 deer-mini-thread-<随机串>。
    # None 表示尚未创建，或已经确认删除。名字叫 container_id，但这里不要求是 Docker 短 ID。
    container_id: str | None = None
    # 正在使用它的 Run，例如 run-101；归还后改成 None，表示此刻没有 Run 占用。
    active_run_id: str | None = None
    # 从什么时候开始闲置。使用 time.monotonic() 的秒数，只用于计算经过了多久。
    # 例如归还时是 1000.0，后来是 1605.0，相减就是闲置 605 秒。
    # 它不是“2026 年某月某日”的时间戳；正在使用时为 None。
    idle_since: float | None = None
    # True 表示这张卡关联的容器需要先清理，例如上一条命令被取消且清理尚未完成。
    # 管理器保留这张卡以便重试清理，不会直接把这个容器继续交给下一条命令。
    broken: bool = False
