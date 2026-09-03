"""在一次性受限 Docker 容器中执行命令。"""

import asyncio
from asyncio.subprocess import PIPE, STDOUT, Process
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import re
import signal
from uuid import uuid4

from app.sandbox.base import CommandResult


_SAFE_NAME_PART = re.compile(r"[^a-z0-9_.-]+")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_MEMORY_RE = re.compile(
    r"^(?P<number>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>[kmgt]?i?b?)?$",
    re.IGNORECASE,
)
_MEMORY_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "ki": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "ti": 1024**4,
    "tib": 1024**4,
}
_MIN_MEMORY_BYTES = 6 * 1024**2
logger = logging.getLogger(__name__)


class SandboxCleanupError(RuntimeError):
    """Docker 容器无法确认已删除。"""


def _parse_memory_limit(value: str) -> int:
    match = _MEMORY_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("Docker 内存限制格式无效")
    number = float(match.group("number"))
    unit = (match.group("unit") or "").lower()
    if unit not in _MEMORY_UNITS or not math.isfinite(number):
        raise ValueError("Docker 内存限制格式无效")
    memory_bytes = int(number * _MEMORY_UNITS[unit])
    if memory_bytes < _MIN_MEMORY_BYTES:
        raise ValueError("Docker 内存限制必须至少为 6 MiB")
    return memory_bytes


def _load_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


def _load_float(name: str, default: str) -> float:
    raw_value = os.getenv(name, default)
    try:
        return float(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} 必须是数字") from error


def _load_int(name: str, default: str) -> int:
    raw_value = os.getenv(name, default)
    try:
        return int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} 必须是整数") from error


class _BoundedCapture:
    """持续排空管道，但只保存输出头尾，避免无限输出占满内存。"""

    def __init__(self, limit_bytes: int) -> None:
        self._limit_bytes = limit_bytes
        self._head_limit = limit_bytes // 2
        self._tail_limit = limit_bytes - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.total_bytes = 0

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        head_missing = self._head_limit - len(self._head)
        if head_missing > 0:
            self._head.extend(chunk[:head_missing])
            chunk = chunk[head_missing:]
        if chunk:
            self._tail.extend(chunk)
            if len(self._tail) > self._tail_limit:
                del self._tail[: len(self._tail) - self._tail_limit]

    def render(self) -> tuple[str, bool]:
        if self.total_bytes <= self._limit_bytes:
            data = bytes(self._head + self._tail)
            return data.decode("utf-8", errors="ignore"), False

        marker = (
            f"\n... [output truncated: {self.total_bytes} bytes total] ...\n"
        ).encode("utf-8")
        if len(marker) >= self._limit_bytes:
            data = marker[: self._limit_bytes]
        else:
            kept = self._limit_bytes - len(marker)
            head_size = kept // 2
            tail_size = kept - head_size
            data = bytes(
                self._head[:head_size]
                + marker
                + (self._tail[-tail_size:] if tail_size else b"")
            )
        return data.decode("utf-8", errors="ignore"), True


@dataclass(frozen=True)
class DockerRunnerConfig:
    """Docker 命令的安全边界与资源限制。"""

    image: str = "python:3.12-slim"
    timeout_seconds: float = 60.0
    max_output_bytes: int = 20_000
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    pids_limit: int = 64
    network_enabled: bool = False
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        if not self.image.strip():
            raise ValueError("Docker Sandbox 镜像不能为空")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Bash 命令超时必须是大于 0 的有限数字")
        if self.max_output_bytes <= 0:
            raise ValueError("Bash 输出上限必须大于 0")
        _parse_memory_limit(self.memory_limit)
        if not math.isfinite(self.cpu_limit) or self.cpu_limit <= 0:
            raise ValueError("Docker CPU 限制必须是大于 0 的有限数字")
        if self.pids_limit <= 0:
            raise ValueError("Docker PID 限制必须大于 0")
        if not self.docker_binary.strip():
            raise ValueError("Docker 可执行文件不能为空")


