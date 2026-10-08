"""原生文件工具的受限 IO：Thread 路径、目录描述符、扫描预算和串行持久写入。"""
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path, PurePosixPath
import stat
import threading
import time
from typing import Any, Literal
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.messages import ToolCall
from app.domain.tools import ToolResult
from app.filesystem.thread_paths import ThreadPaths
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.storage.errors import StorageError, StorageLimitError, classify_os_error
from app.storage.file_io import AtomicWriter, bounded_read, child_directory_fd, directory_fd

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SCAN_ENTRIES = 10000
MAX_SCAN_DEPTH = 64
MAX_SKIPPED_DETAILS = 100
MAX_SKIPPED_BYTES = 256 * 1024
SCAN_SECONDS = 5.0
SKIP_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__", ".tool-results"}
Encoding = Literal["utf-8", "gb18030", "utf-16"]


class PathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=4096, description="当前 Thread 的文件或目录路径")

    @field_validator("path")
    @classmethod
    def nonblank_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path 不能为空白")
        return value


class NativeFileError(ValueError):
    """只含安全错误码；不可带入服务器绝对路径或文件正文。"""


@dataclass(frozen=True)
class FileTarget:
    thread_dir: Path
    parts: tuple[str, ...]

    @property
    def canonical(self) -> str:
        return "/".join(self.parts)


def internal_name(name: str) -> bool:
    return name == ".tool-results" or name.startswith((".storage-", ".upload-"))


def resolve_target(context: RuntimeContext, raw_path: str) -> FileTarget:
    try:
        paths = ThreadPaths(Path(context.workspace_path).parent)
        target = paths.resolve_agent_path(raw_path)
        parts = target.relative_to(paths.thread_dir).parts
        if not parts or any(internal_name(part) for part in parts):
            raise ValueError("内部路径")
        return FileTarget(paths.thread_dir, parts)
    except StatePersistenceError:
        raise
    except (ValueError, OSError, RuntimeError) as error:
        raise NativeFileError("invalid_path") from error


