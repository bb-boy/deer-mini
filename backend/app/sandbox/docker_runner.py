"""把 Python 的执行请求变成真实 Docker 操作，并把结果收回来。

用户场景：Agent 要把 uploads/input.txt 处理成 outputs/result.txt。
上游调用：BashTool → ThreadSandboxManager → 本文件的 DockerCommandRunner。
本文件接收命令、服务器目录或容器名称，返回输出和退出情况。

先区分三个东西：
1. Docker CLI：服务器上的 docker 命令行程序，Python 会启动这个程序。
2. Docker 后台服务：接收 CLI 请求，真正管理容器。
3. 容器里的 Bash：在 /mnt/user-data/workspace 中执行模型给出的命令。
CLI 进程退出，不一定表示容器里的任务已经停止，因此下面单独处理容器清理。

推荐沿这条路径读：配置 → build_run_args → start_container → run_in_container
→ _run_process → _drain_output；最后再读超时和取消时的清理分支。
所有路径例子中的 /data/t1 都是为便于讲解而假设的服务器 Thread 目录。
"""

# asyncio 提供异步子进程、等待超时、后台任务等能力。
import asyncio
# PIPE 表示把输出接到 Python 可读取的管道；STDOUT 表示合并错误输出和普通输出。
# Process 是 asyncio 启动的子进程对象类型，可以查询 returncode、等待退出或发出停止信号。
from asyncio.subprocess import PIPE, STDOUT, Process
# dataclass 根据配置字段生成初始化代码，下面用于 DockerRunnerConfig。
from dataclasses import dataclass
# logging 用来记录取消和清理时遇到的错误。
import logging
# math.isfinite() 检查数字是否有限，排除无穷大、NaN 等不能用作时间或配额的值。
import math
# os 既用于读环境变量，也用于向一个进程组发送停止信号。
import os
# Path 用来查询服务器上的目录、拥有者和时区文件。
from pathlib import Path
# re 提供正则表达式：按指定字符规则检查名称、拆解内存配置。
import re
# signal 提供 SIGTERM、SIGKILL 等操作系统信号的名称。
import signal
# uuid4 产生随机标识，给不同命令或容器生成不同名字。
from uuid import uuid4

# ThreadPaths 找到当前对话的三个真实目录；VIRTUAL_WORKSPACE 是容器内统一的工作目录。
from app.filesystem.thread_paths import ThreadPaths, VIRTUAL_WORKSPACE
# 统一返回给工具层的结果格式，定义在 base.py。
from app.sandbox.base import CommandResult

# [^...] 表示“不属于这些字符”；末尾 + 表示连续一个或多个这样的字符。
# 例如 "Run/001" 转小写后，其中 / 会被 _name_part() 替换成 -。
# r"..." 是原始字符串写法，适合表达正则规则；compile 把规则准备成可反复使用的对象。
_SAFE_NAME_PART = re.compile(r"[^a-z0-9_.-]+")

# 花括号创建集合，用 in 就能判断一个字符串是否属于这些写法。
# 环境变量只提供文字，所以把以下写法统一解释为“开启”。
_TRUE_VALUES = {"1", "true", "yes", "on"}
# 以下写法统一解释为“关闭”，不区分大小写的处理在 _load_bool 中。
_FALSE_VALUES = {"0", "false", "no", "off"}

# 内存配置拆成两部分，例如 "512m" → number="512"、unit="m"。
# (?P<number>...) 为一段匹配结果命名，稍后就能用 match.group("number") 取出来。
_MEMORY_RE = re.compile(
    # ^ 和 $ 要求整段字符串匹配；数字可有小数；\s* 允许数字与单位间有空白。
    # [kmgt]?i?b? 允许 k、mb、mib 等单位；? 表示前一部分可以不出现。
    r"^(?P<number>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>[kmgt]?i?b?)?$",
    # 忽略大小写，所以 "512M" 和 "512m" 都可以先通过格式匹配。
    re.IGNORECASE,
)

# 字典把单位转换成“一个单位有多少字节”；** 是乘方，例如 1024**2 = 1048576。
# 本项目将下面各类 k/m/g/t 写法都按 1024 的倍数解释。
_MEMORY_UNITS = {
    "": 1,  # 没写单位时，把数字直接当字节数。
    "b": 1,  # b 表示字节。
    "k": 1024,  # 一个 k 按 1024 字节计算。
    "kb": 1024,  # kb 是同一倍率的另一种输入写法。
    "ki": 1024,  # ki 也使用 1024 字节倍率。
    "kib": 1024,  # KiB 的小写写法，倍率为 1024。
    "m": 1024**2,  # 一个 m 按 1024 × 1024 字节计算。
    "mb": 1024**2,  # mb 使用同一倍率。
    "mi": 1024**2,  # mi 使用同一倍率。
    "mib": 1024**2,  # MiB 的小写写法，例如 512mib。
    "g": 1024**3,  # 一个 g 按 1024 × 1024 × 1024 字节计算。
    "gb": 1024**3,  # gb 使用同一倍率。
    "gi": 1024**3,  # gi 使用同一倍率。
    "gib": 1024**3,  # GiB 的小写写法，例如 1gib。
    "t": 1024**4,  # 一个 t 按 1024 的四次方字节计算。
    "tb": 1024**4,  # tb 使用同一倍率。
    "ti": 1024**4,  # ti 使用同一倍率。
    "tib": 1024**4,  # TiB 的小写写法。
}


# Docker 的内存限制要求至少 6 MiB，这里提前换算成字节以便比较。
_MIN_MEMORY_BYTES = 6 * 1024**2
# 创建属于当前模块的日志记录器，后面的 logger.exception 会带上模块来源。
logger = logging.getLogger(__name__)


class SandboxCleanupError(RuntimeError):
    """表示重试清理后，仍然无法确认容器已经删除。

    接收和 RuntimeError 一样的错误说明，例如“Docker 服务无法连接”。
    调用方可专门识别这个异常，避免把“未清理成功”误报为“已停止”。
    类体只需要说明，因为错误消息和异常行为直接继承 RuntimeError。
    """


def _parse_memory_limit(value: str) -> int:
    """把易读的内存配置转换成 Docker 参数需要的字节数。

    value 是配置文字，例如 "512m"；返回整数 536870912。
    例子："1.5g" 换算为 1610612736 字节；"1m" 太小，会报告 ValueError。
    本函数只计算和检查值，不读取文件，也不启动 Docker。
    """
    # strip 去掉两端空格；fullmatch 要求整个字符串都符合上面的数字与单位格式。
    match = _MEMORY_RE.fullmatch(value.strip())
    # None 表示没有匹配，例如传入了 "很多内存"。
    if match is None:
        # 立即说明配置格式有问题，不等到真正启动容器时才发现。
        raise ValueError("Docker 内存限制格式无效")
    # 取出命名分组 number，把文字 "512" 或 "1.5" 转成可计算的浮点数。
    number = float(match.group("number"))
    # 没写单位时使用空字符串；lower 统一为小写，便于查询倍率字典。
    unit = (match.group("unit") or "").lower()
    # 正则匹配后还要检查单位确实有定义，并排除无限大等异常数字。
    if unit not in _MEMORY_UNITS or not math.isfinite(number):
        # 两种检查中的任意一种失败，都拒绝这份配置。
        raise ValueError("Docker 内存限制格式无效")
    # 数值乘倍率得到字节；int 转成整数，去掉可能的小数字节部分。
    memory_bytes = int(number * _MEMORY_UNITS[unit])
    # 检查换算后的值是否低于 6 MiB。
    if memory_bytes < _MIN_MEMORY_BYTES:
        # 例如 1m 格式正确，但不满足 Docker 的最小限制。
        raise ValueError("Docker 内存限制必须至少为 6 MiB")
    # 交回规范的字节数，之后会转成字符串放进 --memory 参数。
    return memory_bytes