class DockerCommandRunner:
    """为每次工具调用创建一个独立、用完即删的容器。"""

    def __init__(self, config: DockerRunnerConfig | None = None) -> None:
        self.config = config or DockerRunnerConfig()

    @staticmethod
    def _name_part(value: str) -> str:
        cleaned = _SAFE_NAME_PART.sub("-", value.lower()).strip("-._")
        return cleaned[:12] or "unknown"

    def build_run_args(
        self,
        *,
        command: str,
        workspace_path: str,
        run_id: str,
        tool_call_id: str,
    ) -> tuple[str, list[str]]:
        """构造 argv；模型命令只作为容器内 Bash 的一个独立参数。"""
        workspace = Path(workspace_path).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("Thread Workspace 不是目录")

        container_name = (
            f"deer-mini-{self._name_part(run_id)}-"
            f"{self._name_part(tool_call_id)}-{uuid4().hex[:8]}"
        )
        stat = workspace.stat()
        # Docker --mount 使用 CSV 解析。把整个 source=... 字段加引号，
        # 并按 CSV 规则把路径中的双引号写成两个双引号。
        source_field = f"source={workspace}".replace('"', '""')
        mount_spec = f'type=bind,"{source_field}",target=/workspace'
        network_mode = "bridge" if self.config.network_enabled else "none"
        args = [
            self.config.docker_binary,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            network_mode,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            str(_parse_memory_limit(self.config.memory_limit)),
            "--memory-swap",
            str(_parse_memory_limit(self.config.memory_limit)),
            "--cpus",
            str(self.config.cpu_limit),
            "--pids-limit",
            str(self.config.pids_limit),
            "--user",
            f"{stat.st_uid}:{stat.st_gid}",
            "--workdir",
            "/workspace",
            "--mount",
            mount_spec,
            "--env",
            "HOME=/workspace",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "PYTHONUNBUFFERED=1",
            self.config.image,
            "/bin/bash",
            "-lc",
            command,
        ]
        return container_name, args

    async def run(
        self,
        *,
        command: str,
        workspace_path: str,
        run_id: str,
        tool_call_id: str,
    ) -> CommandResult:
        container_name, args = self.build_run_args(
            command=command,
            workspace_path=workspace_path,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
        launch_task = asyncio.create_task(
            self._start_process(args),
            name=f"bash-start-{container_name}",
        )
        try:
            # shield 保证外层取消不会把“正在创建容器”的任务一并取消；
            # 这样下面仍能拿到进程句柄并执行 rm -f。
            process = await asyncio.shield(launch_task)
        except asyncio.CancelledError:
            try:
                process = await asyncio.shield(launch_task)
            except BaseException:
                # Docker CLI 可能尚未返回进程句柄；仍按名字尝试清理，
                # 但绝不让清理异常覆盖用户的取消操作。
                try:
                    await self._remove_container(container_name)
                except Exception:
                    logger.exception(
                        "取消 Bash 启动时无法清理容器 %s",
                        container_name,
                    )
                raise
            try:
                await self._cleanup_cancelled_process(
                    process,
                    container_name,
                )
            except Exception:
                logger.exception(
                    "取消 Bash 启动时无法清理容器 %s",
                    container_name,
                )
            raise
        except FileNotFoundError as error:
            raise RuntimeError(
                f"找不到 Docker 可执行文件：{self.config.docker_binary}"
            ) from error
        except OSError as error:
            raise RuntimeError(f"启动 Docker 失败：{error}") from error

        assert process.stdout is not None
        capture = _BoundedCapture(self.config.max_output_bytes)
        drain_task = asyncio.create_task(
            self._drain_output(process.stdout, capture),
            name=f"bash-output-{container_name}",
        )

        try:
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.config.timeout_seconds,
                )
            except TimeoutError:
                try:
                    await self._cleanup_cancelled_process(
                        process,
                        container_name,
                    )
                finally:
                    await drain_task
                output, truncated = capture.render()
                return CommandResult(
                    output=output,
                    exit_code=None,
                    timed_out=True,
                    output_truncated=truncated,
                )
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(
                self._cleanup_cancelled_process(process, container_name),
                name=f"bash-cleanup-{container_name}",
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
            except Exception:
                logger.exception(
                    "取消 Bash 时无法确认容器 %s 已删除",
                    container_name,
                )
            await drain_task
            raise

        await drain_task
        output, truncated = capture.render()
        return CommandResult(
            output=output,
            exit_code=process.returncode,
            output_truncated=truncated,
        )

    async def _start_process(self, args: list[str]) -> Process:
        """启动 Docker CLI；独立成方法以便测试启动取消竞态。"""
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=PIPE,
            stderr=STDOUT,
            start_new_session=True,
        )

    @staticmethod
    async def _drain_output(
        stream: asyncio.StreamReader,
        capture: _BoundedCapture,
    ) -> None:
        while chunk := await stream.read(64 * 1024):
            capture.append(chunk)

    async def _cleanup_cancelled_process(
        self,
        process: Process,
        container_name: str,
    ) -> None:
        """先结束 docker run 客户端，再强制删除可能已创建的容器。"""
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    process.kill()
                await process.wait()

        # 超时可能发生在 Docker 仍在创建容器时。若先 docker kill 再终止
        # 客户端，会出现 kill 报“不存在”，随后容器才启动的竞态。因此在
        # docker run 客户端结束后使用 rm -f，并短暂重试等待守护进程收尾。
        await self._remove_container(container_name)

    async def _remove_container(self, container_name: str) -> None:
        """删除容器；仅“容器不存在”视为已经清理。"""
        last_error = "未知错误"
        for attempt in range(5):
            try:
                remover = await asyncio.create_subprocess_exec(
                    self.config.docker_binary,
                    "rm",
                    "-f",
                    container_name,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=PIPE,
                )
                try:
                    await asyncio.wait_for(remover.wait(), timeout=10)
                except TimeoutError:
                    remover.kill()
                    await remover.wait()
                stderr = b""
                if remover.stderr is not None:
                    stderr = await remover.stderr.read()
                error_text = stderr.decode("utf-8", errors="replace").strip()
                if remover.returncode == 0 or "no such container" in error_text.lower():
                    return
                last_error = error_text or f"exit code {remover.returncode}"
            except (FileNotFoundError, OSError):
                last_error = f"无法启动 docker rm：{self.config.docker_binary}"
            if attempt < 4:
                await asyncio.sleep(0.1)
        raise SandboxCleanupError(
            f"无法删除 Docker 容器 {container_name}：{last_error}"
        )


def load_docker_runner_from_env() -> DockerCommandRunner | None:
    """按环境变量创建 Runner；默认关闭，关闭时不会暴露 Bash 工具。"""
    if not _load_bool("DEER_MINI_BASH_ENABLED", False):
        return None

    config = DockerRunnerConfig(
        image=os.getenv("DEER_MINI_BASH_IMAGE", "python:3.12-slim"),
        timeout_seconds=_load_float(
            "DEER_MINI_BASH_TIMEOUT_SECONDS",
            "60",
        ),
        max_output_bytes=_load_int(
            "DEER_MINI_BASH_MAX_OUTPUT_BYTES",
            "20000",
        ),
        memory_limit=os.getenv("DEER_MINI_BASH_MEMORY_LIMIT", "512m"),
        cpu_limit=_load_float("DEER_MINI_BASH_CPU_LIMIT", "1"),
        pids_limit=_load_int("DEER_MINI_BASH_PIDS_LIMIT", "64"),
        network_enabled=_load_bool(
            "DEER_MINI_BASH_NETWORK_ENABLED",
            False,
        ),
        docker_binary=os.getenv("DEER_MINI_DOCKER_BINARY", "docker"),
    )
    return DockerCommandRunner(config)
