"""管理“哪个对话使用哪个容器”，让连续两轮任务能够复用工作环境。

用户场景：第一轮生成 outputs/result.txt，第二轮继续处理它。
本文件负责领取、归还、复用和清理；docker_runner.py 负责向 Docker 发出具体命令。
服务器文件由三个挂载目录保存；同一容器里的 /tmp 内容还可在复用期间继续使用。

真实调用顺序：
1. 后端启动：RunCoordinator.start() → manager.start()。
2. 一轮开始：AgentRuntime.run() → manager.begin_run()，登记使用权。
3. Agent 调用工具：BashTool.execute() → manager.run()，第一次才创建容器。
4. 一轮收尾：AgentRuntime → manager.end_run()，归还到空闲集合。
5. 定时检查：manager.reap_idle()，删除闲置太久的容器。
6. 后端退出：RunCoordinator.shutdown() → manager.close()。

这里的字典和锁只对当前 Python 进程有效，所以 Mini 使用一个后端进程。
这些方法不会替 Runtime 保存 Run 成败，也不会自己把工具结果发送给浏览器。
"""

# asyncio 提供异步等待、任务和锁。等待 Docker 时，后端还能去处理其他对话。
import asyncio
# Callable 表示“可以调用的对象”；Awaitable 表示“调用后可以用 await 等待的结果”。
from collections.abc import Awaitable, Callable
# suppress 可以忽略明确指定的异常，下面用它接住预期的“任务已取消”。
from contextlib import suppress
# hashlib 把项目路径转换成稳定的短标记，用来识别本项目创建的容器。
import hashlib
# logging 把清理失败等情况写入应用日志，便于在后端终端中排查。
import logging
# os 用于读取当前进程的环境变量，例如 Docker 程序的位置。
import os
# Path 用“路径对象”处理目录，支持 .parent、.resolve() 等操作。
from pathlib import Path
# shutil.which() 用于查找 docker 可执行程序是否存在。
import shutil
# time.monotonic() 像持续走动的秒表，用来计算容器已经闲置多久。
import time
# uuid4() 生成随机标识，避免给不同容器取到同一个名字。
from uuid import uuid4

# CommandResult 是最终要返回给 BashTool 的结果格式。
from app.sandbox.base import CommandResult
# 管理器把真正的容器操作交给 Runner；Config 保存 Runner 的限制和超时配置。
from app.sandbox.docker_runner import DockerCommandRunner, DockerRunnerConfig
# SandboxEntry 是“容器借用登记卡”，记录归属、占用者和闲置状态。
from app.sandbox.models import SandboxEntry
from app.repositories.sandbox_safety_repository import SandboxSafetyRepository
from app.runtime.async_io import run_sync


# __name__ 是当前模块名；日志因此能标明消息来自 app.sandbox.manager。
logger = logging.getLogger(__name__)
# 给“两个字符串组成的元组”起一个类型别名，例如 ("alice", "thread-001")。
# 同时使用用户和对话作为字典键，避免把不同用户的记录混在一起。
ThreadKey = tuple[str, str]


def default_sandbox_scope() -> str:
    """根据项目所在目录，得到用来识别本项目容器的标记。

    接收：没有显式参数，使用当前文件的路径 __file__。
    返回：16 个十六进制字符，例如一个类似 "a1b2c3d4e5f60718" 的字符串。
    例子：同一后端重启后项目路径不变，标记就不变，因此能找到旧进程留下的容器。
    这个标记用于分组，不是用户密码，也不负责身份认证。
    """
    # 把下面的项目目录字符串交给 SHA-256，生成确定的摘要。
    return hashlib.sha256(
        # __file__ 是本文件；parents[2] 从 sandbox → app → backend，得到后端目录。
        # str() 转为文字，encode() 转为字节，因为摘要函数接收字节。
        str(Path(__file__).resolve().parents[2]).encode()
    # hexdigest() 将摘要变为十六进制字符串；[:16] 只保留前 16 个字符。
    ).hexdigest()[:16]