def _load_bool(name: str, default: bool) -> bool:
    """读取一个开关配置，返回 True 或 False。

    name 是环境变量名，例如 DEER_MINI_BASH_ENABLED；default 是未配置时的值。
    例子：变量值为 " YES " 时返回 True；变量不存在时返回 default。
    环境变量是进程持有的文字配置，此函数读取它，不负责加载 .env 文件。
    """
    # getenv 没找到变量时返回 None，能与“用户填写了空字符串”区分开。
    raw_value = os.getenv(name)
    # 只有未配置，才采用调用方给出的默认值。
    if raw_value is None:
        # 例如 Bash 默认关闭，对应返回 False。
        return default
    # 去空白、转小写，让 true、TRUE 和两端带空格的写法含义一致。
    normalized = raw_value.strip().lower()
    # 查询“表示开启”的写法集合。
    if normalized in _TRUE_VALUES:
        # 将字符串配置变成 Python 的布尔值。
        return True
    # 查询“表示关闭”的写法集合。
    if normalized in _FALSE_VALUES:
        # 明确返回关闭状态。
        return False
    # 两个集合都没找到，例如写成 sometimes，直接指出是哪项配置无效。
    raise ValueError(f"{name} 必须是 true 或 false")


def _load_float(name: str, default: str) -> float:
    """读取允许小数的配置，例如命令超时 12.5 秒。

    name 是变量名；default 是默认数字文字，例如 "60"；返回 float。
    这里只负责文字转数字，是否为正数、是否有限等检查由 DockerRunnerConfig 完成。
    """
    # 配置不存在时使用默认文字，存在时使用实际配置。
    raw_value = os.getenv(name, default)
    # 转换可能失败，例如 float("fast")，所以用 try 接住。
    try:
        # 将 "12.5" 转成数字 12.5，供计时或 CPU 配额使用。
        return float(raw_value)
    # 保存原始 ValueError，下一行让报错包含具体环境变量名。
    except ValueError as error:
        # from error 保留原始原因，调试时能同时看到转换失败的细节。
        raise ValueError(f"{name} 必须是数字") from error


def _load_int(name: str, default: str) -> int:
    """读取整数配置，例如最多保留 20000 字节输出。

    name 是变量名；default 是默认整数文字；返回 int。
    例子："64" 返回 64，"64.5" 无法转成这里要求的整数文字，会报错。
    """
    # 得到环境中的文字值，未设置时使用 default。
    raw_value = os.getenv(name, default)
    # int 转换可能报告 ValueError。
    try:
        # 将 "20000" 转成整数 20000。
        return int(raw_value)
    # 转换失败时保存具体异常对象。
    except ValueError as error:
        # 给出可定位到配置项的错误，并保留底层原因。
        raise ValueError(f"{name} 必须是整数") from error


class _BoundedCapture:
    """持续接收命令输出，但只长期保留限定容量的头部和尾部。

    接收：创建时的字节上限 limit_bytes，以及运行中逐块送来的 bytes。
    产出：render() 返回“可展示的文字、是否截断”这一对值。
    例子：命令打印了几百万字节，也不会把全部输出一直积存在内存中。
    仍然必须继续读取管道，否则写满管道的命令可能卡住，迟迟无法退出。
    """

    def __init__(self, limit_bytes: int) -> None:
        """按字节上限分配头尾容量，初始两个缓冲区都为空。

        例如 limit_bytes=100 时，头部最多留 50 字节，尾部最多留 50 字节。
        字节不是中文字数：UTF-8 中一个常见汉字通常占 3 字节。
        """
        # 总共长期保存多少字节输出，由配置中的 max_output_bytes 传入。
        self._limit_bytes = limit_bytes
        # // 是整数除法，例如 101 // 2 得到 50，把这一半留给开头。
        self._head_limit = limit_bytes // 2
        # 剩下部分给末尾，例如 101 - 50 = 51，保证两部分合计不超过上限。
        self._tail_limit = limit_bytes - self._head_limit
        # bytearray 是可以追加、删除内容的字节数组；这里只记录最初的输出。
        self._head = bytearray()
        # 这一份不断更新，用于保留最近收到的输出。
        self._tail = bytearray()
        # 累计收到的实际字节数，包括之后因为超限而丢弃的内容。
        self.total_bytes = 0

    def append(self, chunk: bytes) -> None:
        """接收刚从管道读到的一块字节，更新头尾缓冲区。

        chunk 例如 b"hello"；返回 None，内容保存在对象内部。
        例子：上限为 6，收到 b"abcdefgh" 后，头部是 abc，尾部是 fgh。
        """
        # len 对 bytes 计算的是字节数；+= 表示在已有累计值上再加这次的大小。
        self.total_bytes += len(chunk)
        # 算出头部还差多少才填满，例如容量 50、已有 30，还差 20。
        head_missing = self._head_limit - len(self._head)
        # 只有开头尚未收满时，才继续向头部追加。
        if head_missing > 0:
            # [:head_missing] 取当前块最前面的一段，extend 把这些字节追加进去。
            self._head.extend(chunk[:head_missing])
            # 从当前块去掉已经交给头部的部分，剩下部分再用来更新尾部。
            chunk = chunk[head_missing:]
        # 非空 bytes 在 if 中为真；空的 b"" 表示这次没有剩余字节。
        if chunk:
            # 新内容先接到尾部，稍后再裁掉过早的部分。
            self._tail.extend(chunk)
            # 尾部长度超过其额度时，需要删除最旧的字节。
            if len(self._tail) > self._tail_limit:
                # 例如长度 80、额度 50，删除开头 30 字节，只保留最后 50 字节。
                del self._tail[: len(self._tail) - self._tail_limit]

    def render(self) -> tuple[str, bool]:
        """将已保存的字节整理成给模型看的文字。

        不接收额外参数；返回 (文字, 是否截断)，例如 ("hello", False)。
        超限时会为“已截断”的提示预留空间，提示本身也算进输出字节上限。
        """
        # 总共收到的内容没超过额度时，头尾相连仍是完整的原始输出。
        if self.total_bytes <= self._limit_bytes:
            # + 连接两个字节数组，bytes() 得到不可修改的字节串。
            data = bytes(self._head + self._tail)
            # UTF-8 解码成文字；无法解码的字节会被跳过，False 表示没有因长度超限截断。
            return data.decode("utf-8", errors="ignore"), False

        # 已经超限，准备提示原始输出实际有多少字节。
        marker = (
            # 两端的换行让截断提示与头尾文字分开显示。
            f"\n... [output truncated: {self.total_bytes} bytes total] ...\n"
        # 把提示转换为字节，才能与按字节计算的限额相减。
        ).encode("utf-8")
        # 如果上限小到连完整提示也放不下，只能保留提示的前面一段。
        if len(marker) >= self._limit_bytes:
            # 切片确保最终保留的原始字节不超过限额。
            data = marker[: self._limit_bytes]
        # 提示放得下时，剩余空间分给真正的命令输出。
        else:
            # 例如限额 200、提示占 60，就还剩 140 字节可放原始内容。
            kept = self._limit_bytes - len(marker)
            # 将剩余空间的一半给输出开头。
            head_size = kept // 2
            # 另一半给输出末尾，多出的一个字节也放在末尾。
            tail_size = kept - head_size
            # 拼成“开头 + 截断提示 + 末尾”，再统一变成 bytes。
            data = bytes(
                # 从头部保留最早的一段。
                self._head[:head_size]
                # 中间加上截断提示。
                + marker
                # 负数切片取最后几个字节；tail_size=0 时必须用空字节串。
                # 因为 [-0:] 等于 [0:]，会错误地取出整个尾部。
                + (self._tail[-tail_size:] if tail_size else b"")
            )
        # 切片可能切在一个汉字的中间，ignore 会跳过不完整字节；True 表示发生过截断。
        return data.decode("utf-8", errors="ignore"), True