@contextmanager
def parent_fd(target: FileTarget, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """从 Thread 根逐级打开父目录；创建目录也必须先同步，禁止路径跟随链接。"""
    with ExitStack() as stack:
        fd = stack.enter_context(directory_fd(target.thread_dir))
        for part in target.parts[:-1]:
            fd = stack.enter_context(child_directory_fd(fd, part, create=create))
        yield fd, target.parts[-1]


def file_stat(fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise classify_os_error(error, operation="read", stage="open") from error


def check_regular(value: os.stat_result | None) -> None:
    if value is None:
        raise NativeFileError("not_found")
    if stat.S_ISDIR(value.st_mode):
        raise NativeFileError("directory")
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise NativeFileError("unsafe_file")


def read_bytes(target: FileTarget, limit: int = MAX_FILE_BYTES) -> bytes:
    with parent_fd(target) as (fd, name):
        check_regular(file_stat(fd, name))
        result = bounded_read(fd, name, limit)
        if result is None:
            raise NativeFileError("not_found")
        return result


_locks: WeakValueDictionary[str, Any] = WeakValueDictionary()
_locks_guard = threading.Lock()


def thread_lock(context: RuntimeContext) -> Any:
    # 锁的强引用由每个等待/执行中的操作持有，空闲 Thread 不在全局字典累积。
    key = str(Path(context.workspace_path).parent)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def mutate_file(
    context: RuntimeContext, path: str, *,
    overwrite: bool, require_existing: bool,
    transform: Callable[[bytes | None, str], tuple[bytes, dict[str, Any]]],
) -> dict[str, Any]:
    """输入修改函数，锁内校验/读取/提交；提交后失败升级为 Runtime 关键状态异常。"""
    with thread_lock(context):
        target = resolve_target(context, path)
        replaced = False
        try:
            with parent_fd(target, create=not require_existing) as (fd, name):
                value = file_stat(fd, name)
                if value is not None:
                    check_regular(value)
                    if not overwrite:
                        raise NativeFileError("already_exists")
                elif require_existing:
                    raise NativeFileError("not_found")
                old = bounded_read(fd, name, MAX_FILE_BYTES) if require_existing else None
                if require_existing and old is None:
                    raise NativeFileError("not_found")
                content, metadata = transform(old, target.canonical)
                if len(content) > MAX_FILE_BYTES:
                    raise StorageLimitError("文件超过 8 MiB 上限")
                with AtomicWriter(fd, name) as writer:
                    try:
                        writer.write(content)
                        if value is not None:
                            os.fchmod(writer.descriptor, stat.S_IMODE(value.st_mode) & 0o777)
                        writer.commit()
                    finally:
                        replaced = writer.replaced
            return {"path": target.canonical, **metadata}
        except StorageError as error:
            if replaced or error.commit_state != "not_committed":
                raise StatePersistenceError("文件变更未能确认持久化，停止本次运行") from error
            raise


def error_result(call: ToolCall, error: Exception) -> ToolResult:
    messages = {
        "invalid_path": "文件路径无效：只能访问当前 Thread 的普通文件，禁止链接和内部路径",
        "not_found": "文件或目录不存在",
        "directory": "目标是目录，需要普通文件",
        "unsafe_file": "目标不是安全的普通单链接文件",
        "already_exists": "文件已存在；只有 overwrite=true 才能覆盖",
        "missing_match": "未找到 old_string，文件未修改",
        "ambiguous_match": "old_string 匹配多处；请提供唯一文本或 replace_all=true",
        "invalid_pattern": "搜索模式无效",
        "invalid_regex": "正则表达式无效",
    }
    if isinstance(error, NativeFileError):
        message = messages.get(str(error), "文件操作失败")
    elif isinstance(error, StorageLimitError):
        message = "文件超过允许的大小上限（read/write/edit 为 8 MiB）"
    elif isinstance(error, StorageError):
        message = f"文件操作失败：{error.category}"
    elif isinstance(error, UnicodeError):
        message = "文件编码与指定编码不兼容"
    else:
        message = "文件操作失败"
    return ToolResult(call.id, call.name, message, is_error=True)


def json_result(call: ToolCall, value: dict[str, Any]) -> ToolResult:
    return ToolResult(call.id, call.name, json.dumps(value, ensure_ascii=False))


def validate_pattern(value: str) -> str:
    if (not value or len(value) > 4096 or not value.strip() or value.startswith("/")
            or "\\" in value or "\x00" in value or ".." in PurePosixPath(value).parts):
        raise ValueError("必须使用不越界的相对匹配模式")
    return value


def matches_glob(relative: str, pattern: str) -> bool:
    """按路径段匹配；** 可消费零段，因此 **/*.py 也匹配顶层文件。"""
    segments = PurePosixPath(pattern).parts
    states = {0}
    for part in PurePosixPath(relative).parts:
        # ** 的 epsilon 闭包。
        for index in range(len(segments)):
            if index in states and segments[index] == "**":
                states.add(index + 1)
        next_states = set()
        for index in states:
            if index == len(segments):
                continue
            segment = segments[index]
            if segment == "**":
                next_states.add(index)
            elif fnmatchcase(part, segment):
                next_states.add(index + 1)
        states = next_states
    for index in range(len(segments)):
        if index in states and segments[index] == "**":
            states.add(index + 1)
    return len(segments) in states


@dataclass
class ScanReport:
    deadline: float = field(default_factory=lambda: time.monotonic() + SCAN_SECONDS)
    skipped: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False
    entries: int = 0
    skipped_count: int = 0
    skipped_bytes: int = 2

    def skip(self, path: str, reason: str, *, stop: bool = False) -> None:
        self.skipped_count += 1
        detail = {"path": path, "reason": reason}
        size = len(json.dumps(detail, ensure_ascii=False).encode("utf-8")) + 2
        if len(self.skipped) < MAX_SKIPPED_DETAILS and self.skipped_bytes + size <= MAX_SKIPPED_BYTES:
            self.skipped.append(detail)
            self.skipped_bytes += size
        if stop:
            self.truncated = True

    def metadata(self) -> dict[str, Any]:
        return {"truncated": self.truncated, "skipped": self.skipped,
                "skipped_count": self.skipped_count,
                "skipped_truncated": self.skipped_count > len(self.skipped)}

    def expired(self, path: str) -> bool:
        if time.monotonic() >= self.deadline:
            self.skip(path, "time_limit", stop=True)
            return True
        return False


def walk_files(target: FileTarget, report: ScanReport) -> Iterator[tuple[int, str, str, str]]:
    """在 fd 生命周期内 yield 文件，限制深度/数量/时间；调用者提前结束须 close。"""
    def visit(fd: int, canonical: str, relative: str, depth: int) -> Iterator[tuple[int, str, str, str]]:
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    if report.expired(canonical):
                        return
                    if report.entries >= MAX_SCAN_ENTRIES:
                        report.skip(canonical, "entry_limit", stop=True)
                        return
                    report.entries += 1
                    child_path = canonical + "/" + entry.name
                    child_relative = relative + "/" + entry.name if relative else entry.name
                    if internal_name(entry.name):
                        report.skip(child_path, "internal")
                        continue
                    try:
                        value = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(value.st_mode):
                            if entry.name in SKIP_DIRECTORIES:
                                report.skip(child_path, "excluded_directory")
                            elif depth >= MAX_SCAN_DEPTH:
                                report.skip(child_path, "depth_limit", stop=True)
                            else:
                                with child_directory_fd(fd, entry.name) as child:
                                    yield from visit(child, child_path, child_relative, depth + 1)
                                if report.truncated:
                                    return
                        elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
                            yield fd, entry.name, child_path, child_relative
                        else:
                            report.skip(child_path, "unsafe_file")
                    except StorageError as error:
                        report.skip(child_path, error.category)
                    except OSError as error:
                        report.skip(child_path, classify_os_error(error, operation="scan", stage="read").category)
        except OSError as error:
            report.skip(canonical, classify_os_error(error, operation="scan", stage="read").category)

    with parent_fd(target) as (fd, name):
        value = file_stat(fd, name)
        if value is None:
            raise NativeFileError("not_found")
        if stat.S_ISDIR(value.st_mode):
            if name in SKIP_DIRECTORIES:
                report.skip(target.canonical, "excluded_directory")
                return
            with child_directory_fd(fd, name) as child:
                yield from visit(child, target.canonical, "", 0)
        else:
            check_regular(value)
            yield fd, name, target.canonical, name