async def cleanup_orphaned_thread_sandboxes() -> None:
    """后端启动时，处理旧进程留下的本项目容器。

    接收：没有显式参数；Docker 程序位置从环境变量读取。
    返回：None；调用方只等待检查和清理完成。
    例子：上一次开着 Bash，后端异常退出；这次关闭 Bash，也应处理旧容器。
    找不到 Docker 程序时由 Coordinator 检查持久化使用标记；连接或清理失败阻止启动。
    """
    # 优先使用配置的程序位置，没有配置时使用 PATH 中的 docker。
    docker_binary = os.getenv("DEER_MINI_DOCKER_BINARY", "docker")
    # which 返回找到的程序路径；返回 None 表示当前环境找不到这个程序。
    if shutil.which(docker_binary) is None:
        # 从未使用容器的部署不依赖 Docker；有未确认停止记录时必须拒绝启动。
        await run_sync(SandboxSafetyRepository().require_clear_without_docker)
        return
    # 这里只创建 Python 执行器对象，真正调用 Docker 是下一步。
    runner = DockerCommandRunner(DockerRunnerConfig(docker_binary=docker_binary))
    # 清理时可能连接不到 Docker 服务，所以把可能失败的操作放进 try。
    # 停止失败必须阻止启动，否则旧后台进程可能在恢复目录时继续写入。
    await runner.remove_owned_containers(default_sandbox_scope())
    await run_sync(SandboxSafetyRepository().mark_clear)


