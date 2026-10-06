"""只恢复已登记的文件保存，绝不重新调用模型或重放工具。

一个用户的实际保存串行，退避只存时间不占锁。同步提交及结果登记在
同一个工作线程完成；asyncio 取消必须等它结束，随后继续传播。
"""
import asyncio
import logging
import math
import os
from datetime import datetime, timedelta

from app.memory.operations import PreparedMemoryOperation, MemoryConflictError, MemoryVerificationError
from app.memory.store import MemoryStore
from app.repositories.memory_task_repository import MemoryTaskRepository
from app.runtime.async_io import run_sync
from app.storage.errors import StorageError, classify_os_error

logger = logging.getLogger(__name__)


def positive_setting(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须为有限正数")
    return value


class MemoryRecovery:
    """可注入账本时钟和退避参数；启动周期扫描，关闭后不再发起新尝试。"""

    def __init__(self, store: MemoryStore, repository: MemoryTaskRepository, *,
                 retry_base_seconds: float = 30, retry_max_seconds: float = 3600,
                 max_attempts: int = 12, concurrency: int = 4, poll_seconds: float = 5) -> None:
        for value in (retry_base_seconds, retry_max_seconds, poll_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("调度间隔必须是有限正数")
        if max_attempts < 1 or concurrency < 1:
            raise ValueError("尝试次数和并发上限必须为正数")
        self.store, self.repository = store, repository
        self.retry_base_seconds, self.retry_max_seconds = retry_base_seconds, retry_max_seconds
        self.max_attempts, self.concurrency, self.poll_seconds = max_attempts, concurrency, poll_seconds
        self._poll_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closing = False
        self._cleanup_after: datetime | None = None

    @classmethod
    def configured(cls, store: MemoryStore, repository: MemoryTaskRepository) -> 'MemoryRecovery':
        return cls(store, repository,
            retry_base_seconds=positive_setting("DEER_MINI_MEMORY_RETRY_BASE_SECONDS", 30),
            retry_max_seconds=positive_setting("DEER_MINI_MEMORY_RETRY_MAX_SECONDS", 3600),
            max_attempts=int(positive_setting("DEER_MINI_MEMORY_MAX_ATTEMPTS", 12)),
            concurrency=int(positive_setting("DEER_MINI_MEMORY_SAVE_CONCURRENCY", 4)),
            poll_seconds=positive_setting("DEER_MINI_MEMORY_POLL_SECONDS", 5))

    async def start(self) -> None:
        if self._task is not None:
            return
        await run_sync(self.repository.recover_inflight)
        self._task = asyncio.create_task(self._loop(), name="memory-save-recovery")

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                await self.run_ready()
            except (StorageError, OSError) as error:
                logger.warning("memory_recovery_scan_failed reason=%s", type(error).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    def stop_accepting(self) -> None:
        """停止未来尝试；已启动的同步提交仍由拥有它的协程等候。"""
        self._closing = True
        self._wake.set()

    async def run_ready(self) -> None:
        """执行当前到期项一次；失败项写入未来时间，不在本轮忙等。"""
        async with self._poll_lock:
            if self._closing:
                return
            if self._cleanup_after is None or self.repository.now() >= self._cleanup_after:
                await run_sync(self.repository.cleanup_expired_history)
                self._cleanup_after = self.repository.now() + timedelta(hours=1)
            # 此锁内没有上一轮在途提交；running 是上轮结果登记失败的遗留项。
            await run_sync(self.repository.recover_inflight)
            due = await run_sync(self.repository.due)
            by_user: dict[str, list[dict[str, str]]] = {}
            for item in due:
                by_user.setdefault(item["user_id"], []).append(item)
            semaphore = asyncio.Semaphore(self.concurrency)

            async def save_user(items: list[dict[str, str]]) -> None:
                async with semaphore:
                    for item in items:
                        if self._closing:
                            break
                        try:
                            await run_sync(self._attempt, item)
                        except (StorageError, OSError) as error:
                            # 账本失败不猜测文件状态；保留 running，后续扫描先核实。
                            logger.warning("memory_save_record_failed task=%s operation=%s reason=%s",
                                item["task_id"], item["operation_id"], type(error).__name__)
            # 所有用户的在途同步操作结束后才释放本轮锁，关键异常仍向上传播。
            results = await asyncio.gather(*(save_user(items) for items in by_user.values()), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    def _attempt(self, key: dict[str, str]) -> None:
        repo = self.repository
        item = repo.claim(key["user_id"], key["task_id"], key["operation_id"])
        if item is None:
            return
        error_fields = None
        next_at = None
        allow_write = item["allow_write"] and item["attempts"] <= self.max_attempts
        try:
            operation = PreparedMemoryOperation.from_dict(item["payload"])
            result = self.store.apply_operation(item["user_id"], operation, allow_write=allow_write)
            if result is not None:
                status, phase = "success", "complete"
            else:
                status = "cancelled" if item["cancel_requested"] else "failed"
                phase = "unapplied_verified"
        except MemoryConflictError:
            status, phase = "conflict", "compare"
            error_fields = {"category": "conflict", "recovery": "manual"}
        except (StorageError, OSError) as error:
            if isinstance(error, OSError):
                error = classify_os_error(error, operation="write", stage="write", commit_state="uncertain")
            technical = getattr(error, "storage_error", None) or error
            error_fields = technical.safe_fields()
            uncertain = (isinstance(error, MemoryVerificationError)
                         or error.commit_state in {"uncertain", "committed"}
                         or item["verify_first"] or not allow_write)
            status, phase = ("verifying" if uncertain else "waiting"), error.stage
            if technical.recovery == "manual":
                status = "verifying" if uncertain else "failed"
            elif allow_write and item["attempts"] < self.max_attempts:
                delay = min(self.retry_max_seconds, self.retry_base_seconds * 2 ** min(item["attempts"] - 1, 20))
                if technical.recovery == "wait":
                    delay = max(delay, min(60, self.retry_max_seconds))
                deadline = datetime.fromisoformat(item["deadline_at"])
                if repo.now() < deadline:
                    next_at = min(deadline, repo.now() + timedelta(seconds=delay))
                elif uncertain:
                    next_at = repo.now()
                else:
                    status = "failed"
            elif allow_write and uncertain:
                # 保存预算用尽后再核实一次；无法核实的项继续保留必要内容。
                next_at = repo.now()
            elif not uncertain:
                status = "failed"
        except (ValueError, TypeError, KeyError):
            status, phase = "failed", "validation"
            error_fields = {"category": "invalid_access", "recovery": "manual"}
        repo.finish(item["user_id"], item["task_id"], item["operation_id"],
                    status=status, phase=phase, error=error_fields, next_attempt_at=next_at)
        logger.info("memory_save_result task=%s operation=%s status=%s phase=%s error=%s",
                    item["task_id"], item["operation_id"], status, phase, error_fields)

    async def shutdown(self) -> None:
        self.stop_accepting()
        if self._task is not None:
            self._task.cancel("memory_recovery_shutdown")
            try:
                await self._task
            except asyncio.CancelledError:
                # 这里只消费本对象主动取消的后台任务；调用者的取消继续传播。
                if asyncio.current_task().cancelling():
                    raise
            self._task = None