# dataclass 自动生成按字段名接收参数的初始化方法；frozen=True 防止字段被随意改写。
@dataclass(frozen=True)
class DockerRunnerConfig:
    """保存“用什么容器、允许使用多少资源、等待多久”等设置。

    创建时可按字段名传入下面十项配置；省略的字段使用等号右边的默认值。
    得到一个配置对象，供 DockerCommandRunner 和 ThreadSandboxManager 读取。
    例子：DockerRunnerConfig(timeout_seconds=10, memory_limit="256m")。
    创建配置只会检查参数，不会下载镜像、启动容器或执行任何模型命令。
    """

    # 镜像是创建容器时使用的系统与程序模板；默认模板自带 Python 3.12。
    image: str = "python:3.12-slim"
    # Runner 等待执行进程退出的时间上限，单位秒；默认 60 秒。
    timeout_seconds: float = 60.0
    # 最终保留的命令输出字节上限；20_000 与 20000 数值相同，下划线只方便阅读。
    max_output_bytes: int = 20_000
    # 容器内存额度，默认 512 × 1024 × 1024 字节；后面会转换成 --memory 的值。
    memory_limit: str = "512m"
    # CPU 时间额度，1.0 表示最多约一个 CPU 核心的运行时间，不是绑定某个指定核心。
    cpu_limit: float = 1.0
    # 容器内允许的进程/线程任务数量上限，防止无限创建任务。
    pids_limit: int = 64
    # 是否连接 Docker bridge 网络；默认 False，使用 none 网络模式。
    network_enabled: bool = False
    # 服务器上调用哪个 Docker 程序；可填写完整路径，测试中也可以替换为测试程序。
    docker_binary: str = "docker"
    # 归还后闲置多久才可回收；600 秒就是 10 分钟，管理器使用这一项。
    idle_timeout_seconds: float = 600.0
    # 管理器每隔多久检查一次空闲集合；不是每个容器独立启动一个计时器。
    idle_check_interval_seconds: float = 60.0

    def __post_init__(self) -> None:
        """dataclass 初始化完字段后自动调用，提前检查配置能否使用。

        接收已经保存到 self 的配置；正常返回 None，发现问题则抛 ValueError。
        例子：DockerRunnerConfig(timeout_seconds=0) 会在创建对象时立即报错。
        """
        # strip 去空白；空字符串在 if 判断中为假，所以 not 检查的是“没有有效镜像名”。
        if not self.image.strip():
            # 避免稍后拼出没有镜像名称的 docker run。
            raise ValueError("Docker Sandbox 镜像不能为空")
        # 时间既要有限，也必须大于 0；NaN、无穷大、负数和 0 都不接受。
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            # 明确说明是哪项时间配置有问题。
            raise ValueError("Bash 命令超时必须是大于 0 的有限数字")
        # 输出至少要有一个字节的保存额度。
        if self.max_output_bytes <= 0:
            # 防止创建没有可用容量的输出缓冲区。
            raise ValueError("Bash 输出上限必须大于 0")
        # 复用前面的内存解析函数做校验；此处不改写 memory_limit 原来的文字值。
        _parse_memory_limit(self.memory_limit)
        # CPU 配额也必须是有限正数，例如 0.5、1.0、2.0。
        if not math.isfinite(self.cpu_limit) or self.cpu_limit <= 0:
            # 负额度或无法计算的数字没有实际意义。
            raise ValueError("Docker CPU 限制必须是大于 0 的有限数字")
        # 至少要允许创建一个进程任务。
        if self.pids_limit <= 0:
            # 提前拒绝无效的进程数量限制。
            raise ValueError("Docker PID 限制必须大于 0")
        # 服务器要启动的程序名称或路径不能为空。
        if not self.docker_binary.strip():
            # 路径是否真的存在，会在实际启动程序时再检查。
            raise ValueError("Docker 可执行文件不能为空")
        # 这两个字段具有同样的要求，因此把它们放在元组中逐个检查。
        for value in (self.idle_timeout_seconds, self.idle_check_interval_seconds):
            # 回收时限与检查间隔都必须是可以计时的有限正数。
            if not math.isfinite(value) or value <= 0:
                # 避免无效间隔造成不停检查，或无法按时计算回收条件。
                raise ValueError("容器闲置超时和检查间隔必须是大于 0 的有限数字")