class ThreadSandboxManager:
    """像设备管理员一样，维护每段对话的容器借用记录。

    创建时接收具体的 Docker runner，可选接收项目标记 scope 和计时函数 clock。
    一个实例由 RunCoordinator 保存，供当前后端的各次 Run 共用。
    它既有 run() 给 BashTool 调用，也有 begin_run()/end_run() 给 Runtime 调用。

    例子：alice 的 t1 使用容器 A，bob 的 t2 使用容器 B。
    t1 的连续两轮可以复用 A；t2 的命令则不会被分配到 A。
    """

    def __init__(
        self,  # 正在创建的管理器对象，Python 自动传入。
        runner: DockerCommandRunner,  # 真正执行 Docker 操作的对象。
        *,  # scope 和 clock 必须按名称传入，例如 scope="test-only"。
        scope: str | None = None,  # 容器分组标记；不传则根据后端路径计算。
        clock: Callable[[], float] = time.monotonic,  # 不接收参数、返回秒数的计时函数。
    ) -> None:
        """建立管理员的登记本，创建时不启动任何容器。

        runner 负责执行，scope 区分项目，clock 用于计算闲置时长。
        测试可传 clock=lambda: 1000.0，让“当前时刻”可控而不必真的等十分钟。
        初始化方法返回 None；创建表达式 ThreadSandboxManager(...) 得到实例。
        """
        # 保存执行器，后面通过 self._runner.start_container() 等方法操作容器。
        self._runner = runner
        # 保存函数本身而不是立即调用；以后 self._clock() 才读取当时的秒数。
        self._clock = clock
        # 调用方给了非空标记就使用它，否则计算本项目默认标记。
        self._scope = scope or default_sandbox_scope()
        # 使用中的登记本：例如 {("alice", "t1"): 某个 SandboxEntry}。
        self._active: dict[ThreadKey, SandboxEntry] = {}
        # 已归还、等待下一轮使用的登记本；里面的容器通常仍在运行。
        self._warm_pool: dict[ThreadKey, SandboxEntry] = {}
        # 每段对话一把锁，保证该对话的领取、执行和删除不会同时改同一份记录。
        self._locks: dict[ThreadKey, asyncio.Lock] = {}
        # 定时回收任务的引用；None 表示尚未建立这个后台任务。
        self._reaper: asyncio.Task[None] | None = None
        # 管理器刚创建时可以接新任务；close() 会把这个开关设为 True。
        self._closed = False

    def _lock(self, key: ThreadKey) -> asyncio.Lock:
        """为一段对话取出同一把锁。

        key 是 (user_id, thread_id)，例如 ("alice", "t1")；返回 asyncio.Lock。
        第一次调用时存入一把锁，之后对同一个 key 都返回已经保存的锁。
        锁像“单人使用牌”：同一时刻只有一段相关代码能持牌操作这段对话。
        """
        # setdefault：有 key 就返回旧值，没有才把右边的新锁存入字典并返回。
        # 右边 asyncio.Lock() 会先被计算；key 已存在时，这个临时新锁不会被采用。
        return self._locks.setdefault(key, asyncio.Lock())

    async def start(self) -> None:
        """后端启动时清理旧容器，并安排之后定期检查闲置容器。

        接收：当前管理器已经保存的 runner、scope 和配置；返回 None。
        例子：旧进程遗留容器 A，先清理 A；之后每隔默认 60 秒检查闲置集合。
        当前 Mini 按单进程运行，同一项目路径不应同时启动多个这样的管理器进程。
        """
        # 已保存回收任务引用时直接返回，避免重复安排定时任务。
        if self._reaper is not None:
            # 裸 return 表示结束函数，返回 None。
            return
        # 已关闭的管理器不能重新开始服务。
        if self._closed:
            # 报错交给启动它的上层处理。
            raise RuntimeError("容器管理器已经关闭")
        # 等待带当前 scope 标记的旧容器清理完成，再开始提供本次进程的服务。
        await self._runner.remove_owned_containers(self._scope)
        # create_task 安排异步函数在事件循环里继续运行，并立即给出一个 Task 对象。
        # 它不是另外创建一个 Python 线程；遇到 await 时会与其他任务交替执行。
        self._reaper = asyncio.create_task(
            # name 只是便于调试时识别任务；_reap_loop() 是要持续运行的回收循环。
            self._reap_loop(), name="thread-sandbox-reaper"
        )

    async def begin_run(
        self, *, user_id: str, thread_id: str, run_id: str, workspace_path: str
    ) -> None:
        """Run 开始时，为它领取当前对话的执行环境。

        user_id 是用户，thread_id 是对话，run_id 是本轮执行的标识。
        workspace_path 是服务器上的 workspace，例如 /data/t1/workspace。
        返回 None；登记成功后，这一轮才可以通过 run() 执行命令。
        例子：run-101 领取 t1；同一轮重复领取不会多建记录，另一轮抢占会报错。
        此处只检查目录并移动内存记录，第一次 Bash 才会真正创建容器。
        """
        # 用用户和对话组成不可变的元组，作为三本字典共有的索引。
        key = (user_id, thread_id)
        # resolve(strict=True) 得到规范绝对路径，并要求这个位置已经存在。
        # 例如 /data/t1/./workspace 会变成 /data/t1/workspace，便于之后准确比较。
        workspace = str(Path(workspace_path).resolve(strict=True))
        # 路径即使存在，也可能是普通文件，所以再检查它确实是目录。
        if not Path(workspace).is_dir():
            # 无法把普通文件当工作目录时，立即报告给 Runtime。
            raise ValueError("Thread Workspace 不是目录")
        # 等待拿到这段对话的锁；离开缩进块时自动释放，包括 return 或抛异常的情况。
        async with self._lock(key):
            # 等锁期间管理器也可能被关闭，因此在锁内再次看当前状态。
            if self._closed:
                # 停止接受新的使用权登记。
                raise RuntimeError("容器管理器已经关闭")
            # get 找不到 key 时返回 None，表示当前没有 Run 使用这段对话的环境。
            entry = self._active.get(key)
            # 找到了正在使用的登记卡，需要确认是不是同一轮的重复调用。
            if entry is not None:
                # 例如卡上是 run-101，传入的是 run-102，表示上一轮还没归还。
                if entry.active_run_id != run_id:
                    # 不能让两轮同时把自己当作容器的使用者。
                    raise RuntimeError("同一 Thread 的另一个 Run 尚未归还容器")
                # 即使 Run 相同，也不能悄悄换成另一段对话的服务器目录。
                if entry.workspace_path != workspace:
                    # 路径不一致时保留原记录并报错。
                    raise ValueError("同一 Thread 的 Workspace 不一致")
                # 同一 Run、同一目录再次领取，相当于确认现有登记即可。
                return
            # 使用中没有记录，再去空闲集合找上一轮留下的容器。
            entry = self._warm_pool.get(key)
            # 空闲容器已经挂载了固定目录，复用时要求目录仍然相同。
            if entry is not None and entry.workspace_path != workspace:
                # 挂载目录不能通过修改一条 Python 记录来改变。
                raise ValueError("同一 Thread 的 Workspace 不一致")
            # pop 从空闲集合“取出并删除”登记；没找到时返回 None，再创建新登记卡。
            entry = self._warm_pool.pop(key, None) or SandboxEntry(
                # 新卡记录用户、对话和规范后的服务器目录，container_id 默认仍为 None。
                user_id=user_id, thread_id=thread_id, workspace_path=workspace
            )
            # 在卡上写明这一次是谁在使用，例如 run-102。
            entry.active_run_id = run_id
            # 使用中不计算闲置时长，所以清空上一次归还的时刻。
            entry.idle_since = None
            # 把卡放进使用中的登记本。现在仍可能只有卡，还没有真实容器。
            self._active[key] = entry

    async def end_run(self, *, user_id: str, thread_id: str, run_id: str) -> None:
        """本轮收尾时归还容器，让同一对话的下一轮可以复用。

        接收本轮的 user_id、thread_id、run_id；返回 None。
        例子：run-101 归还后容器进入空闲集合，run-102 可以再领走它。
        这里只更新内存中的使用者和闲置时刻，不会因为正常归还就删除容器。
        """
        # 找到本轮所属用户和对话的共同索引。
        key = (user_id, thread_id)
        # 与领取和命令执行使用同一把锁，等待它们完成当前操作。
        async with self._lock(key):
            # 读取目前使用中的登记卡。
            entry = self._active.get(key)
            # 没有登记，或卡上已经是另一轮，都表示本次无需归还。
            if entry is None or entry.active_run_id != run_id:
                # 这样旧 run-101 的迟到收尾不会把 run-102 的环境释放掉。
                return
            # 从“使用中”删除索引，entry 变量仍引用着这张卡，所以卡没有立即丢失。
            del self._active[key]
            # 卡上不再登记任何正在使用的 Run。
            entry.active_run_id = None
            # 只有已经存在容器时，才需要保留等待复用的卡。
            if entry.container_id is not None:
                # 从归还这一刻开始计时；例子：秒表读数为 1000.0。
                entry.idle_since = self._clock()
                # 把同一张卡放进空闲集合，容器本身继续运行。
                self._warm_pool[key] = entry

    async def run(
        self,  # 本次负责分配容器的管理器。
        *,  # 之后的参数都要按名称传入。
        command: str,  # 模型决定执行的命令，例如 "cp ../uploads/input.txt ../outputs/result.txt"。
        workspace_path: str,  # Runtime 提供的服务器工作目录，必须与领取时相同。
        user_id: str,  # 任务所属用户，例如 alice。
        thread_id: str,  # 任务所属对话，例如 t1。
        run_id: str,  # 当前执行轮次，例如 run-101，用来核对使用权。
        tool_call_id: str,  # 保留统一工具接口的参数；本管理器按 Thread 分配容器，不使用它命名。
    ) -> CommandResult:
        """给当前 Run 选择正确的容器，再执行一条真实 Bash 命令。

        返回 CommandResult，内含输出、退出码和超时信息，随后交给 BashTool。
        例子：run-101 第一次执行 cp 时创建容器 A；接着执行 cat 时继续使用 A。
        命令可以修改挂载目录中的文件；创建、查询和删除容器都交给 self._runner。
        普通非零退出码作为结果返回；执行环境异常或取消则进入异常清理流程。
        """
        # 同一用户、同一对话的多轮任务共用这一个键。
        key = (user_id, thread_id)
        # 锁覆盖整条命令：它没结束时，同一 Thread 的另一条命令要等待。
        # 等待 Docker 的 await 会让其他 Thread 获得运行机会，不会锁住所有对话。
        async with self._lock(key):
            # 只从“使用中”找，不能绕过 begin_run() 直接领取空闲容器。
            entry = self._active.get(key)
            # 三种情况均不可执行：管理器关了、没领取、领取者不是当前 run_id。
            if self._closed or entry is None or entry.active_run_id != run_id:
                # 报错后 BashTool 会把普通执行错误整理成工具结果。
                raise RuntimeError("Bash 必须由当前 Run 领取 Thread 容器后执行")
            # 再规范一次调用方路径，确认仍指向这张登记卡对应的服务器目录。
            if str(Path(workspace_path).resolve(strict=True)) != entry.workspace_path:
                # 防止把一段对话的容器用来处理另一段对话的目录。
                raise ValueError("Bash 的 Workspace 与 Thread 容器不一致")
            # 上次发生清理问题的容器，必须先尝试删除，不能直接接着执行。
            if entry.broken:
                # 删除成功会清空 container_id；失败则抛异常，后面的创建逻辑不会执行。
                await self._destroy(entry)
            # 卡上有名字时，还需要向 Docker 确认容器是否确实在运行。
            if entry.container_id is not None:
                # 例如有人在外面手动停掉了容器，内存中还可能保留着旧名字。
                if not await self._runner.container_is_running(entry.container_id):
                    # 只有确认停止或不存在，才清理旧登记并准备重建。
                    # 如果只是连接不到 Docker，查询会报错，不会冒充“容器不存在”。
                    await self._destroy(entry)
            # 接下来是真正创建、执行的阶段；失败时需要尝试清理相关容器。
            try:
                # 没有容器的情况：首次使用，或者上面的检查已经清理了旧容器。
                if entry.container_id is None:
                    # 先生成并记住名字，即使创建途中取消，也知道该按哪个名字清理。
                    # uuid4().hex 是不带横杠的随机十六进制字符串。
                    entry.container_id = f"deer-mini-thread-{uuid4().hex}"
                    # await 等待容器启动完成，之后才能 docker exec 进去执行命令。
                    await self._runner.start_container(
                        container_name=entry.container_id,  # 新生成的容器名称。
                        workspace_path=entry.workspace_path,  # 按该目录找到三个挂载源。
                        user_id=user_id,  # 写入 Docker 标签，便于查看归属。
                        thread_id=thread_id,  # 同样作为归属标签。
                        scope=self._scope,  # 本项目的清理分组标记。
                    )
                # 无论刚刚创建还是复用旧容器，都走同一个 docker exec 执行入口。
                result = await self._runner.run_in_container(
                    # 在指定容器中执行模型命令；每次从虚拟 workspace 开始。
                    container_name=entry.container_id, command=command
                )
                # 超时与普通命令失败不同：Runner 的超时处理会删除容器来停止其中的命令。
                if result.timed_out:
                    # 清空旧名字；若本轮还要执行下一条 Bash，就会重新创建容器。
                    entry.container_id = None
                # 把结果原样交给 BashTool，由它决定如何生成模型看到的工具消息。
                return result
            # BaseException 还包含 asyncio.CancelledError，确保用户取消也会进入清理。
            except BaseException:
                # 先标记“不能直接复用”，即使下面清理失败，也保留这个事实。
                entry.broken = True
                # 单独安排清理任务，后面可保护它，避免跟随外层取消一起中断。
                cleanup = asyncio.create_task(self._destroy(entry))
                # try 用来区分“等待又被取消”和“清理本身失败”。
                try:
                    # shield 保护 cleanup 任务；外层仍可能收到取消，但 cleanup 可以继续运行。
                    await asyncio.shield(cleanup)
                # 外层在等清理时又收到取消，仍尝试等已开始的清理任务结束。
                except asyncio.CancelledError:
                    # 忽略这次清理的普通异常，让后面继续抛原来的执行异常或取消。
                    with suppress(Exception):
                        # cleanup 是已启动的 Task，不会因此重新执行一遍 _destroy()。
                        await cleanup
                # 普通清理错误，例如 Docker 服务中断，记录下来供排查。
                except Exception:
                    # 失败时 _destroy() 没有清掉容器名，之后仍可按这张卡重试。
                    logger.exception("清理 Thread 容器失败：%s", entry.container_id)
                # 不把失败伪装成成功：继续把原来的执行异常或取消交给上层。
                raise

    async def _destroy(self, entry: SandboxEntry) -> None:
        """根据登记卡删除容器，并在确认完成后更新这张卡。

        entry 是某段对话的登记卡；返回 None。
        例子：entry.container_id 指向容器 A，删除成功后改为 None。
        这里只处理容器及登记字段，不删除服务器上的 workspace/uploads/outputs。
        """
        # 没有容器名称时无需调用 Docker。
        if entry.container_id is not None:
            # 开始删除前先标记，防止删除失败后被误认为可正常使用。
            entry.broken = True
            # 等待 Docker 确认删除；失败会抛异常，下面的清空动作不会执行。
            await self._runner.remove_container(entry.container_id)
            # 只有已经确认容器删除，才忘掉它的名字。
            entry.container_id = None
        # 当前已经没有需要继续清理的容器，可以移除故障标记。
        entry.broken = False

    async def stop_thread(self, *, user_id: str, thread_id: str) -> None:
        """确认当前 Thread 的所有容器已停止；失败保留登记，禁止假装停止成功。"""
        key = (user_id, thread_id)
        async with self._lock(key):
            for pool in (self._active, self._warm_pool):
                entry = pool.get(key)
                if entry is not None:
                    await self._destroy(entry)
                    pool.pop(key, None)

    async def reap_idle(self) -> list[str]:
        """检查空闲集合，删除闲置太久或需要清理的容器。

        接收：管理器内部的空闲登记、秒表函数、闲置时限配置。
        返回：本次确认删除的容器名称列表，例如 ["deer-mini-thread-..."]。
        例子：1000 秒归还，1605 秒检查，时限为 600 秒，这个容器就可以回收。
        _active 中的容器正在供 Run 使用，不在本次回收范围。
        """
        # 准备返回结果，开始时一个容器也还没删除。
        removed: list[str] = []
        # list() 复制当前键，避免等待期间字典变化导致“遍历时字典大小改变”。
        for key in list(self._warm_pool):
            # 与 begin_run 使用同一把锁，避免一边刚领走、一边正准备删除。
            async with self._lock(key):
                # 获取锁后重新读取：等待锁时，这张卡可能已经被下一轮取走了。
                entry = self._warm_pool.get(key)
                # 记录已被取走，或没有有效闲置起点，都跳过。
                if entry is None or entry.idle_since is None:
                    # continue 结束本轮循环，去检查下一个对话。
                    continue
                # 状态正常且还没达到闲置时限，就保留；broken=True 则不用等到时限。
                if not entry.broken and (
                    # 当前秒表读数减去归还时读数，得到闲置秒数。
                    self._clock() - entry.idle_since
                    # 例如已闲置 30 秒，小于默认 600 秒。
                    < self._runner.config.idle_timeout_seconds
                ):
                    # 容器还可以留给下一轮使用，本次不删除。
                    continue
                # _destroy 会清空字段，所以先保存旧名字，稍后加入返回列表。
                container_id = entry.container_id
                # 单个容器删除失败，不应妨碍检查其他空闲容器。
                try:
                    # 确认删除并更新登记卡；失败时会保留名字及 broken 标记。
                    await self._destroy(entry)
                # 捕获普通删除错误，例如暂时连接不到 Docker。
                except Exception:
                    # 日志包含容器名及当前异常调用栈。
                    logger.exception("回收闲置容器失败：%s", container_id)
                    # 保留这张空闲登记卡，以后还可以尝试清理。
                    continue
                # 删除成功后，移除空闲集合中的登记。
                del self._warm_pool[key]
                # 只有原先确实关联了容器，才需要把名字记入返回结果。
                if container_id is not None:
                    # append 追加一个元素，例如将容器 A 的名字放到结果末尾。
                    removed.append(container_id)
        # 调用者可以知道本次清理了哪些容器；没有清理时得到 []。
        return removed

    async def _reap_loop(self) -> None:
        """持续等待并检查闲置容器，由 start() 安排在后台运行。

        不接收额外参数；读取 runner.config 中的检查间隔。
        例子：默认等待 60 秒，检查一次，再等待 60 秒。
        正常会一直循环；close() 通过取消这个任务让它退出。
        """
        # True 始终成立，表示持续执行，直到取消或异常终止。
        while True:
            # asyncio.sleep 只让本任务等待，不会阻塞其他对话的执行。
            await asyncio.sleep(self._runner.config.idle_check_interval_seconds)
            # 一轮检查结束后，再回到 while 顶部等待下一次。
            await self.reap_idle()

    async def delete_thread(
        self,  # 持有容器登记的管理器。
        *,  # 参数按名称传入。
        user_id: str,  # 要删除的对话属于谁。
        thread_id: str,  # 用户选中了哪段对话。
        delete_workspace: Callable[[], Awaitable[None]],  # 上层提供的异步删除函数。
    ) -> None:
        """删除对话前，先把仍挂载其目录的容器清理掉。

        user_id、thread_id 用来找登记；delete_workspace 由 RunCoordinator 提供。
        Callable[[], Awaitable[None]] 表示“不带参数调用后，可以 await 等待”的函数。
        返回 None；容器处理完后才调用上层的删除逻辑，后者负责 Thread 记录和文件。
        例子：用户删除 t1，先停止并删除容器 A，再处理 t1 的数据库记录和目录。
        """
        # 根据用户和对话定位登记。
        key = (user_id, thread_id)
        # 在删除全过程持锁，这段对话不能中途又被 begin_run 领取。
        async with self._lock(key):
            # 使用中的对话不能删除，避免截断还在执行的任务。
            if key in self._active:
                # 把“当前不能删除”的原因交回 API 调用链。
                raise RuntimeError("运行中的 Thread 不能删除")
            # 找出可能还在空闲集合等待复用的容器。
            entry = self._warm_pool.get(key)
            # 有登记时先清理对应的容器。
            if entry is not None:
                # 如果清理失败会直接报错，上层的文件删除就不会开始。
                await self._destroy(entry)
                # 清理成功后，从空闲集合移除这张卡。
                del self._warm_pool[key]
            # 执行传进来的函数并等待。具体删除操作在服务层，不写死在管理器里。
            await delete_workspace()

    async def close(self) -> None:
        """后端退出时，停止后台检查并尝试清理所有登记中的容器。

        没有额外参数；成功返回 None，无法全部清理则抛 RuntimeError。
        调用顺序是 RunCoordinator 先结束正在运行的 Run，再调用这里。
        例子：关掉后端服务时，空闲容器 A、B 都需要清理，不能只清理最后用过的一个。
        """
        # 先停止接受新的领取和 Bash 请求，避免清理期间又加入新任务。
        self._closed = True
        # 只有 start() 建立过后台任务时，才需要取消它。
        if self._reaper is not None:
            # 发出取消请求；它通常在下一个 await 处收到 CancelledError。
            self._reaper.cancel()
            # 这是我们主动发起的取消，因此只忽略这种预期异常。
            with suppress(asyncio.CancelledError):
                # cancel() 只是请求；await 才等待后台任务实际结束。
                await self._reaper
            # 后台任务已处理完，清空保存的引用。
            self._reaper = None
        # 收集清理错误，保证一个容器失败后仍尝试清理其他容器。
        failures: list[Exception] = []
        # set 取得字典键的集合，| 求并集，得到两本登记簿中所有不同的对话。
        for key in set(self._active) | set(self._warm_pool):
            # 每段对话仍使用自己的锁，避免与已经开始的操作冲突。
            async with self._lock(key):
                # 先查使用中，再查空闲集合；正常情况下同一张卡只会在其中一处。
                entry = self._active.get(key) or self._warm_pool.get(key)
                # 等锁期间记录已经移除，就无需再处理。
                if entry is None:
                    # 去处理下一段对话。
                    continue
                # 本次清理失败时需要保存错误，同时继续检查其他对话。
                try:
                    # 删除容器并在成功后清空卡上的名字。
                    await self._destroy(entry)
                # error 是捕获到的异常对象，保存它以保留真正的失败原因。
                except Exception as error:
                    # 记录本次失败，不覆盖前面已经记录的错误。
                    failures.append(error)
                    # 保留未清理成功的登记卡，继续清理下一项。
                    continue
                # pop(key, None) 表示“存在就移除，不存在也不用报 KeyError”。
                self._active.pop(key, None)
                # 同时确保空闲集合中也不再留下已经清理的记录。
                self._warm_pool.pop(key, None)
        # 列表非空说明至少一个容器无法确认清理完成。
        if failures:
            # from failures[0] 将第一条原始异常作为原因，排查时可以看到完整异常链。
            raise RuntimeError("关闭时未能回收全部 Thread 容器") from failures[0]
