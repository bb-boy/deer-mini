"""先约定“怎样请求执行命令”和“怎样拿到结果”。

用户场景：你让 Agent 读取上传资料，Agent 决定调用 bash 工具。
调用顺序：BashTool.execute() → 某个执行器的 run() → CommandResult。
本文件提供双方都能看懂的约定，真正的执行代码在 manager.py 和 docker_runner.py。

把它理解为餐馆的点菜单：菜单规定要填什么、会拿到什么；做菜由厨房负责。
CommandRunner 约定怎样执行一条命令；SandboxLifecycle 约定怎样领取和归还环境。
CommandResult 是执行完后交回来的结果单。

这里的类型标注帮助读者和检查工具理解代码，不会自动检查所有传入值。
例如 command: str 表示“这里应该传字符串”，实际的内容检查仍需其他代码完成。
"""

# dataclass 会根据下面声明的字段，自动生成初始化方法等常用代码。
from dataclasses import dataclass
# Protocol 用来描述“对象需要有哪些方法”，不要求具体实现继承同一个父类。
from typing import Protocol


# @ 是装饰器语法：让 dataclass 处理紧接着的类；frozen=True 禁止事后改写字段。
# 例如创建结果后 result.exit_code = 7 会报错，避免结果被调用方无意改掉。
@dataclass(frozen=True)
class CommandResult:
    """一条命令执行完后的结果单，BashTool 会据此生成给模型的工具结果。

    接收：输出文字、退出码、是否超时、输出是否被截断，分别存入下面四个字段。
    得到：一个 CommandResult 对象；创建它只是在内存中整理结果。
    例子：printf hello 正常结束，可以得到 CommandResult(output="hello", exit_code=0)。
    例子：命令执行太久被停止，可以得到 exit_code=None、timed_out=True 的结果。
    工具结果回到 Agent 后，才由上游继续保存状态、调用模型和发送浏览器事件。
    """

    # stdout 与 stderr 合并后的文字。例如 cat input.txt 输出的文件内容。
    output: str
    # 退出码：通常 0 表示命令成功，非 0 表示命令失败；None 表示这里没有正常退出码。
    # “int | None”表示允许整数或 None 两种值；超时分支会明确使用 None。
    exit_code: int | None
    # 默认没有超时；Runner 超时并完成清理后，才把它设置成 True。
    timed_out: bool = False
    # 默认保留了全部输出；输出太长、只留下头尾时设置为 True。
    output_truncated: bool = False
    # 缓冲预览之外的完整原始输出；消费者读完后负责删除临时文件。
    full_output_path: str | None = None


# Protocol 像“插座标准”：调用方只关心方法是否符合约定，不关心内部用哪种实现。
class CommandRunner(Protocol):
    """约定 BashTool 能怎样调用一个执行器。

    BashTool 接收一个符合该约定的 runner，然后调用 await runner.run(...)。
    当前应用通常传 ThreadSandboxManager；直接验证时也可以传 DockerCommandRunner。
    它们内部实现不同，但都接收下面这些信息并返回 CommandResult。
    这个类用于描述接口，本身没有执行 Bash 的实现。
    """

    # async def 定义可以等待外部工作的函数；调用方用 await 等它返回执行结果。
    async def run(
        self,  # 当前执行器对象，写 runner.run(...) 时 Python 会自动传入。
        *,  # 后续参数必须写名字，例如 command="pwd"，避免一串字符串传错位置。
        command: str,  # 要执行的 Bash 文字，例如 "cp ../uploads/input.txt ../outputs/result.txt"。
        workspace_path: str,  # 服务器上的目录，例如 /data/t1/workspace，用于定位当前 Thread。
        user_id: str,  # 任务属于谁，例如 alice；与 thread_id 一起区分不同对话。
        thread_id: str,  # 当前是哪段对话，例如 thread-001；同一对话可连续执行多轮。
        run_id: str,  # 当前是哪一轮执行，例如 run-101，用来检查是否由这轮领取了环境。
        tool_call_id: str,  # 模型这一次工具调用的 ID；同一轮中可能调用多次 Bash。
    ) -> CommandResult:  # 等待完成后返回一张执行结果单。
        """接收命令及归属信息，约定返回 CommandResult；实现类负责真正执行。

        例如同一 run-101 先调用 pwd，再调用 cat，每次 tool_call_id 不同，
        user_id、thread_id、run_id 和 workspace_path 则仍然相同。
        """
        # ... 是“此处省略实现”的占位写法；具体执行器必须提供真正的方法体。
        ...


class SandboxLifecycle(Protocol):
   
    # 开始一轮执行时，Runtime 提供归属信息和服务器上的工作目录。
    async def begin_run(
        self, *, user_id: str, thread_id: str, run_id: str, workspace_path: str
    ) -> None:  # None 表示调用方等待登记完成，不接收另外一份返回数据。
        """为本轮登记对当前 Thread 执行环境的使用权。

        user_id 是用户；thread_id 是对话；run_id 是这轮执行；
        workspace_path 是服务器上这段对话的 workspace 绝对路径。
        例子：alice 的 thread-001 开始 run-101 后，登记“由 run-101 使用”。
        当前管理器在这里登记或取回复用记录，第一次执行 Bash 时才创建容器。
        """
        # 接口约定在这里结束；具体登记过程在 ThreadSandboxManager.begin_run()。
        ...

    # 结束时不必再传目录：管理器已在 begin_run() 保存了本轮使用的目录。
    async def end_run(self, *, user_id: str, thread_id: str, run_id: str) -> None:
        """根据用户、对话和本轮 ID，归还这轮占用的执行环境。

        三个 ID 与 begin_run() 对应；返回 None 表示归还处理已经完成。
        例子：run-101 结束后，容器可以留给同一 thread-001 的 run-102 使用。
        当前管理器通过内存记录处理归还；Run 是否成功仍由 Runtime 判断和保存。
        """
        # 真正的归还和闲置计时在 ThreadSandboxManager.end_run() 中完成。
        ...
