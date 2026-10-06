"""以调用方已校验的目录描述符为边界读写普通单链接文件，不执行重试。

业务调用方负责用户/Thread 目录选择和大小约束。写入只有文件同步、替换及
目录同步全部完成才返回；取消或领域异常继续传播，临时文件尽力清理。
"""

from collections.abc import Iterator
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import sys
from types import TracebackType
from uuid import uuid4

from app.storage.errors import (
    StorageError, StorageLimitError, UnsafeStoragePathError, classify_os_error,
)


def _validate_name(name: str, operation: str) -> None:
    if not isinstance(name, str) or not name or name in {".", ".."} or any(c in name for c in "/\\\x00"):
        raise UnsafeStoragePathError(operation=operation)


def _validate_regular(value: os.stat_result, operation: str) -> None:
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise UnsafeStoragePathError(operation=operation)


def _close_descriptor(fd: int, *, operation: str, preserve_error: bool) -> None:
    try:
        os.close(fd)
    except OSError as error:
        if not preserve_error:
            raise classify_os_error(error, operation=operation, stage="cleanup") from error


def _validate_destination(fd: int, name: str) -> None:
    try:
        value = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise classify_os_error(error, operation="write", stage="open") from error
    _validate_regular(value, "write")


@contextmanager
def directory_fd(path: Path) -> Iterator[int]:
    """逐级打开绝对目录，禁止任意中间目录为符号链接，返回调用期间持有的 fd。"""
    if not path.is_absolute() or ".." in path.parts:
        raise UnsafeStoragePathError(operation="open")
    fd: int | None = None
    try:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except OSError as error:
        if fd is not None:
            _close_descriptor(fd, operation="open", preserve_error=True)
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeStoragePathError(operation="open") from error
        raise classify_os_error(error, operation="open", stage="open") from error
    try:
        yield fd
    finally:
        _close_descriptor(fd, operation="open", preserve_error=sys.exception() is not None)


@contextmanager
def child_directory_fd(fd: int, name: str, *, create: bool = False) -> Iterator[int]:
    """相对已持有的父目录打开子目录；创建模式总是同步父目录后才允许写入。

    即使子目录已存在也同步父目录，以覆盖上次 mkdir 成功但父目录同步失败的情况。
    符号链接和非目录入口拒绝；清理失败不能覆盖取消或原始持久化失败。
    """
    _validate_name(name, "open")
    child: int | None = None
    stage = "open"
    try:
        try:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        if create:
            stage = "directory_sync"
            os.fsync(fd)
    except OSError as error:
        if child is not None:
            _close_descriptor(child, operation="open", preserve_error=True)
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeStoragePathError(operation="open") from error
        raise classify_os_error(error, operation="open", stage=stage,
                                commit_state="uncertain" if stage == "directory_sync" else "not_committed") from error
    try:
        yield child
    finally:
        _close_descriptor(child, operation="open", preserve_error=sys.exception() is not None)


@contextmanager
def open_regular_file(fd: int, name: str, *, operation: str = "read") -> Iterator[int]:
    """打开目录中的普通单链接文件；调用者持有 fd 期间不会重新按路径读内容。"""
    _validate_name(name, operation)
    descriptor: int | None = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        _validate_regular(os.fstat(descriptor), operation)
    except OSError as error:
        if descriptor is not None:
            _close_descriptor(descriptor, operation=operation, preserve_error=True)
        if error.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR}:
            raise UnsafeStoragePathError(operation=operation) from error
        raise classify_os_error(error, operation=operation, stage="open") from error
    except BaseException:
        if descriptor is not None:
            _close_descriptor(descriptor, operation=operation, preserve_error=True)
        raise
    try:
        yield descriptor
    finally:
        _close_descriptor(descriptor, operation=operation, preserve_error=sys.exception() is not None)