class DockerCommandRunner:
    """负责“怎样执行”，由管理器决定“何时使用哪个容器”。

    创建时接收 DockerRunnerConfig；得到可执行容器操作的 Python 对象。
    应用常用路径：start_container() 创建可复用容器，run_in_container() 反复执行命令。
    独立验证路径：run() 创建一次性容器，命令结束后 Docker 自动移除它。
    两条路径都复用同一份参数构造、输出收集和异常处理逻辑。
    """

    def __init__(self, config: DockerRunnerConfig | None = None) -> None:
        """保存调用方给的配置，未传入时使用上面声明的默认配置。

        config 可为 DockerRunnerConfig 或 None；创建本对象不会启动 Docker。
        需要从环境变量读配置时，用文件末尾的 load_docker_runner_from_env()。
        """
        # or 在 config 为 None 时使用右边创建的默认配置对象。
        self.config = config or DockerRunnerConfig()

    # staticmethod 表示这个方法不需要 self，它只根据传入文字计算结果。
    @staticmethod
    def _name_part(value: str) -> str:
        """把一个 ID 清理成可用于容器名称的短片段。

        value 是 ID 文字；返回最多 12 个字符的名称片段。
        例子："Run/001" → "run-001"；"///" 清理为空后使用 "unknown"。
        """
        # 先小写，再把不允许的字符替换为 -，最后去掉两端的 -、.、_。
        cleaned = _SAFE_NAME_PART.sub("-", value.lower()).strip("-._")
        # [:12] 截取前 12 个字符；空字符串时用 unknown，保证名称片段不为空。
        return cleaned[:12] or "unknown"

    def build_run_args(
        self,  # 当前执行器，提供镜像、超时和资源限制等配置。
        *,  # 之后的参数必须按名字传入。
        command: str,  # 容器内要执行的命令，例如 "cat ../uploads/input.txt"。
        workspace_path: str,  # 服务器真实工作目录，例如 /data/t1/workspace。
        run_id: str,  # 本轮执行 ID，独立运行时用于生成容器名。
        tool_call_id: str,  # 本次工具调用 ID，同样用于生成可识别的名字。
    ) -> tuple[str, list[str]]:
        """准备 docker run 要用的完整参数列表，此时还不会启动程序。

        返回 (容器名, 字符串列表)，调用方可用 container_name, args = ... 分别接收。
        argv 就是“程序接收到的参数列表”，例如 ["docker", "run", "--rm", ...]。
        本函数检查服务器目录、读取目录拥有者，并在内存中拼接参数。
        例子：/data/t1/uploads 会作为 /mnt/user-data/uploads 出现在容器里。
        这两个路径是同一份文件的两个访问入口，创建挂载不会复制一份文件。
        """
        # .parent 得到 /data/t1；ThreadPaths 再找到 workspace、uploads、outputs 三个目录。
        paths = ThreadPaths(Path(workspace_path).parent)
        # resolve(strict=True) 要求 workspace 已存在，并将路径规范化。
        workspace = paths.workspace_path.resolve(strict=True)
        # 传入的必须确实是标准 workspace，不能拿 uploads 的路径冒充工作目录。
        if workspace != Path(workspace_path).resolve(strict=True):
            # 如果不一致，就停止构造，避免挂载到错误的 Thread 目录。
            raise ValueError("workspace_path 必须指向 Thread 的 workspace 目录")

        # 用固定前缀、本轮 ID、工具调用 ID 和随机尾缀组成名字。
        container_name = (
            # f 字符串将花括号中的计算结果填进文字，例如 deer-mini-run-101-。
            f"deer-mini-{self._name_part(run_id)}-"
            # 两个相邻 f 字符串会自动连接；随机尾缀减少重名机会。
            f"{self._name_part(tool_call_id)}-{uuid4().hex[:8]}"
        )
        # stat 读取目录的元数据，后面要用 st_uid、st_gid 得到拥有者的数字 ID。
        stat = workspace.stat()
        # 准备一个只装字符串的列表，每个目录将贡献两个元素：--mount 和挂载说明。
        mount_args: list[str] = []
        # 每次从一对值里分别取出服务器路径和容器路径，这种写法叫“解包”。
        # 第一轮例子：host_path=/data/t1/workspace，virtual_path=/mnt/user-data/workspace。
        for host_path, virtual_path in paths.mount_pairs():
            # 三个真实目录都应在创建 Thread 时准备好，这里只检查是否确实为目录。
            if not host_path.is_dir():
                # 目录不存在或是普通文件时停止；name 只显示最后一段，例如 uploads。
                raise ValueError(
                    f"Thread 标准目录不存在或不是目录：{host_path.name}"
                )

            # f 字符串先得到 source=/data/t1/workspace。
            # replace('"', '""') 把路径里的一个双引号写成两个，符合 Docker 的 CSV 字段规则。
            # CSV 使用逗号分字段；普通路径没有引号时，这一步不会改变它。
            source_field = f"source={host_path}".replace('"', '""')
            # extend 把下面两个字符串依次加到列表末尾；三轮循环后共有六个元素。
            mount_args.extend([
                "--mount",  # 告诉 Docker：下一项是一组挂载配置。
                # bind 表示使用服务器现有目录；source 是服务器位置，target 是容器位置。
                # 完整 source 字段用双引号包住，因此 /data/thread,1/workspace 中的逗号不会分错字段。
                f'type=bind,"{source_field}",target={virtual_path}',
            ])
        # 条件表达式：开关为真时用 bridge 网络，为假时用 none 网络。
        network_mode = "bridge" if self.config.network_enabled else "none"
        # 每个列表元素都会作为一个独立参数交给服务器上的 Docker 程序。
        # 不把它们拼成服务器 Shell 命令，因此模型的分号等内容留在最后的 command 元素中。
        args = [
            self.config.docker_binary,  # 第一个元素是服务器要启动的程序，通常为 docker。
            "run",  # 请求创建并启动一个新容器。
            "--rm",  # 容器主进程退出后，让 Docker 自动移除这个容器。
            "--name",  # 下一项是容器名称。
            container_name,  # 使用刚刚生成的名称，后续能按它查询或清理。
            "--network",  # 下一项指定容器网络模式。
            network_mode,  # 默认 none；启用网络时是 bridge。
            "--read-only",  # 容器自己的根文件系统只读，后面单独挂载的三个目录仍按权限可写。
            "--tmpfs",  # 下一项指定一个存在于内存中的临时文件系统。
            # /tmp 可写、最多 64 MiB；不使用提权位和设备文件，禁止直接执行该目录里的程序。
            "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--cap-drop",  # 下一项指定要去掉的 Linux 权限能力。
            "ALL",  # 去掉这组额外权限能力。
            "--security-opt",  # 下一项提供一个容器安全选项。
            "no-new-privileges",  # 容器进程不能通过启动新程序获得更高权限。
            "--memory",  # 下一项限制容器可使用的内存。
            str(_parse_memory_limit(self.config.memory_limit)),  # 例如把 512m 换算为字符串 "536870912"。
            "--memory-swap",  # 下一项是内存与交换空间的总额度。
            str(_parse_memory_limit(self.config.memory_limit)),  # 与 memory 相等，表示不再额外给交换空间。
            "--cpus",  # 下一项限制 CPU 时间额度。
            str(self.config.cpu_limit),  # 例如 "1.0"；程序参数使用字符串。
            "--pids-limit",  # 下一项限制进程/线程任务数量。
            str(self.config.pids_limit),  # 默认 "64"。
            "--user",  # 下一项指定容器进程使用的数字用户和组。
            f"{stat.st_uid}:{stat.st_gid}",  # 与服务器 workspace 的拥有者对应，便于写入挂载文件。
            "--workdir",  # 下一项指定程序启动时的当前目录。
            VIRTUAL_WORKSPACE,  # 值为 /mnt/user-data/workspace，容器里的相对路径从这里开始。
            "--env",  # 下一项为容器设置一个环境变量。
            f"HOME={VIRTUAL_WORKSPACE}",  # 把 HOME 设置为容器里的工作目录。
            "--env",  # 再设置一项环境变量，每项都需要对应的 --env。
            "LANG=C.UTF-8",  # 使用支持 UTF-8 的语言环境。
            "--env",  # 继续设置 Python 输出相关配置。
            "PYTHONUNBUFFERED=1",  # 让 Python 尽早写出输出，方便外层及时读取管道。
        ]
        # 把前面准备的三组 --mount 参数放进完整列表，位置必须在镜像名称前。
        args.extend(mount_args)
        # Linux 容器共用服务器时钟；这里再共享时区文件，使 date 等程序按服务器时区显示。
        if Path("/etc/localtime").is_file():
            # 服务器存在时区文件时，增加一组只读挂载和一项环境变量。
            args.extend([
                "--mount",  # 告诉 Docker 后面还有一组文件挂载。
                "type=bind,source=/etc/localtime,target=/etc/localtime,readonly",  # 容器可读时区文件，不能写回。
                "--env",  # 下一项设置时区读取方式。
                "TZ=:/etc/localtime",  # 让程序从这个文件读取时区，避免镜像中的 UTC 默认值覆盖它。
            ])
        # Docker 选项结束后，依次追加镜像、容器中运行的程序以及它的参数。
        args.extend([
            self.config.image,  # 用这个镜像创建容器，例如 python:3.12-slim。
            "/bin/bash",  # 真正解释模型命令的是容器里的 Bash。
            "-lc",  # -l 使用登录 Shell 的启动方式；-c 执行后面这一个命令字符串。
            command,  # 例如 "cp ... && cat ..."，整段文字仍然只是参数列表中的一个元素。
        ])
        # 返回两项；到这里仍只是准备数据，调用者随后才会启动 Docker。
        return container_name, args

    async def run(
        self,  # 当前执行器。
        *,  # 后续参数按名称传入。
        command: str,  # 在新容器里执行的 Bash 命令。
        workspace_path: str,  # 当前 Thread 在服务器上的 workspace，用来确定三个挂载源。
        run_id: str,  # 本轮标识，用于一次性容器的名称。
        tool_call_id: str,  # 工具调用标识，也用于名称。
        user_id: str | None = None,  # 保持统一工具接口；这个一次性执行入口不使用该字段。
        thread_id: str | None = None,  # 同上；应用中的归属检查由共享管理器处理。
    ) -> CommandResult:
        """一次性创建容器、执行命令、收集结果，常用于独立验证。

        返回 CommandResult；docker run 带 --rm，主进程正常退出后容器自动移除。
        例子：直接 await runner.run(command="pwd", ...) 验证挂载与默认目录。
        应用通过管理器复用容器时，走 start_container() 和 run_in_container()。
        """
        # 解包返回的两项：container_name 是名字，args 是将传给 Docker 的参数列表。
        container_name, args = self.build_run_args(
            command=command,  # 保留整段模型命令，供容器里的 Bash 解释。
            workspace_path=workspace_path,  # 用服务器目录准备挂载。
            run_id=run_id,  # 为本轮生成名称片段。
            tool_call_id=tool_call_id,  # 为本次调用生成名称片段。
        )
        # 真正启动 Docker，等待执行和输出收集完成，再把结果交给调用方。
        return await self._run_process(container_name, args)

    async def start_container(
        self,  # 当前执行器。
        *,  # 后续参数按名称传入，避免混淆这些字符串。
        container_name: str,  # 管理器已经生成并登记的容器名称。
        workspace_path: str,  # 服务器 workspace，用来找到 workspace/uploads/outputs。
        user_id: str,  # 归属标签中的用户 ID。
        thread_id: str,  # 归属标签中的对话 ID。
        scope: str,  # 本项目的容器分组标记，启动清理时按它筛选。
    ) -> None:
        """创建一个可以给同一对话连续使用的后台容器。

        接收管理器指定的名称、目录和归属信息；成功返回 None，失败抛异常。
        例子：第一次 Bash 前创建容器 A，让它保持运行，后续命令用 docker exec 进入 A。
        容器主进程运行 sleep infinity；具体的模型命令由 run_in_container() 执行。
        """
        # 先复用一次性执行的参数构造，保证两种用法使用相同目录、权限和资源限制。
        # _ 表示不需要使用第一项临时名称；稍后用管理器给的名称替换它。
        _, args = self.build_run_args(
            # 此时不执行模型命令，所以 command 为空；workspace 用来准备真实挂载。
            command="", workspace_path=workspace_path,
            # 这两个值只满足名称构造的输入要求，生成的临时名称下一行就会被替换。
            run_id=thread_id, tool_call_id="sandbox",
        )
        # index 找到 --name 的位置，+1 是它后面的值，把这个值改成已登记的容器名称。
        args[args.index("--name") + 1] = container_name
        # [2:2] 是空切片，给它赋列表表示“插入”；插到 docker、run 两个元素之后。
        args[2:2] = [
            "--detach", "--init",  # detach 让容器在后台运行；init 帮助回收容器中已退出的子进程。
            "--label", f"deer-mini.sandbox-owner={scope}",  # 标记这是哪个项目创建的容器。
            "--label", f"deer-mini.user-id={user_id}",  # 标记归属用户，便于查看和排查。
            "--label", f"deer-mini.thread-id={thread_id}",  # 标记归属对话。
        ]
        # 原列表最后三项是 /bin/bash、-lc、command；把这三项换成 sleep、infinity 两项。
        # 镜像仍在它们前面。sleep infinity 保持容器主进程存活，等待之后执行其他命令。
        args[-3:] = ["sleep", "infinity"]
        # 因为使用了 detach，这里等待的是创建命令结束，不是等待 sleep infinity 结束。
        result = await self._run_process(container_name, args)
        # 创建超时，或 Docker 返回非零退出码，说明不能继续把这个容器交给 Bash。
        if result.timed_out or result.exit_code != 0:
            # Docker 的输出通常包含具体失败原因，将它附在错误说明后。
            raise RuntimeError(f"创建 Thread 容器失败：{result.output}")

    async def run_in_container(
        self, *, container_name: str, command: str
    ) -> CommandResult:
        """在已经运行的容器中执行一条命令，返回 CommandResult。

        container_name 指定管理器已分配的容器；command 是模型给出的 Bash 文字。
        每条命令都启动新的 Bash，并从 /mnt/user-data/workspace 开始。
        例子：上一条运行 cd ../outputs，下一条 pwd 仍输出 /mnt/user-data/workspace。
        同一容器中写入的文件可以继续存在，但上一条 Shell 的 cd、export 不会自动延续。
        """
        # 准备 docker exec 参数并等待命令执行完成，沿用统一的输出和超时处理。
        return await self._run_process(container_name, [
            # exec 表示进入已有容器执行程序；workdir 明确本次程序的起始目录。
            self.config.docker_binary, "exec", "--workdir", VIRTUAL_WORKSPACE,
            # 先指定容器，再指定容器里的 Bash；command 始终是单独的一个参数元素。
            container_name, "/bin/bash", "-lc", command,
        ])

    async def _docker_query(self, *args: str) -> tuple[int, str]:
        """执行输出较少的 Docker 查询，返回退出码和去掉两端空白的输出。

        *args 接收任意个字符串，例如 ("inspect", "--format", "{{.State.Running}}", "A")。
        返回 (code, output)，例如 (0, "true")。这里只使用于少量状态查询。
        查询超时或被取消时会停止服务器上的查询进程，并继续报告异常。
        """
        # 列表里的 *args 将多项参数展开，例如拼成 ["docker", "inspect", ...]。
        process = await self._start_process([self.config.docker_binary, *args])
        # 启动后要等待查询输出；异常时也要处理已经启动的进程。
        try:
            # communicate 读取输出并等待进程退出；wait_for 最多等待 10 秒。
            # 它返回普通输出和错误输出两项；本项目已合并两者，第二项用 _ 忽略。
            output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        # 包含普通错误和取消，确保查询中断时不会留下查询进程继续等待。
        except BaseException:
            # returncode 为 None 表示 asyncio 目前还没有拿到进程退出结果。
            if process.returncode is None:
                # 检查与发信号之间，进程可能自行退出，所以这里还要接住这种情况。
                try:
                    # 停止服务器上的 Docker 查询客户端；这不是删除容器的操作。
                    process.kill()
                # 查询进程已经结束时，操作系统可能报告找不到进程。
                except ProcessLookupError:
                    # pass 表示不额外处理，继续往下确认进程已经退出。
                    pass
            # 等进程退出并收集结束状态，避免留下未处理的子进程。
            await process.wait()
            # 继续抛出原查询异常或取消，不返回一个冒充成功的查询结果。
            raise
        # 解码查询文字，替换无法解码的字节，并去掉两端空白，方便比较 true/false。
        return process.returncode, output.decode("utf-8", errors="replace").strip()

    async def container_is_running(self, container_name: str) -> bool:
        """确认指定容器是否还在运行，供管理器判断能否复用。

        container_name 是登记卡上的名字；返回 True 或 False。
        False 表示已停止或确认不存在；连接失败等无法确认的情况会抛异常。
        例子：用户手动停止容器 A，下一条 Bash 前检查得到 False，然后管理器重建。
        """
        # inspect 查看容器状态；format 只取 Running 这个布尔值，避免读取大量无关信息。
        code, output = await self._docker_query(
            "inspect", "--format", "{{.State.Running}}", container_name
        )
        # 查询成功，并且输出确实为我们预期的两种布尔文字之一。
        if code == 0 and output in {"true", "false"}:
            # 字符串比较产生布尔值："true" 返回 True，"false" 返回 False。
            return output == "true"
        # Docker 明确说明对象不存在时，也属于不可复用的已知状态。
        if "no such object" in output.lower() or "no such container" in output.lower():
            # 允许上层按“旧容器已经没有了”继续处理。
            return False
        # 例如权限不足或无法连接 Docker，都不能假装成“不存在”。
        raise RuntimeError(f"无法确认 Docker 容器状态：{output}")

    async def remove_container(self, container_name: str) -> None:
        """给管理器提供删除指定容器的入口。

        container_name 是要清理的名字；成功返回 None，无法确认删除则抛清理异常。
        具体重试逻辑集中在 _remove_container()，这里直接复用。
        """
        # 等待删除完成；不提前告诉管理器“已经删掉”。
        await self._remove_container(container_name)

    async def remove_owned_containers(self, scope: str) -> None:
        """按项目标记找到一组容器并逐个删除，用于后端启动清理。

        scope 是 start_container() 写入的 sandbox-owner 标签值；返回 None。
        例子：项目 A 的标记是 app-a，只查找和删除带 sandbox-owner=app-a 的容器。
        查询包含运行中和已停止的容器，因此该入口按 Mini 的单进程启动约定使用。
        """
        # ps 列出容器；all 包含已停止的；quiet 只输出 ID；filter 按标签筛选。
        code, output = await self._docker_query(
            "ps", "--all", "--quiet", "--filter", f"label=deer-mini.sandbox-owner={scope}"
        )
        # 列表查询失败时，不能声称已经检查完旧容器。
        if code != 0:
            # 将查询错误交给启动流程决定如何处理。
            raise RuntimeError(f"无法检查遗留 Thread 容器：{output}")
        # quiet 的结果是一行一个 ID；splitlines 将各行拆成列表，空输出对应空列表。
        for container_id in output.splitlines():
            # 等当前容器删除后再处理下一个，任何未解决的失败都会向上报告。
            await self._remove_container(container_id)

    async def _run_process(self, container_name: str, args: list[str]) -> CommandResult:
        """启动 Docker 客户端，收集输出，并处理这一段执行中的超时和取消。

        container_name 是必要时用来清理的容器名；args 是完整程序参数列表。
        正常或已清理的超时返回 CommandResult；启动失败、清理失败、取消则报告异常。
        例子：args 是 docker exec A ... cat input.txt，收集 cat 的输出并返回。
        分成两个阶段：先拿到客户端进程对象，再一边读输出、一边等客户端退出。
        """
        # 把“创建客户端进程”安排成独立任务，以便稍后保护它不被外层取消直接打断。
        launch_task = asyncio.create_task(
            self._start_process(args),  # 返回 Process 对象后，才能查询、等待或停止这个客户端。
            name=f"bash-start-{container_name}",  # 调试时能看出任务属于哪个容器。
        )
        # 第一阶段：等待拿到客户端的 Process 对象。
        try:
            # shield 保护 launch_task。外层可收到取消，但启动任务仍有机会完成。
            # 否则可能已经启动了 docker 程序，却丢失 Process 对象，无法方便地停止它。
            process = await asyncio.shield(launch_task)
        # 例如用户恰好在客户端刚启动、Process 尚未返回时按下停止。
        except asyncio.CancelledError:
            # 再次等待同一个启动任务，尝试拿到已经在创建中的进程对象。
            try:
                # 这里没有再次启动 Docker，只是在等原来的 launch_task。
                process = await asyncio.shield(launch_task)
            # 可能启动本身失败，或者在这次等待中又收到取消。
            except BaseException:
                # 即使没有拿到 Process，也知道预先登记的容器名，可以尝试按名称清理。
                try:
                    # 清理可能已经被 Docker 后台服务创建出来的容器。
                    await self._remove_container(container_name)
                # 普通清理错误在这里记日志，随后继续报告当前启动异常或取消。
                except Exception:
                    # exception 会同时记录当前异常的调用栈。
                    logger.exception(
                        "取消 Bash 启动时无法清理容器 %s",  # %s 是日志参数的占位位置。
                        container_name,  # 实际替换进占位位置的容器名。
                    )
                # 将这次捕获的启动错误或再次取消交回调用链。
                raise
            # 拿到 Process 后，可以先停止客户端，再按名称清理容器。
            try:
                # 等待停止和清理完成，防止用户已取消但容器任务继续执行。
                await self._cleanup_cancelled_process(
                    process,  # 服务器上的 Docker 客户端进程。
                    container_name,  # Docker 后台服务管理的容器名称。
                )
            # 清理失败时记录原因，不把这次取消伪装成普通成功。
            except Exception:
                # 日志文字与异常栈一起帮助定位无法清理的对象。
                logger.exception(
                    "取消 Bash 启动时无法清理容器 %s",
                    container_name,
                )
            # 继续向上传递外层已经收到的取消。
            raise
        # 启动可执行程序时找不到文件，常见原因是服务器没有安装 Docker CLI。
        except FileNotFoundError as error:
            # 改成应用可理解的错误说明，同时保留原始操作系统错误。
            raise RuntimeError(
                f"找不到 Docker 可执行文件：{self.config.docker_binary}"
            ) from error
        # 其他启动错误，例如没有执行权限或操作系统无法创建进程。
        except OSError as error:
            # 包装错误信息，from error 保留原始原因。
            raise RuntimeError(f"启动 Docker 失败：{error}") from error

        # _start_process 用 stdout=PIPE 创建了读取端，因此这里必须拿得到 stdout。
        assert process.stdout is not None
        # 为这一次命令创建独立输出缓冲区，按配置限制长期保存的字节数。
        capture = _BoundedCapture(self.config.max_output_bytes)
        # 启动读输出的后台任务，让读取与等待进程退出同时进行。
        # 如果只等退出而不读，进程可能因输出管道写满而卡住。
        drain_task = asyncio.create_task(
            self._drain_output(process.stdout, capture),  # 不断把读取到的字节交给 capture。
            name=f"bash-output-{container_name}",  # 任务名称只用于调试识别。
        )

        # 第二阶段：等待退出；外层 try 接取消，内层 try 处理本执行器的超时。
        try:
            # 把有限时间的等待包起来，超时后进入下面的 TimeoutError 分支。
            try:
                # wait_for 等待指定操作，同时启动一个超时计时。
                await asyncio.wait_for(
                    process.wait(),  # 等待 Docker 客户端退出；在 exec 用法中通常对应命令结束。
                    timeout=self.config.timeout_seconds,  # 默认最多等 60 秒。
                )
            # 到时仍没退出：仅取消 wait() 不会自动停止客户端或容器，需要主动清理。
            except TimeoutError:
                # finally 确保清理操作结束后还会等待输出读取任务。
                try:
                    # 先停客户端，再删容器，从而结束其中可能还在运行的 Bash 命令。
                    await self._cleanup_cancelled_process(
                        process,  # 要停止的客户端 Process 对象。
                        container_name,  # 要停止并移除的容器。
                    )
                # 无论上面的清理是否抛异常，都会进入这里处理输出任务。
                finally:
                    # 等管道关闭并读完残余输出，避免漏掉已经产生的最后一段文字。
                    await drain_task
                # 清理成功才会到这里；如果清理抛异常，就继续向上传递，不声称清理完成。
                output, truncated = capture.render()
                # 返回带明确超时标记的结果，让 BashTool 告诉模型这条命令未正常完成。
                return CommandResult(
                    output=output,  # 截止停止时已经收集到的输出。
                    exit_code=None,  # 不把强制停止包装成一个正常的命令退出码。
                    timed_out=True,  # 标明发生了执行超时。
                    output_truncated=truncated,  # 输出是否因为超过字节额度而截断。
                )
        # 在这一段等待期间，用户停止或上层 Run 超时可能通过取消传递到这里。
        except asyncio.CancelledError:
            # 把清理变成独立任务，便于保护它，尽量等清理结束再结束本次调用。
            cleanup_task = asyncio.create_task(
                self._cleanup_cancelled_process(process, container_name),  # 清理客户端和容器。
                name=f"bash-cleanup-{container_name}",  # 给后台清理任务一个可识别名字。
            )
            # 分别处理等待被再次取消，以及清理本身出现普通错误。
            try:
                # 外层取消不直接传给 cleanup_task；它仍可继续尝试完成清理。
                await asyncio.shield(cleanup_task)
            # 等待过程中再次收到取消，仍尝试等待已经开始的清理任务。
            except asyncio.CancelledError:
                # 这里等待原任务，不重新发起一套删除流程。
                await cleanup_task
            # 如果清理失败，记下不能确认容器已删除这一事实。
            except Exception:
                # 不能通过一条普通成功输出掩盖清理问题，因此保留错误日志。
                logger.exception(
                    "取消 Bash 时无法确认容器 %s 已删除",
                    container_name,
                )
            # 清理处理后，让读取任务消费完管道中已经留下的内容。
            await drain_task
            # 取消是控制执行流程的异常，继续交给 Runtime 完成本轮收尾。
            raise

        # 正常退出也要等管道读完：进程结束时，管道里可能还留着尚未消费的文字。
        await drain_task
        # 取出最终显示文字和截断标记。
        output, truncated = capture.render()
        # 返回完整结果；非零退出码也保留给 BashTool 判断，而不是在这里统一改成异常。
        return CommandResult(
            output=output,  # 合并后的普通输出与错误输出。
            exit_code=process.returncode,  # 例如 0 表示正常成功，7 表示命令返回了错误码 7。
            output_truncated=truncated,  # 是否只保留了输出头尾；timed_out 使用默认 False。
        )

    async def _start_process(self, args: list[str]) -> Process:
        """启动服务器上的 Docker 命令行程序，返回可管理的 Process 对象。

        args 例如 ["docker", "exec", "--workdir", "/mnt/user-data/workspace", ...]。
        返回时表示客户端进程已经创建；并不表示容器里的命令已经执行完。
        单独做成一个方法，测试就能模拟“刚启动时恰好收到取消”的情况。
        """
        # create_subprocess_exec 直接启动第一个参数指定的程序，不先经过服务器 Shell。
        return await asyncio.create_subprocess_exec(
            *args,  # * 将列表展开成多个参数，效果类似传入 "docker", "exec", ...。
            stdin=asyncio.subprocess.DEVNULL,  # 不提供交互输入，避免命令等待终端键盘输入。
            stdout=PIPE,  # 将普通输出交给 Python 读取，Process.stdout 因此是读取对象。
            stderr=STDOUT,  # 把错误输出合并进普通输出，最终统一放到 CommandResult.output。
            start_new_session=True,  # 在 Linux 上建立独立会话/进程组，清理时可按组发信号。
        )

    # 这里只使用传入的 stream、capture，不读取 self，所以定义为静态方法。
    @staticmethod
    async def _drain_output(
        stream: asyncio.StreamReader,  # 连接着 Docker 客户端输出的异步读取端。
        capture: _BoundedCapture,  # 本次命令专属的有限容量缓冲区。
    ) -> None:
        """不断读取输出，交给 capture，直到管道结束。

        stream 提供原始字节；capture 决定保留哪些字节；返回 None。
        例子：一条命令打印一大段报告，会分多次读入，而不是一次读取到内存中。
        """
        # := 同时赋值和判断：先 await 读取至多 64 KiB，再把结果保存到 chunk。
        # 读到非空 bytes 就进入循环；读到 b"" 表示输出结束，while 自动退出。
        while chunk := await stream.read(64 * 1024):
            # 即使保存额度已经用完，也继续读，只由 capture 丢弃过长的中间部分。
            capture.append(chunk)

    async def _cleanup_cancelled_process(
        self,  # 当前执行器，提供 Docker 程序位置及删除方法。
        process: Process,  # 服务器上的 Docker CLI 进程，不是容器内的 Bash 进程对象。
        container_name: str,  # 需要停止并移除的容器名称。
    ) -> None:
        """超时或取消时，处理客户端进程，再清理容器。

        接收本次客户端 Process 和已知容器名；成功返回 None，失败会报告异常。
        例子：容器里执行 sleep 60，用户点停止，不能只断开 docker exec 客户端。
        还需要删除容器，才能停止里面可能继续执行的任务；挂载文件仍保存在服务器目录。
        """
        # 还没有退出码时，认为客户端仍需要停止；已经退出就跳过客户端停止步骤。
        if process.returncode is None:
            # 优先结束整个客户端进程组，包含它可能派生出的同组子进程。
            try:
                # _start_process 使用独立会话，组 ID 对应客户端 PID，不会指向整个后端进程组。
                # SIGTERM 是请求进程终止的信号，给程序一个自行结束的机会。
                os.killpg(process.pid, signal.SIGTERM)
            # 找不到进程、缺少权限等情况下，再尝试对当前 Process 单独发送终止请求。
            except (ProcessLookupError, PermissionError, OSError):
                # terminate 在这里对客户端进程发送 SIGTERM。
                process.terminate()
            # 发出信号不等于已经退出，需要再等待结束状态。
            try:
                # 给温和终止最多 2 秒时间。
                await asyncio.wait_for(process.wait(), timeout=2)
            # 2 秒后仍不退出，就升级为强制停止。
            except TimeoutError:
                # 仍优先处理整个客户端进程组。
                try:
                    # SIGKILL 是强制结束信号，进程不能像普通信号那样自行忽略它。
                    os.killpg(process.pid, signal.SIGKILL)
                # 对进程组操作失败时，退回单进程操作。
                except (ProcessLookupError, PermissionError, OSError):
                    # kill 对这个客户端进程发送强制结束信号。
                    process.kill()
                # 等待操作系统确认进程结束，并收集它的退出状态。
                await process.wait()

        # 超时可能发生在容器尚在创建时；先结束创建客户端，再按名称清理，减少前后交错。
        # 这种“两个操作发生顺序不固定而影响结果”的情况常称为竞态。
        # _remove_container 对删除失败进行有限重试，明确不存在则视为已经清理。
        await self._remove_container(container_name)

    async def _remove_container(self, container_name: str) -> None:
        """按指定名称停止并移除容器，失败时最多尝试五次。

        container_name 是要清理的名字；成功返回 None。
        例子：删除暂时失败，就等待 0.1 秒再试；五次仍失败则抛 SandboxCleanupError。
        Docker 返回成功或明确说明容器不存在，才视为完成；连接失败不会算作已删除。
        删除容器不会把 bind 挂载的服务器目录当作容器内部文件一起删除。
        """
        # 先准备兜底说明，之后每次失败用较具体的原因替换它。
        last_error = "未知错误"
        # range(5) 依次得到 0、1、2、3、4，一共执行五次尝试。
        for attempt in range(5):
            # 删除程序可能无法启动，也可能启动后返回错误码，分别处理。
            try:
                # 单独启动一个清理客户端，不经过 _run_process，避免清理又递归触发清理。
                remover = await asyncio.create_subprocess_exec(
                    self.config.docker_binary,  # 服务器 Docker CLI 的名称或路径。
                    "rm",  # 请求移除指定容器。
                    "-f",  # 容器仍在运行时，先强制停止再移除。
                    container_name,  # 只操作这一个已知名字，不使用宽泛清理命令。
                    stdin=asyncio.subprocess.DEVNULL,  # 清理过程不接受键盘输入。
                    stdout=asyncio.subprocess.DEVNULL,  # 成功输出的容器名不需要再保存。
                    stderr=PIPE,  # 保存错误说明，用来区分“不存在”与真正的清理故障。
                )
                # 给这一次清理客户端的退出等待设置单独的时间限制。
                try:
                    # 最多等 10 秒，不使用用户 Bash 命令本身的超时额度。
                    await asyncio.wait_for(remover.wait(), timeout=10)
                # 超时就停止这个清理客户端，稍后根据错误状态决定是否重试。
                except TimeoutError:
                    # 强制停止当前 docker rm 客户端。
                    remover.kill()
                    # 等客户端确认退出，避免遗留未处理的进程对象。
                    await remover.wait()
                # 先用空字节串作为默认错误输出，兼容没有 stderr 读取端的情况。
                stderr = b""
                # 因为上面指定了 stderr=PIPE，正常会得到这个读取端。
                if remover.stderr is not None:
                    # 读出已结束客户端留下的错误文字字节。
                    stderr = await remover.stderr.read()
                # 转成可比较的文字，替换无效编码，并去掉换行等两端空白。
                error_text = stderr.decode("utf-8", errors="replace").strip()
                # 删除成功，或明确说明容器不存在，二者都意味着无需继续删除。
                if remover.returncode == 0 or "no such container" in error_text.lower():
                    # 立刻结束整个函数，后续尝试不会再运行。
                    return
                # 有具体文字就保留文字，没有文字时至少保存退出码。
                last_error = error_text or f"exit code {remover.returncode}"
            # 连 Docker 删除程序都无法启动，例如程序不存在或没有执行权限。
            except (FileNotFoundError, OSError):
                # 保存清理命令无法启动的说明，循环仍可以继续尝试。
                last_error = f"无法启动 docker rm：{self.config.docker_binary}"
            # 最后一次失败后不用再等待，因为已经没有下一次尝试。
            if attempt < 4:
                # 让出执行机会 0.1 秒，然后进入下一轮循环重试。
                await asyncio.sleep(0.1)
        # 走到这里说明五次都未确认删除，必须报告清理失败。
        raise SandboxCleanupError(
            # 将容器名与最后一次原因一起交给上层管理器和日志。
            f"无法删除 Docker 容器 {container_name}：{last_error}"
        )


