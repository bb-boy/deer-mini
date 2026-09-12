"""单进程的 Thread 容器管理：按需创建、跨 Run 复用、闲置回收。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import hashlib
import logging
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

from app.sandbox.base import CommandResult
from app.sandbox.docker_runner import DockerCommandRunner, DockerRunnerConfig
from app.sandbox.models import SandboxEntry


logger = logging.getLogger(__name__)
ThreadKey = tuple[str, str]


def default_sandbox_scope() -> str:
    """返回当前 deer_mini 后端专属的稳定标签值。"""
    return hashlib.sha256(
        str(Path(__file__).resolve().parents[2]).encode()
    ).hexdigest()[:16]


async def cleanup_orphaned_thread_sandboxes() -> None:
    """即使 Bash 当前关闭，也清理本项目上次进程遗留的 Docker 容器。"""
    docker_binary = os.getenv("DEER_MINI_DOCKER_BINARY", "docker")
    if shutil.which(docker_binary) is None:
        return
    runner = DockerCommandRunner(DockerRunnerConfig(docker_binary=docker_binary))
    try:
        await runner.remove_owned_containers(default_sandbox_scope())
    except Exception:
        # Bash 关闭时，Docker 服务不可用不应阻止整个 HTTP 服务启动。
        logger.warning("启动时无法检查遗留的 Thread 容器", exc_info=True)


class ThreadSandboxManager:
    """一份实例供所有 Run 共享；同一 Thread 的操作由同一把锁保护。"""

    def __init__(
        self,
        runner: DockerCommandRunner,
        *,
        scope: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._scope = scope or default_sandbox_scope()
        self._active: dict[ThreadKey, SandboxEntry] = {}
        self._warm_pool: dict[ThreadKey, SandboxEntry] = {}
        self._locks: dict[ThreadKey, asyncio.Lock] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    def _lock(self, key: ThreadKey) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def start(self) -> None:
        """启动时清理本项目上次进程遗留的容器，再开始定期回收。"""
        if self._reaper is not None:
            return
        if self._closed:
            raise RuntimeError("容器管理器已经关闭")
        # Mini 只运行一个后端进程；仅删除带有本项目专属标签的容器。
        await self._runner.remove_owned_containers(self._scope)
        self._reaper = asyncio.create_task(
            self._reap_loop(), name="thread-sandbox-reaper"
        )

    async def begin_run(
        self, *, user_id: str, thread_id: str, run_id: str, workspace_path: str
    ) -> None:
        """占用已有容器；没有容器时只登记，不提前启动 Docker。"""
        key = (user_id, thread_id)
        workspace = str(Path(workspace_path).resolve(strict=True))
        if not Path(workspace).is_dir():
            raise ValueError("Thread Workspace 不是目录")
        async with self._lock(key):
            if self._closed:
                raise RuntimeError("容器管理器已经关闭")
            entry = self._active.get(key)
            if entry is not None:
                if entry.active_run_id != run_id:
                    raise RuntimeError("同一 Thread 的另一个 Run 尚未归还容器")
                if entry.workspace_path != workspace:
                    raise ValueError("同一 Thread 的 Workspace 不一致")
                return
            entry = self._warm_pool.get(key)
            if entry is not None and entry.workspace_path != workspace:
                raise ValueError("同一 Thread 的 Workspace 不一致")
            entry = self._warm_pool.pop(key, None) or SandboxEntry(
                user_id=user_id, thread_id=thread_id, workspace_path=workspace
            )
            entry.active_run_id = run_id
            entry.idle_since = None
            self._active[key] = entry

    async def end_run(self, *, user_id: str, thread_id: str, run_id: str) -> None:
        """Run 收尾后归还，开始闲置计时；旧 Run 不能释放新 Run 的占用。"""
        key = (user_id, thread_id)
        async with self._lock(key):
            entry = self._active.get(key)
            if entry is None or entry.active_run_id != run_id:
                return
            del self._active[key]
            entry.active_run_id = None
            if entry.container_id is not None:
                entry.idle_since = self._clock()
                self._warm_pool[key] = entry

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
        key = (user_id, thread_id)
        # 锁覆盖整个 Bash，避免同一 Thread 的命令、删除和回收互相打断。
        async with self._lock(key):
            entry = self._active.get(key)
            if self._closed or entry is None or entry.active_run_id != run_id:
                raise RuntimeError("Bash 必须由当前 Run 领取 Thread 容器后执行")
            if str(Path(workspace_path).resolve(strict=True)) != entry.workspace_path:
                raise ValueError("Bash 的 Workspace 与 Thread 容器不一致")
            if entry.broken:
                await self._destroy(entry)
            if entry.container_id is not None:
                # Docker 不可连接时会报错；只有确认容器已停止/消失才重建。
                if not await self._runner.container_is_running(entry.container_id):
                    await self._destroy(entry)
            try:
                if entry.container_id is None:
                    # 先登记名字，再调用 Docker；启动被取消也不会丢失清理目标。
                    entry.container_id = f"deer-mini-thread-{uuid4().hex}"
                    await self._runner.start_container(
                        container_name=entry.container_id,
                        workspace_path=entry.workspace_path,
                        user_id=user_id,
                        thread_id=thread_id,
                        scope=self._scope,
                    )
                result = await self._runner.run_in_container(
                    container_name=entry.container_id, command=command
                )
                if result.timed_out:
                    # Runner 返回超时结果前已经确认删除了容器。
                    entry.container_id = None
                return result
            except BaseException:
                entry.broken = True
                cleanup = asyncio.create_task(self._destroy(entry))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await cleanup
                except Exception:
                    # 保留 broken 登记，下一次调用/回收继续尝试删除。
                    logger.exception("清理 Thread 容器失败：%s", entry.container_id)
                raise

    async def _destroy(self, entry: SandboxEntry) -> None:
        if entry.container_id is not None:
            entry.broken = True
            await self._runner.remove_container(entry.container_id)
            entry.container_id = None
        entry.broken = False

    async def reap_idle(self) -> list[str]:
        """只回收 warm pool；在锁内重新检查，避免删除刚被领取的容器。"""
        removed: list[str] = []
        for key in list(self._warm_pool):
            async with self._lock(key):
                entry = self._warm_pool.get(key)
                if entry is None or entry.idle_since is None:
                    continue
                if not entry.broken and (
                    self._clock() - entry.idle_since
                    < self._runner.config.idle_timeout_seconds
                ):
                    continue
                container_id = entry.container_id
                try:
                    await self._destroy(entry)
                except Exception:
                    logger.exception("回收闲置容器失败：%s", container_id)
                    continue
                del self._warm_pool[key]
                if container_id is not None:
                    removed.append(container_id)
        return removed

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self._runner.config.idle_check_interval_seconds)
            await self.reap_idle()

    async def delete_thread(
        self,
        *,
        user_id: str,
        thread_id: str,
        delete_workspace: Callable[[], Awaitable[None]],
    ) -> None:
        """先销毁容器，再删除 Thread/文件；期间不能重新领取该容器。"""
        key = (user_id, thread_id)
        async with self._lock(key):
            if key in self._active:
                raise RuntimeError("运行中的 Thread 不能删除")
            entry = self._warm_pool.get(key)
            if entry is not None:
                await self._destroy(entry)
                del self._warm_pool[key]
            await delete_workspace()

    async def close(self) -> None:
        """所有 Run 停止后调用；销毁本实例登记的容器并关闭回收任务。"""
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        failures: list[Exception] = []
        for key in set(self._active) | set(self._warm_pool):
            async with self._lock(key):
                entry = self._active.get(key) or self._warm_pool.get(key)
                if entry is None:
                    continue
                try:
                    await self._destroy(entry)
                except Exception as error:
                    failures.append(error)
                    continue
                self._active.pop(key, None)
                self._warm_pool.pop(key, None)
        if failures:
            raise RuntimeError("关闭时未能回收全部 Thread 容器") from failures[0]