def bounded_read(fd: int, name: str, limit: int) -> bytes | None:
    if limit < 0:
        raise ValueError("读取上限不能为负数")
    try:
        with open_regular_file(fd, name) as descriptor:
            if os.fstat(descriptor).st_size > limit:
                raise StorageLimitError("文件超过读取上限")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                data = os.read(descriptor, min(remaining, 64 * 1024))
                if not data:
                    break
                chunks.append(data)
                remaining -= len(data)
            if remaining == 0:
                raise StorageLimitError("文件超过读取上限")
            return b"".join(chunks)
    except StorageError as error:
        if error.category == "not_found":
            return None
        raise
    except OSError as error:
        raise classify_os_error(error, operation="read", stage="read") from error


class AtomicWriter:
    """流式写入私有临时文件，由调用者显式 commit；退出时清理未提交暂存文件。"""

    def __init__(self, fd: int, name: str, *, prefix: str = ".storage-", suffix: str = ".tmp") -> None:
        _validate_name(name, "write")
        self.directory = fd
        self.name = name
        self.temporary = prefix + uuid4().hex + suffix
        _validate_name(self.temporary, "write")
        self.descriptor: int | None = None
        self.replaced = False

    def __enter__(self) -> "AtomicWriter":
        _validate_destination(self.directory, self.name)
        try:
            self.descriptor = os.open(self.temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                      0o600, dir_fd=self.directory)
        except OSError as error:
            raise classify_os_error(error, operation="write", stage="open") from error
        return self

    def write(self, data: bytes) -> None:
        if self.descriptor is None or self.replaced:
            raise ValueError("暂存文件未打开或已经提交")
        if not isinstance(data, bytes):
            raise TypeError("写入数据必须是 bytes")
        remaining = memoryview(data)
        try:
            while remaining:
                count = os.write(self.descriptor, remaining)
                if count == 0:
                    raise OSError(errno.EIO, "write made no progress")
                remaining = remaining[count:]
        except OSError as error:
            raise classify_os_error(error, operation="write", stage="write") from error

    def commit(self) -> None:
        if self.descriptor is None or self.replaced:
            raise ValueError("暂存文件未打开或已经提交")
        stage = "file_sync"
        try:
            _validate_regular(os.fstat(self.descriptor), "write")
            os.fsync(self.descriptor)
            stage = "replace"
            _validate_destination(self.directory, self.name)
            current = os.stat(self.temporary, dir_fd=self.directory, follow_symlinks=False)
            opened = os.fstat(self.descriptor)
            _validate_regular(current, "write")
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise UnsafeStoragePathError(operation="write", stage="replace")
            os.replace(self.temporary, self.name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            self.replaced = True
            stage = "directory_sync"
            os.fsync(self.directory)
        except OSError as error:
            raise classify_os_error(error, operation="write", stage=stage,
                                    commit_state="uncertain" if self.replaced else "not_committed") from error

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        cleanup_error: OSError | None = None
        if self.descriptor is not None:
            try:
                os.close(self.descriptor)
            except OSError as error:
                cleanup_error = error
            self.descriptor = None
        if not self.replaced:
            try:
                os.unlink(self.temporary, dir_fd=self.directory)
            except FileNotFoundError:
                pass
            except OSError as error:
                cleanup_error = error
        if cleanup_error is not None and exc is None:
            raise classify_os_error(cleanup_error, operation="write", stage="cleanup",
                                    commit_state="committed" if self.replaced else "not_committed") from cleanup_error


def atomic_write(fd: int, name: str, data: bytes) -> None:
    with AtomicWriter(fd, name) as writer:
        writer.write(data)
        writer.commit()


def confirm_durable(fd: int, name: str) -> None:
    """核实既有结果时补做文件和目录同步，不替换文件、不刷新内容更新时间。"""
    stage = "file_sync"
    try:
        with open_regular_file(fd, name, operation="verify") as descriptor:
            os.fsync(descriptor)
            stage = "directory_sync"
            os.fsync(fd)
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            opened = os.fstat(descriptor)
            _validate_regular(current, "verify")
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise StorageError(backend="file", operation="verify", category="io", stage="verify",
                                   commit_state="uncertain", recovery="verify")
    except OSError as error:
        raise classify_os_error(error, operation="verify", stage=stage, commit_state="uncertain") from error