def load_docker_runner_from_env() -> DockerCommandRunner | None:
    """按当前进程的环境配置，决定是否创建 Bash 使用的 Docker 执行器。

    没有显式参数；读取下面这些 DEER_MINI_* 环境变量。
    返回 DockerCommandRunner，或在 Bash 关闭时返回 None。
    例子：DEER_MINI_BASH_ENABLED=true、DEER_MINI_BASH_TIMEOUT_SECONDS=12.5，
    会创建等待额度为 12.5 秒的执行器；真正创建容器仍在第一次 Bash 调用时。
    .env 文件由应用入口加载，这里只读取已进入进程环境的配置文字。
    """
    # 未配置时默认关闭；_load_bool 把 true/false 等文字转换成布尔值。
    if not _load_bool("DEER_MINI_BASH_ENABLED", False):
        # 上层拿到 None 后不会在工具表中注册 Bash。
        return None

    # 按字段名构造配置对象，随后 __post_init__ 会统一检查这些值是否合法。
    config = DockerRunnerConfig(
        image=os.getenv("DEER_MINI_BASH_IMAGE", "python:3.12-slim"),  # 容器镜像模板。
        # 命令等待时间允许小数，因此使用 _load_float。
        timeout_seconds=_load_float(
            "DEER_MINI_BASH_TIMEOUT_SECONDS",  # 配置项名称。
            "60",  # 未设置时默认 60 秒。
        ),
        # 输出额度按整数个字节计算。
        max_output_bytes=_load_int(
            "DEER_MINI_BASH_MAX_OUTPUT_BYTES",  # 控制最终保留多少输出字节。
            "20000",  # 默认 20000 字节；原始管道仍持续读取。
        ),
        memory_limit=os.getenv("DEER_MINI_BASH_MEMORY_LIMIT", "512m"),  # 保留带单位的文字，稍后换算。
        cpu_limit=_load_float("DEER_MINI_BASH_CPU_LIMIT", "1"),  # 默认一个核心的 CPU 时间额度。
        pids_limit=_load_int("DEER_MINI_BASH_PIDS_LIMIT", "64"),  # 默认最多 64 个进程/线程任务。
        # 网络是开关，使用统一的布尔值解析方法。
        network_enabled=_load_bool(
            "DEER_MINI_BASH_NETWORK_ENABLED",  # 是否启用 bridge 网络。
            False,  # 未设置时使用 none 网络模式。
        ),
        docker_binary=os.getenv("DEER_MINI_DOCKER_BINARY", "docker"),  # 服务器上使用哪个 Docker 程序。
        idle_timeout_seconds=_load_float("DEER_MINI_SANDBOX_IDLE_TIMEOUT_SECONDS", "600"),  # 归还后闲置多久可回收。
        idle_check_interval_seconds=_load_float("DEER_MINI_SANDBOX_CHECK_INTERVAL_SECONDS", "60"),  # 后台多久检查一次。
    )
    # 返回装好配置的执行器对象；此处仍然不执行任何模型命令。
    return DockerCommandRunner(config)
